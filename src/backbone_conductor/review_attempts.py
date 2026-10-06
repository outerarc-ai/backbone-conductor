"""Private, redacted operational records for advisory review attempts."""

from __future__ import annotations

import json
import math
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from statistics import median


class ReviewAttemptLog:
    """Append outcomes outside every Backbone/Git worktree in a private JSONL file."""

    def __init__(
        self, filename: str | Path, forbidden_roots: tuple[Path, ...], *, create: bool = True
    ) -> None:
        source = Path(filename).expanduser()
        if not source.is_absolute():
            raise ValueError("Review attempt log path must be absolute")
        self.path = source.absolute()
        parent = self.path.parent.resolve(strict=True)
        if any(parent.is_relative_to(root.resolve()) for root in forbidden_roots):
            raise ValueError("Review attempt log must be outside the repository and Git directory")
        parent_mode = parent.stat().st_mode
        if not stat.S_ISDIR(parent_mode) or parent_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError("Review attempt log directory must be private (0700)")
        if parent.stat().st_uid != os.getuid():
            raise ValueError("Review attempt log directory must belong to the current user")
        if create:
            self._open().close()

    def _open(self):
        if self.path.is_symlink():
            raise ValueError("Review attempt log must be a regular file, not a symlink")
        descriptor = os.open(
            self.path,
            os.O_WRONLY
            | os.O_APPEND
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            0o600,
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            os.close(descriptor)
            raise ValueError("Review attempt log must be an owner-only regular file (0600)")
        return os.fdopen(descriptor, "ab", buffering=0)

    def record(
        self,
        *,
        task_id: str,
        model: str,
        provider: str,
        observed_version: str,
        artifact_sha: str,
        phase: str,
        elapsed_ms: float,
        error_type: str | None = None,
        status: str = "failed",
    ) -> None:
        if status not in {"failed", "committed"}:
            raise ValueError("Unknown review attempt status")
        if status == "failed" and not error_type:
            raise ValueError("Failed review attempt requires an error type")
        event = {
            "schema_version": 2,
            "recorded_at": datetime.now(UTC).isoformat(),
            "task_id": task_id,
            "model": model,
            "provider": provider,
            "observed_version": observed_version,
            "artifact_sha": artifact_sha,
            "status": status,
            "phase": phase,
            "elapsed_ms": elapsed_ms,
        }
        if error_type is not None:
            event["error_type"] = error_type
        data = (json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        with self._open() as stream:
            if stream.write(data) != len(data):
                raise OSError("Incomplete review attempt log write")
            os.fsync(stream.fileno())

    def summary(self) -> dict:
        """Summarize only recorded attempts; crashes and unlogged runs are unknown."""
        descriptor = os.open(
            self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO)
            or metadata.st_uid != os.getuid()
            or metadata.st_nlink != 1
        ):
            os.close(descriptor)
            raise ValueError("Review attempt log must be an owner-only regular file (0600)")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            events = [json.loads(line) for line in stream if line.strip()]
        committed = 0
        legacy_failures = 0
        failures: dict[str, int] = {}
        elapsed: list[float] = []
        for event in events:
            if not isinstance(event, dict) or event.get("status") not in {"failed", "committed"}:
                raise ValueError("Review attempt log contains an invalid event")
            version = event.get("schema_version")
            if version not in {None, 2} or (version is None and event["status"] != "failed"):
                raise ValueError("Review attempt log contains an unsupported event version")
            if event["status"] == "committed":
                committed += 1
            else:
                if version is None:
                    legacy_failures += 1
                phase = event.get("phase")
                if phase not in {"runtime", "commit"}:
                    raise ValueError("Review attempt log contains an invalid failure phase")
                failures[phase] = failures.get(phase, 0) + 1
            value = event.get("elapsed_ms")
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError("Review attempt log contains an invalid duration")
            elapsed.append(float(value))
        return {
            "recorded_attempts": len(events),
            "committed": committed,
            "failed": len(events) - committed,
            "legacy_failure_records": legacy_failures,
            "failure_by_phase": failures,
            "recorded_commit_rate": (
                round(committed / len(events), 4) if events and not legacy_failures else None
            ),
            "median_elapsed_ms": round(median(elapsed), 3) if elapsed else None,
            "token_usage": None,
            "cost": None,
        }
