"""Git-backed, transactional persistence for the Backbone protocol.

The JSON snapshot is authoritative. Markdown and per-object JSON files are
generated views. Commits use a private index so application changes staged by
the repository's owner are never accidentally included in a Backbone commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from filelock import FileLock, Timeout

from backbone_conductor.audit import attributed_message
from backbone_conductor.conflicts import refresh_conflicts
from backbone_conductor.models import BackboneState, IntentStatus, TaskStatus

T = TypeVar("T")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}\Z")
_COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
AUDIT_EVENT_TYPES = (
    "initialize",
    "intent",
    "decision",
    "task",
    "artifact",
    "conflict",
    "reconcile",
    "migrate",
    "other",
)


def _audit_event_type(message: str) -> str:
    """Classify a commit subject for navigation, not as verified semantic evidence."""
    if not message.startswith("backbone: "):
        return "other"
    category = message.removeprefix("backbone: ").split(" ", 1)[0]
    if category == "conflicts":
        category = "conflict"
    return category if category in AUDIT_EVENT_TYPES else "other"


def _audit_time(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise StorageError(f"{name} must be an ISO 8601 timestamp with a timezone") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise StorageError(f"{name} must be an ISO 8601 timestamp with a timezone")
    return timestamp


class StorageError(RuntimeError):
    """A repository cannot safely complete a Backbone storage operation."""


@dataclass(frozen=True)
class GitOutputDigest:
    """A bounded preview and digest of Git stdout; incomplete means a hard limit was hit."""

    preview: bytes
    sha256: str | None
    size: int
    complete: bool


class GitStore:
    """Persist snapshots and audit records in an existing working-tree repo."""

    def __init__(self, repo: str | Path, *, lock_timeout: float = 10) -> None:
        self.root = Path(repo).expanduser().resolve()
        if not self.root.is_dir():
            raise StorageError(f"Repository directory does not exist: {self.root}")
        self.root = Path(self._git("rev-parse", "--show-toplevel").stdout.strip())
        self.git_dir = Path(self._git("rev-parse", "--absolute-git-dir").stdout.strip())
        self.path = self.root / ".backbone"
        self._index = Path(
            self._git("rev-parse", "--path-format=absolute", "--git-path", "index").stdout.strip()
        )
        self._lock = FileLock(self.git_dir / "backbone.lock", timeout=lock_timeout)

    @staticmethod
    def _git_environment(env: dict[str, str] | None = None) -> dict[str, str]:
        git_env = os.environ.copy()
        for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
            git_env.pop(key, None)
        git_env["GIT_OPTIONAL_LOCKS"] = "0"
        git_env.update(env or {})
        return git_env

    def _git(
        self,
        *args: str,
        check: bool = True,
        env: dict[str, str] | None = None,
        input: str | None = None,
        timeout: int = 30,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.root,
                env=self._git_environment(env),
                input=input,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StorageError(f"Could not run Git: {exc}") from exc
        if check and result.returncode:
            detail = result.stderr.strip() or result.stdout.strip()
            raise StorageError(f"git {args[0]} failed: {detail}")
        return result

    def _git_output_digest(
        self,
        *args: str,
        preview_limit: int,
        max_bytes: int | None = None,
        timeout: int = 30,
    ) -> GitOutputDigest:
        """Stream Git stdout without retaining more than the requested preview."""
        try:
            with subprocess.Popen(
                ["git", *args],
                cwd=self.root,
                env=self._git_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ) as process:

                def consume() -> GitOutputDigest:
                    assert process.stdout is not None
                    digest = hashlib.sha256()
                    preview = bytearray()
                    size = 0
                    while chunk := process.stdout.read1(65_536):
                        size += len(chunk)
                        if len(preview) < preview_limit:
                            preview.extend(chunk[: preview_limit - len(preview)])
                        if max_bytes is not None and size > max_bytes:
                            return GitOutputDigest(bytes(preview), None, size, False)
                        digest.update(chunk)
                    return GitOutputDigest(bytes(preview), digest.hexdigest(), size, True)

                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(consume)
                    try:
                        result = future.result(timeout=timeout)
                    except FutureTimeout as exc:
                        process.kill()
                        raise StorageError("Timed out reading Git output") from exc
                    except Exception:
                        process.kill()
                        raise
                    if not result.complete:
                        process.kill()
                        return result
                if process.wait(timeout=timeout):
                    raise StorageError(f"git {args[0]} failed while reading output")
                return result
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StorageError(f"Could not stream Git output: {exc}") from exc

    def _head(self) -> str | None:
        result = self._git("rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else None

    def _signing_args(self) -> tuple[str, ...]:
        """Make commit-tree honor the repository's normal commit signing setting."""
        configured = self._git("config", "--bool", "commit.gpgsign", check=False)
        if configured.returncode not in {0, 1}:
            raise StorageError("Invalid Git commit.gpgsign setting")
        return ("-S",) if configured.returncode == 0 and configured.stdout.strip() == "true" else ()

    def _ensure_safe(self) -> None:
        if self.path.is_symlink():
            raise StorageError(".backbone must not be a symbolic link")
        if self.path.exists() and not self.path.is_dir():
            raise StorageError(".backbone must be a directory")
        if self.path.exists():
            for directory, dirs, files in os.walk(self.path, followlinks=False):
                for name in dirs + files:
                    item = Path(directory) / name
                    if item.is_symlink():
                        raise StorageError(f"Symbolic links are not allowed in .backbone: {item}")
                    if not item.is_file() and not item.is_dir():
                        raise StorageError(f"Unsupported file in .backbone: {item}")

    def _ensure_clean(self) -> None:
        self._ensure_safe()
        status = self._git(
            "status", "--porcelain=v1", "--untracked-files=all", "--ignored", "--", ".backbone"
        ).stdout
        if status:
            raise StorageError(
                ".backbone has uncommitted changes; commit or restore them before continuing"
            )

    def _read(self) -> BackboneState:
        self._ensure_clean()
        state_path = self.path / "state.json"
        if not state_path.is_file():
            raise StorageError("Backbone is not initialized; run backbone init first")
        try:
            state = BackboneState.model_validate(json.loads(state_path.read_text(encoding="utf-8")))
        except (ValueError, OSError) as exc:
            raise StorageError(f"Invalid Backbone state: {exc}") from exc
        state.version = (
            self._git("log", "-1", "--format=%H", "--", ".backbone").stdout.strip() or None
        )
        return state

    def read(self) -> BackboneState:
        try:
            with self._lock:
                return self._read()
        except Timeout as exc:
            raise StorageError("Timed out waiting for the Backbone repository lock") from exc

    def read_version(self, version: str) -> BackboneState:
        """Read an exact reachable metadata commit without using mutable working-tree views."""
        if not isinstance(version, str) or not re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", version
        ):
            raise StorageError("A full Backbone metadata commit SHA is required")
        try:
            with self._lock:
                current = self._read()
                if (
                    current.version is None
                    or self._git(
                        "merge-base", "--is-ancestor", version, current.version, check=False
                    ).returncode
                    or self._git(
                        "log", "-1", "--format=%H", version, "--", ".backbone"
                    ).stdout.strip()
                    != version
                ):
                    raise StorageError("Backbone metadata version is not in current audit history")
                state = self._state_at(version)
                state.version = version
                return state
        except Timeout as exc:
            raise StorageError("Timed out waiting for the Backbone repository lock") from exc

    def init(self) -> BackboneState:
        """Create and commit the initial state, or return the existing state."""
        try:
            with self._lock:
                self._ensure_clean()
                if (self.path / "state.json").exists():
                    return self._read()
                self._commit(BackboneState(), "backbone: initialize")
                return self._read()
        except Timeout as exc:
            raise StorageError("Timed out waiting for the Backbone repository lock") from exc

    def mutate(self, callback: Callable[[BackboneState], T], message: str) -> T:
        """Run a change under the repository lock, then commit it atomically."""
        if not message.strip() or "\x00" in message:
            raise StorageError("A nonempty commit message without NUL characters is required")
        try:
            with self._lock:
                state = self._read()
                previous_version = state.version
                result = callback(state)
                # Validate after callbacks too: dict assignment can bypass model validators.
                state = BackboneState.model_validate(state.model_dump(mode="json"))
                state.parent_version = previous_version
                state.merged_parent_version = None
                state.version = None
                self._commit(state, message, expected_version=previous_version)
                return result
        except Timeout as exc:
            raise StorageError("Timed out waiting for the Backbone repository lock") from exc

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    @staticmethod
    def _markdown(kind: str, identifier: str, item: dict[str, Any]) -> str:
        title = item.get("summary") or item.get("problem") or item.get("title") or identifier
        lines = [f"# {kind}: {title}", "", f"ID: `{identifier}`", ""]
        for key, value in item.items():
            if key == "id" or value is None or value == [] or value == {}:
                continue
            lines.extend([f"## {key.replace('_', ' ').capitalize()}", ""])
            if isinstance(value, list):
                if all(isinstance(entry, dict) for entry in value):
                    lines.extend(["```json", GitStore._json(value).rstrip(), "```"])
                else:
                    lines.extend(f"- {entry}" for entry in value)
            elif isinstance(value, dict):
                lines.extend(["```json", GitStore._json(value).rstrip(), "```"])
            else:
                lines.append(str(value))
            lines.append("")
        return "\n".join(lines)

    def _render(self, state: BackboneState) -> dict[str, str]:
        data = state.model_dump(mode="json")
        # A commit cannot contain its own hash. Resolve it from Git when reading.
        data["version"] = None
        views = {"state.json": self._json(data)}
        overview = [
            "# Backbone",
            "",
            "Generated from `state.json`. Change state through Backbone tools.",
            "",
        ]
        for collection in ("intents", "decisions", "conflicts", "tasks", "sessions"):
            objects = data[collection]
            overview.extend([f"## {collection.capitalize()} ({len(objects)})", ""])
            for identifier, item in sorted(objects.items()):
                if collection == "sessions":
                    # Member/session names are free text, unlike protocol object IDs.
                    filename = "session-" + hashlib.sha256(identifier.encode()).hexdigest()
                elif not _SAFE_ID.fullmatch(identifier) or identifier in {".", ".."}:
                    raise StorageError(f"Unsafe {collection} identifier: {identifier!r}")
                else:
                    filename = identifier
                extension = "md" if collection in {"intents", "decisions", "sessions"} else "json"
                relative = f"{collection}/{filename}.{extension}"
                views[relative] = (
                    self._markdown(collection[:-1].capitalize(), identifier, item)
                    if extension == "md"
                    else self._json(item)
                )
                status = item.get("status", "")
                summary = item.get("summary") or item.get("problem") or item.get("title") or ""
                summary = str(summary).replace("\n", " ")
                overview.append(f"- [{identifier}]({relative}) {status} — {summary}".rstrip())
            if not objects:
                overview.append("None.")
            overview.append("")
        views["BACKBONE.md"] = "\n".join(overview)
        return views

    def _write_views(self, views: dict[str, str]) -> None:
        self._ensure_safe()
        self.path.mkdir(exist_ok=True)
        for collection in ("intents", "decisions", "conflicts", "tasks", "sessions"):
            directory = self.path / collection
            directory.mkdir(exist_ok=True)
            for item in directory.iterdir():
                if item.is_file() and item.suffix in {".json", ".md"}:
                    if item.relative_to(self.path).as_posix() not in views:
                        item.unlink()
        for relative, content in views.items():
            target = self.path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def _commit(
        self, state: BackboneState, message: str, *, expected_version: str | None = None
    ) -> None:
        views = self._render(state)
        self._ensure_clean()
        head = self._head()
        if expected_version is not None:
            actual_version = (
                self._git("log", "-1", "--format=%H", head, "--", ".backbone").stdout.strip()
                if head
                else None
            )
            if actual_version != expected_version:
                raise StorageError(
                    "Backbone changed during the transaction; retry from current state"
                )
        branch_result = self._git("symbolic-ref", "-q", "HEAD", check=False)
        ref = branch_result.stdout.strip() if branch_result.returncode == 0 else "HEAD"
        index_lock = Path(f"{self._index}.lock")
        try:
            lock_fd = os.open(index_lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise StorageError(
                "Git index is locked by another operation; retry when it finishes"
            ) from exc
        os.close(lock_fd)
        changed_head = False
        commit: str | None = None
        try:
            with tempfile.TemporaryDirectory(prefix="backbone-", dir=self.git_dir) as temp:
                work = Path(temp)
                backup = work / "backup"
                had_backbone = self.path.exists()
                if had_backbone:
                    shutil.copytree(self.path, backup)
                try:
                    self._write_views(views)
                    commit_env = {"GIT_INDEX_FILE": str(work / "commit-index")}
                    self._git("read-tree", head or "--empty", env=commit_env)
                    self._git("add", "--force", "--all", "--", ".backbone", env=commit_env)
                    tree = self._git("write-tree", env=commit_env).stdout.strip()
                    parents = ["-p", head] if head else []
                    commit = self._git(
                        "commit-tree",
                        *self._signing_args(),
                        tree,
                        *parents,
                        input=attributed_message(message),
                    ).stdout.strip()

                    # Prepare an index which retains every unrelated staged change.
                    user_index = work / "user-index"
                    if self._index.exists():
                        shutil.copy2(self._index, user_index)
                    user_env = {"GIT_INDEX_FILE": str(user_index)}
                    if not user_index.exists():
                        self._git("read-tree", head or "--empty", env=user_env)
                    self._git("reset", "-q", commit, "--", ".backbone", env=user_env)
                    shutil.copy2(user_index, index_lock)

                    current_branch = self._git("symbolic-ref", "-q", "HEAD", check=False)
                    current_ref = (
                        current_branch.stdout.strip() if current_branch.returncode == 0 else "HEAD"
                    )
                    if current_ref != ref:
                        raise StorageError(
                            "Git branch changed during the Backbone transaction; retry"
                        )
                    # Compare-and-swap prevents overwriting a concurrent Git commit.
                    self._git("update-ref", "-m", message.splitlines()[0], ref, commit, head or "")
                    changed_head = True
                    os.replace(index_lock, self._index)
                except BaseException:
                    if changed_head and commit:
                        # Roll back only if our commit is still the current tip.
                        if head:
                            self._git("update-ref", ref, head, commit)
                        else:
                            self._git("update-ref", "-d", ref, commit)
                    if self.path.exists():
                        shutil.rmtree(self.path)
                    if had_backbone:
                        shutil.copytree(backup, self.path)
                    raise
        finally:
            index_lock.unlink(missing_ok=True)

    def log(
        self,
        limit: int = 50,
        *,
        author: str | None = None,
        http_principal: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, str]]:
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise StorageError("Log limit must be between 1 and 1000")
        if event_type is not None and event_type not in AUDIT_EVENT_TYPES:
            raise StorageError(f"Unknown audit event type: {event_type!r}")
        start = _audit_time(since, "since")
        end = _audit_time(until, "until")
        if start is not None and end is not None and start > end:
            raise StorageError("since must not be after until")
        with self._lock:
            self._ensure_clean()
            head = self._head()
            if head is None:
                return []
            commits = self._metadata_history_commits(head)
            if not commits:
                return []
            output = self._git(
                "log",
                "--full-history",
                "-z",
                "--format=%H%x00%an%x00%aI%x00%s%x00"
                "%(trailers:key=Backbone-HTTP-Principal,valueonly)%x00"
                "%(trailers:key=Backbone-HTTP-Role,valueonly)",
                head,
                "--",
                ".backbone",
            ).stdout
            fields = output.split("\x00")
            if fields and not fields[-1]:
                fields.pop()
            if len(fields) % 6:
                raise StorageError("Could not parse Backbone Git audit log")
            by_commit = {}
            for offset in range(0, len(fields), 6):
                commit, git_author, timestamp, message, principal, role = fields[
                    offset : offset + 6
                ]
                entry = {
                    "commit": commit,
                    "author": git_author,
                    "timestamp": timestamp,
                    "message": message,
                    "event_type": _audit_event_type(message),
                }
                principal, role = principal.strip(), role.strip()
                if principal and "\n" not in principal and role in {"admin", "member", "reviewer"}:
                    entry["http_principal"] = principal
                    entry["http_role"] = role
                by_commit[commit] = entry
            entries = []
            for commit in commits:
                if commit not in by_commit:
                    raise StorageError("Could not read complete Backbone Git audit log")
                entry = by_commit[commit]
                git_author = entry["author"]
                timestamp = entry["timestamp"]
                if author is not None and git_author != author:
                    continue
                if http_principal is not None and entry.get("http_principal") != http_principal:
                    continue
                if event_type is not None and entry["event_type"] != event_type:
                    continue
                if start is not None or end is not None:
                    event_time = datetime.fromisoformat(timestamp)
                    if start is not None and event_time < start:
                        continue
                    if end is not None and event_time > end:
                        continue
                entries.append(entry)
                if len(entries) == limit:
                    break
            if self._head() != head:
                raise StorageError("Git HEAD changed during audit log read; retry")
            self._ensure_clean()
            return entries

    def verify_audit_signatures(self, limit: int = 50) -> dict[str, Any]:
        """Verify the latest metadata commits using Git's configured trust store."""
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise StorageError("Log limit must be between 1 and 1000")
        with self._lock:
            self._ensure_clean()
            head = self._head()
            commits = self._metadata_history_commits(head) if head else []
            entries = []
            for commit in commits[:limit]:
                raw = self._git("cat-file", "-p", commit).stdout
                headers = raw.split("\n\n", 1)[0]
                signed = any(
                    line.startswith(("gpgsig ", "gpgsig-sha256 ")) for line in headers.splitlines()
                )
                if not signed:
                    status = "unsigned"
                else:
                    check = self._git("verify-commit", commit, check=False)
                    status = "valid" if check.returncode == 0 else "invalid"
                entries.append({"commit": commit, "signature": status})
            if self._head() != head:
                raise StorageError("Git HEAD changed during signature verification; retry")
            self._ensure_clean()
            total = len(commits)
        counts = {
            status: sum(item["signature"] == status for item in entries)
            for status in ("valid", "unsigned", "invalid")
        }
        return {
            "checked": len(entries),
            "limit": limit,
            "total_metadata_commits": total,
            "truncated": total > len(entries),
            "valid": counts["valid"],
            "unsigned": counts["unsigned"],
            "invalid": counts["invalid"],
            "all_inspected_signed_and_valid": bool(entries) and counts["valid"] == len(entries),
            "commits": entries,
        }

    def verify_current_snapshot(self) -> dict[str, Any]:
        """Check committed views and version links for the current metadata snapshot."""
        with self._lock:
            head = self._head()
            state = self._read()
            expected = {
                name: content.encode("utf-8") for name, content in self._render(state).items()
            }
            actual = {
                item.relative_to(self.path).as_posix(): item.read_bytes()
                for item in self.path.rglob("*")
                if item.is_file()
            }
            missing = sorted(expected.keys() - actual.keys())
            extra = sorted(actual.keys() - expected.keys())
            changed = sorted(
                name for name in expected.keys() & actual.keys() if expected[name] != actual[name]
            )
            version = state.version
            if version is None:
                raise StorageError("Current Backbone snapshot has no metadata commit")
            parents, expected_parent, expected_merged_parent = self._metadata_parents(version)
            links_ok = (
                len(parents) <= 2
                and state.parent_version == expected_parent
                and state.merged_parent_version == expected_merged_parent
            )
            if self._head() != head:
                raise StorageError("Git HEAD changed during snapshot verification; retry")
            self._ensure_clean()
            return {
                "version": version,
                "git_parent_count": len(parents),
                "view_count": len(expected),
                "missing_views": missing,
                "extra_views": extra,
                "changed_views": changed,
                "parent_version": state.parent_version,
                "expected_parent_version": expected_parent,
                "merged_parent_version": state.merged_parent_version,
                "expected_merged_parent_version": expected_merged_parent,
                "parent_links_ok": links_ok,
                "ok": not (missing or extra or changed) and links_ok,
            }

    def _metadata_parents(self, version: str) -> tuple[list[str], str | None, str | None]:
        lineage = self._git("rev-list", "--parents", "-n", "1", version).stdout.split()
        if not lineage or lineage[0] != version:
            raise StorageError("Could not read Backbone commit parents")
        parents = lineage[1:]

        def metadata_version(parent: str) -> str | None:
            return (
                self._git("log", "-1", "--format=%H", parent, "--", ".backbone").stdout.strip()
                or None
            )

        first = metadata_version(parents[0]) if parents else None
        second = metadata_version(parents[1]) if len(parents) == 2 else None
        return parents, first, second

    def _metadata_history_commits(self, head: str) -> list[str]:
        """List reachable metadata writes, including merges that discard side state."""
        history = self._git(
            "rev-list", "--full-history", "--parents", head, "--", ".backbone"
        ).stdout.splitlines()
        commits = []
        for row in history:
            commit, *parents = row.split()
            if len(parents) == 2:
                # A code merge can carry the first parent's metadata
                # unchanged while its other parent has an older snapshot.
                # Keep the merge if the side has independent metadata:
                # silently discarding that state needs to fail the audit.
                diff = self._git(
                    "diff-tree",
                    "--quiet",
                    "--no-ext-diff",
                    "--no-textconv",
                    parents[0],
                    commit,
                    "--",
                    ".backbone",
                    check=False,
                )
                if diff.returncode not in (0, 1):
                    raise StorageError("Could not compare metadata in a Git merge commit")
                if diff.returncode == 0:
                    first_metadata = self._git(
                        "log", "-1", "--format=%H", parents[0], "--", ".backbone"
                    ).stdout.strip()
                    side_metadata = self._git(
                        "log", "-1", "--format=%H", parents[1], "--", ".backbone"
                    ).stdout.strip()
                    if not side_metadata or side_metadata == first_metadata:
                        continue
                    if first_metadata:
                        ancestry = self._git(
                            "merge-base",
                            "--is-ancestor",
                            side_metadata,
                            first_metadata,
                            check=False,
                        )
                        if ancestry.returncode == 0:
                            continue
                        if ancestry.returncode != 1:
                            raise StorageError("Could not compare metadata ancestry in a Git merge")
            commits.append(commit)
        return commits

    def verify_audit_history(
        self, limit: int = 50, offset: int = 0, expected_head: str | None = None
    ) -> dict[str, Any]:
        """Inspect a HEAD-pinned page of reachable metadata commits."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise StorageError("History limit must be between 1 and 1000")
        if type(offset) is not int or offset < 0:
            raise StorageError("History offset must be a nonnegative integer")
        if expected_head is not None and (
            not isinstance(expected_head, str)
            or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", expected_head) is None
        ):
            raise StorageError("A full expected Git HEAD SHA is required")
        with self._lock:
            self._ensure_clean()
            head = self._head()
            if head is None:
                raise StorageError("Backbone has no Git history")
            if expected_head is not None and head != expected_head:
                raise StorageError("Git HEAD changed between history pages; restart verification")
            commits = self._metadata_history_commits(head)
            if not commits:
                raise StorageError("Backbone has no metadata history")
            if offset >= len(commits):
                raise StorageError("History offset is beyond the last metadata commit")
            object_format = self._git("rev-parse", "--show-object-format").stdout.strip()
            if object_format not in {"sha1", "sha256"}:
                raise StorageError("Unsupported Git object format for history verification")
            entries = [
                self._verify_historical_snapshot(commit, object_format)
                for commit in commits[offset : offset + limit]
            ]
            if self._head() != head:
                raise StorageError("Git HEAD changed during history verification; retry")
            self._ensure_clean()
            invalid = sum(not entry["ok"] for entry in entries)
            next_offset = offset + len(entries)
            truncated = len(commits) > next_offset
            return {
                "head": head,
                "limit": limit,
                "offset": offset,
                "checked": len(entries),
                "total_metadata_commits": len(commits),
                "truncated": truncated,
                "next_offset": next_offset if truncated else None,
                "invalid": invalid,
                "page_ok": invalid == 0,
                "ok": invalid == 0 and offset == 0 and not truncated,
                "commits": entries,
            }

    def _verify_historical_snapshot(self, commit: str, object_format: str) -> dict[str, Any]:
        entry: dict[str, Any] = {"commit": commit, "ok": False}
        try:
            snapshot = self._git("show", f"{commit}:.backbone/state.json", check=False)
        except UnicodeError:
            return {**entry, "error": "state.json is not UTF-8"}
        if snapshot.returncode:
            return {**entry, "error": "Missing state.json at metadata commit"}
        try:
            state = BackboneState.model_validate(json.loads(snapshot.stdout))
            expected = {
                f".backbone/{name}": content.encode("utf-8")
                for name, content in self._render(state).items()
            }
        except (ValueError, StorageError, UnicodeError) as exc:
            return {**entry, "error": f"Invalid state or generated views: {exc}"}
        try:
            listing = self._git("ls-tree", "-r", "-z", commit, "--", ".backbone").stdout
        except UnicodeError:
            return {**entry, "error": "Metadata tree has non-UTF-8 paths"}
        actual: dict[str, tuple[str, str, str]] = {}
        for record in listing.split("\x00"):
            if not record:
                continue
            try:
                header, path = record.split("\t", 1)
                mode, kind, oid = header.split(" ", 2)
            except ValueError:
                return {**entry, "error": "Metadata tree entry is malformed"}
            actual[path] = (mode, kind, oid)
        missing = sorted(expected.keys() - actual.keys())
        extra = sorted(actual.keys() - expected.keys())
        changed = []
        for path in sorted(expected.keys() & actual.keys()):
            content = expected[path]
            object_id = hashlib.new(
                object_format, b"blob " + str(len(content)).encode() + b"\x00" + content
            ).hexdigest()
            if actual[path] != ("100644", "blob", object_id):
                changed.append(path)
        parents, expected_parent, expected_merged = self._metadata_parents(commit)
        links_ok = (
            len(parents) <= 2
            and state.parent_version == expected_parent
            and state.merged_parent_version == expected_merged
        )
        return {
            **entry,
            "missing_views": missing,
            "extra_views": extra,
            "changed_views": changed,
            "parent_links_ok": links_ok,
            "ok": not (missing or extra or changed) and links_ok,
        }

    def sync(self, remote: str = "origin", branch: str | None = None) -> dict[str, Any]:
        """Explicitly push the current branch; never pull or force-push."""
        with self._lock:
            state = self._read()
            _, branch = self._remote_branch(remote, branch)
            result = self._git(
                "push", "--porcelain", "--", remote, f"HEAD:refs/heads/{branch}", timeout=120
            )
            return {
                "ok": True,
                "remote": remote,
                "branch": branch,
                "version": state.version,
                "detail": result.stdout.strip(),
            }

    def _remote_branch(self, remote: str, branch: str | None) -> tuple[str, str]:
        remotes = self._git("remote").stdout.splitlines()
        if remote.startswith("-") or remote not in remotes:
            raise StorageError(f"Unknown Git remote: {remote!r}")
        current = self._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
        current_branch = current.stdout.strip() if current.returncode == 0 else ""
        if branch is None:
            if not current_branch:
                raise StorageError("Detached HEAD requires an explicit destination branch")
            branch = current_branch
        if (
            branch.startswith("-")
            or self._git("check-ref-format", f"refs/heads/{branch}", check=False).returncode
        ):
            raise StorageError(f"Invalid destination branch: {branch!r}")
        return current_branch, branch

    def fetch_code_branch(self, remote: str, branch: str, expected_sha: str) -> dict[str, Any]:
        """Fetch one configured remote branch after checking its exact tip and ancestry."""
        if not isinstance(expected_sha, str) or not _COMMIT_SHA.fullmatch(expected_sha):
            raise StorageError("Expected code commit must be a full lowercase Git SHA")
        self._remote_branch(remote, branch)
        tracking = f"refs/remotes/{remote}/{branch}"
        if self._git("check-ref-format", tracking, check=False).returncode:
            raise StorageError("Invalid remote-tracking branch")
        temporary = f"refs/backbone/code-fetch/{uuid.uuid4().hex}"
        try:
            self._git(
                "fetch",
                "--no-tags",
                "--refmap=",
                remote,
                f"refs/heads/{branch}:{temporary}",
                timeout=120,
            )
            fetched = self._resolve_revision(temporary)
            if fetched != expected_sha:
                raise StorageError("Remote code branch tip differs from the expected commit")
            with self._lock:
                previous = self._git("rev-parse", "--verify", tracking, check=False)
                old_sha = self._resolve_revision(tracking) if previous.returncode == 0 else None
                if old_sha is not None and old_sha != fetched:
                    if self._git(
                        "merge-base", "--is-ancestor", old_sha, fetched, check=False
                    ).returncode:
                        raise StorageError("Remote code branch was rewritten; review it manually")
                if old_sha != fetched:
                    self._git("update-ref", tracking, fetched, old_sha or "0" * 40)
            return {
                "remote": remote,
                "branch": branch,
                "tracking_ref": f"{remote}/{branch}",
                "commit_sha": fetched,
                "updated": old_sha != fetched,
            }
        finally:
            self._git("update-ref", "-d", temporary, check=False)

    def _state_at(self, revision: str) -> BackboneState:
        result = self._git("show", f"{revision}:.backbone/state.json", check=False)
        if result.returncode:
            raise StorageError(f"Remote history has no Backbone state at {revision}")
        try:
            return BackboneState.model_validate(json.loads(result.stdout))
        except ValueError as exc:
            raise StorageError(f"Invalid Backbone state in remote history: {exc}") from exc

    @staticmethod
    def _object_delta(before: BackboneState, after: BackboneState) -> dict[str, list[str]]:
        return {
            collection: sorted(
                key
                for key in set(getattr(before, collection)) | set(getattr(after, collection))
                if getattr(before, collection).get(key) != getattr(after, collection).get(key)
            )
            for collection in ("intents", "decisions", "conflicts", "tasks", "sessions")
        }

    def _changed_paths(self, before: str, after: str) -> list[str]:
        output = self._git("diff", "--no-renames", "--name-only", "-z", before, after, "--").stdout
        return sorted(path for path in output.split("\x00") if path)

    @staticmethod
    def _merge_objects(
        base: BackboneState,
        local: BackboneState,
        remote: BackboneState,
        resolutions: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> tuple[BackboneState | None, dict[str, list[str]]]:
        """Three-way merge; competing edits need an explicit reviewed resolution."""
        missing = object()
        combined = local.model_dump(mode="json")
        collisions: dict[str, list[str]] = {}
        for collection in ("intents", "decisions", "conflicts", "tasks", "sessions"):
            before = getattr(base, collection)
            ours = getattr(local, collection)
            theirs = getattr(remote, collection)
            merged = {}
            conflicts = []
            for key in sorted(set(before) | set(ours) | set(theirs)):
                old = before.get(key, missing)
                left = ours.get(key, missing)
                right = theirs.get(key, missing)
                if left == right:
                    chosen = left
                elif left == old:
                    chosen = right
                elif right == old:
                    chosen = left
                else:
                    conflicts.append(key)
                    continue
                if chosen is not missing:
                    merged[key] = (
                        chosen.model_dump(mode="json") if hasattr(chosen, "model_dump") else chosen
                    )
            combined[collection] = merged
            collisions[collection] = conflicts
        expected = {(kind, key) for kind, keys in collisions.items() for key in keys}
        if resolutions is not None and not isinstance(resolutions, dict):
            raise StorageError("Reconciliation resolutions must be a JSON object")
        if expected and not resolutions:
            return None, collisions
        provided: dict[tuple[str, str], dict[str, Any]] = {}
        for kind, entries in (resolutions or {}).items():
            if kind not in collisions or not isinstance(entries, dict):
                raise StorageError(f"Invalid reconciliation resolution collection: {kind!r}")
            for key, specification in entries.items():
                provided[(kind, key)] = specification
        if set(provided) != expected:
            missing_keys = sorted(expected - set(provided))
            extra_keys = sorted(set(provided) - expected)
            raise StorageError(
                f"Resolutions must match competing objects exactly; "
                f"missing={missing_keys}, unexpected={extra_keys}"
            )
        for (kind, key), specification in provided.items():
            if not isinstance(specification, dict):
                raise StorageError(f"Invalid resolution for {kind}/{key}")
            if set(specification) == {"source"} and specification["source"] in {
                "local",
                "remote",
            }:
                side = local if specification["source"] == "local" else remote
                chosen = getattr(side, kind).get(key, missing)
                if chosen is not missing:
                    combined[kind][key] = (
                        chosen.model_dump(mode="json") if hasattr(chosen, "model_dump") else chosen
                    )
            elif set(specification) == {"value"} and isinstance(specification["value"], dict):
                combined[kind][key] = specification["value"]
            else:
                raise StorageError(f"Use local, remote, or a complete value for {kind}/{key}")
        combined["version"] = None
        combined["parent_version"] = local.version
        combined["merged_parent_version"] = remote.version
        state = BackboneState.model_validate(combined)
        GitStore._validate_reconciled_state(state)
        refresh_conflicts(state)
        return BackboneState.model_validate(state.model_dump(mode="json")), collisions

    @staticmethod
    def _validate_reconciled_state(state: BackboneState) -> None:
        """Reject object-level merges that break cross-object lifecycle invariants."""
        for intent in state.intents.values():
            if intent.parent_intent and intent.parent_intent not in state.intents:
                raise StorageError(f"Merged intent has missing parent: {intent.id}")
            if intent.supersedes:
                previous = state.intents.get(intent.supersedes)
                if previous is None or previous.status != IntentStatus.SUPERSEDED:
                    raise StorageError(f"Merged intent has invalid predecessor: {intent.id}")
                if not intent.change_reason:
                    raise StorageError(f"Merged intent lacks replacement reason: {intent.id}")
            visited = {intent.id}
            ancestor = intent.parent_intent
            while ancestor:
                if ancestor in visited:
                    raise StorageError(f"Merged intent parent cycle: {intent.id}")
                visited.add(ancestor)
                ancestor = state.intents[ancestor].parent_intent
        for decision in state.decisions.values():
            if decision.supersedes and decision.supersedes not in state.decisions:
                raise StorageError(f"Merged decision has missing predecessor: {decision.id}")
            if any(intent_id not in state.intents for intent_id in decision.related_intents):
                raise StorageError(f"Merged decision has missing related intent: {decision.id}")
        active_by_intent: dict[str, int] = {}
        for task in state.tasks.values():
            intent = state.intents.get(task.intent_id)
            if intent is None:
                raise StorageError(f"Merged task has missing intent: {task.id}")
            if task.status not in {TaskStatus.MERGED, TaskStatus.CANCELLED}:
                active_by_intent[task.intent_id] = active_by_intent.get(task.intent_id, 0) + 1
                if intent.status != IntentStatus.IN_PROGRESS:
                    raise StorageError(f"Merged active task has inconsistent intent: {task.id}")
        duplicate = [key for key, count in active_by_intent.items() if count > 1]
        if duplicate:
            raise StorageError(
                "Merged state has multiple active tasks for: " + ", ".join(duplicate)
            )

    def reconcile(
        self,
        remote: str,
        branch: str | None,
        expected_local_head: str,
        expected_remote_head: str,
        author: str,
        rationale: str,
        resolutions: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        """Merge reviewed metadata-only histories with an audited two-parent commit."""
        if not author.strip() or any(ord(char) < 32 for char in author):
            raise StorageError("A valid reconciliation author is required")
        if not rationale.strip() or "\x00" in rationale:
            raise StorageError("A reconciliation rationale is required")
        with self._lock:
            local_state = self._read()
            current_branch, branch = self._remote_branch(remote, branch)
            if branch != current_branch:
                raise StorageError("Reconciliation requires the checked-out branch")
            local_head = self._head()
            if local_head != expected_local_head:
                raise StorageError("Local HEAD changed since inspection; run refresh again")
            if self._git("status", "--porcelain=v1", "--untracked-files=all").stdout:
                raise StorageError("Reconciliation requires a clean worktree and index")
            temporary_ref = f"refs/backbone/fetch/{uuid.uuid4().hex}"
            try:
                self._git(
                    "fetch",
                    "--no-tags",
                    remote,
                    f"refs/heads/{branch}:{temporary_ref}",
                    timeout=120,
                )
                remote_head = self._resolve_revision(temporary_ref)
                if remote_head != expected_remote_head:
                    raise StorageError("Remote HEAD changed since inspection; run refresh again")
                if self._head() != local_head:
                    raise StorageError("Local HEAD changed during reconciliation; retry")
                base = self._git("merge-base", local_head, remote_head, check=False)
                if base.returncode or base.stdout.strip() in {local_head, remote_head}:
                    raise StorageError("Reconciliation requires two divergent histories")
                base_head = base.stdout.strip()
                local_paths = self._changed_paths(base_head, local_head)
                remote_paths = self._changed_paths(base_head, remote_head)
                code_paths = sorted(
                    path
                    for path in set(local_paths + remote_paths)
                    if not path.startswith(".backbone/")
                )
                if code_paths:
                    return {
                        "status": "requires_review",
                        "updated": False,
                        "reason": "code_changes",
                        "code_paths": code_paths,
                    }
                base_state = self._state_at(base_head)
                remote_state = self._state_at(remote_head)
                remote_state.version = (
                    self._git(
                        "log", "-1", "--format=%H", remote_head, "--", ".backbone"
                    ).stdout.strip()
                    or None
                )
                merged, collisions = self._merge_objects(
                    base_state, local_state, remote_state, resolutions
                )
                if merged is None:
                    return {
                        "status": "requires_review",
                        "updated": False,
                        "reason": "object_conflicts",
                        "objects": collisions,
                    }
                with tempfile.TemporaryDirectory(
                    prefix="backbone-reconcile-", dir=self.git_dir
                ) as temp:
                    index_env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
                    self._git("read-tree", local_head, env=index_env)
                    views = {
                        f".backbone/{path}": value for path, value in self._render(merged).items()
                    }
                    existing = self._git("ls-files", "-z", "--", ".backbone", env=index_env).stdout
                    for path in existing.split("\x00"):
                        if path and path not in views:
                            self._git("update-index", "--force-remove", "--", path, env=index_env)
                    for path, content in views.items():
                        blob = self._git(
                            "hash-object", "-w", "--stdin", input=content
                        ).stdout.strip()
                        self._git(
                            "update-index",
                            "--add",
                            "--cacheinfo",
                            f"100644,{blob},{path}",
                            env=index_env,
                        )
                    tree = self._git("write-tree", env=index_env).stdout.strip()
                message = (
                    f"backbone: reconcile {branch} by {author.strip()}\n\n{rationale.strip()}\n"
                )
                if resolutions:
                    choices = {
                        f"{kind}/{key}": spec.get("source", "merged")
                        for kind, entries in resolutions.items()
                        for key, spec in entries.items()
                    }
                    message += "\nResolutions: " + json.dumps(choices, sort_keys=True) + "\n"
                commit = self._git(
                    "commit-tree",
                    *self._signing_args(),
                    tree,
                    "-p",
                    local_head,
                    "-p",
                    remote_head,
                    input=attributed_message(message),
                ).stdout.strip()
                if (
                    self._head() != local_head
                    or self._git("status", "--porcelain=v1", "--untracked-files=all").stdout
                ):
                    raise StorageError("Local checkout changed during reconciliation; retry")
                self._git("merge", "--ff-only", "--no-edit", commit)
                updated = self._read()
                return {
                    "status": "reconciled",
                    "updated": True,
                    "commit": commit,
                    "parents": [local_head, remote_head],
                    "version": updated.version,
                    "conflicts": [
                        conflict.id
                        for conflict in updated.conflicts.values()
                        if not conflict.resolved
                    ],
                }
            finally:
                self._git("update-ref", "-d", temporary_ref, check=False)

    def refresh(self, remote: str = "origin", branch: str | None = None) -> dict[str, Any]:
        """Fetch a peer branch and fast-forward only; describe divergence for review."""
        with self._lock:
            local_state = self._read()
            current_branch, branch = self._remote_branch(remote, branch)
            if branch != current_branch:
                raise StorageError("Refresh destination must match the checked-out branch")
            local_head = self._head()
            if local_head is None:
                raise StorageError("Refresh requires a local commit")
            temporary_ref = f"refs/backbone/fetch/{uuid.uuid4().hex}"
            try:
                self._git(
                    "fetch",
                    "--no-tags",
                    remote,
                    f"refs/heads/{branch}:{temporary_ref}",
                    timeout=120,
                )
                remote_head = self._resolve_revision(temporary_ref)
                remote_state = self._state_at(remote_head)
                remote_version = (
                    self._git(
                        "log", "-1", "--format=%H", remote_head, "--", ".backbone"
                    ).stdout.strip()
                    or None
                )
                if not remote_version:
                    raise StorageError("Remote branch has no Backbone audit commit")
                if self._head() != local_head:
                    raise StorageError("Local branch changed during refresh; retry")
                result: dict[str, Any] = {
                    "remote": remote,
                    "branch": branch,
                    "local_head": local_head,
                    "remote_head": remote_head,
                    "local_version": local_state.version,
                    "remote_version": remote_version,
                }
                if local_head == remote_head:
                    return {**result, "status": "up_to_date", "updated": False}
                if (
                    self._git(
                        "merge-base", "--is-ancestor", local_head, remote_head, check=False
                    ).returncode
                    == 0
                ):
                    if self._git("status", "--porcelain=v1", "--untracked-files=all").stdout:
                        raise StorageError("Fast-forward requires a clean worktree and index")
                    self._git("merge", "--ff-only", "--no-edit", remote_head)
                    updated = self._read()
                    return {
                        **result,
                        "status": "fast_forwarded",
                        "updated": True,
                        "version": updated.version,
                    }
                if (
                    self._git(
                        "merge-base", "--is-ancestor", remote_head, local_head, check=False
                    ).returncode
                    == 0
                ):
                    return {**result, "status": "local_ahead", "updated": False}
                base = self._git("merge-base", local_head, remote_head, check=False)
                if base.returncode:
                    return {
                        **result,
                        "status": "unrelated",
                        "updated": False,
                        "detail": "Histories do not share an ancestor; inspect manually.",
                    }
                base_head = base.stdout.strip()
                base_state = self._state_at(base_head)
                local_delta = self._object_delta(base_state, local_state)
                remote_delta = self._object_delta(base_state, remote_state)
                local_paths = self._changed_paths(base_head, local_head)
                remote_paths = self._changed_paths(base_head, remote_head)
                overlap = {
                    kind: sorted(set(local_delta[kind]) & set(remote_delta[kind]))
                    for kind in local_delta
                }
                return {
                    **result,
                    "status": "diverged",
                    "updated": False,
                    "base_head": base_head,
                    "local_objects": local_delta,
                    "remote_objects": remote_delta,
                    "overlapping_objects": overlap,
                    "local_paths": local_paths,
                    "remote_paths": remote_paths,
                    "overlapping_paths": sorted(set(local_paths) & set(remote_paths)),
                    "detail": "Review and merge divergent Git history manually; no metadata was combined.",
                }
            finally:
                self._git("update-ref", "-d", temporary_ref, check=False)

    def _resolve_revision(self, revision: str) -> str:
        if not revision or revision.startswith("-") or "\x00" in revision:
            raise StorageError(f"Invalid Git revision: {revision!r}")
        result = self._git(
            "rev-parse", "--verify", "--end-of-options", f"{revision}^{{commit}}", check=False
        )
        if result.returncode:
            raise StorageError(f"Unknown Git revision: {revision!r}")
        return result.stdout.strip()

    def check_diff(self, base_ref: str, branch: str) -> dict[str, Any]:
        with self._lock:
            base, tip = self._resolve_revision(base_ref), self._resolve_revision(branch)
            comparison = f"{base}...{tip}"
            # Both sides of a rename affect scope/forbidden-path checks. Git's
            # default name-only output reports only the rename destination.
            paths = self._git("diff", "--no-renames", "--name-only", "-z", comparison, "--").stdout
            result = self._git("diff", "--check", comparison, "--", check=False)
            return {
                "ok": result.returncode == 0,
                "detail": (result.stdout + result.stderr).strip(),
                "changed_paths": [path for path in paths.split("\x00") if path],
            }
