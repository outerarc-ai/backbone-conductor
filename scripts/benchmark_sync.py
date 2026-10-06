#!/usr/bin/env python3
"""Measure push and fast-forward refresh across two disposable Git clones.

Run from the checkout: PYTHONPATH=src python scripts/benchmark_sync.py --runs 5
The remote is a local bare repository, so this is not a network latency SLA.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from backbone_conductor.service import Conductor


def _git(directory: Path, *args: str) -> str:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(key, None)
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    ).stdout.strip()


def benchmark(number: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="backbone-sync-benchmark-") as directory:
        root = Path(directory)
        remote, sender, receiver = root / "remote.git", root / "sender", root / "receiver"
        sender.mkdir()
        _git(root, "init", "--bare", "-b", "main", str(remote))
        _git(sender, "init", "-b", "main")
        _git(sender, "config", "user.name", "Backbone Sync Benchmark")
        _git(sender, "config", "user.email", "benchmark@example.invalid")
        _git(sender, "config", "commit.gpgsign", "false")
        _git(sender, "remote", "add", "origin", str(remote))
        first = Conductor(sender)
        first.initialize()
        first.sync()
        _git(root, "clone", str(remote), str(receiver))

        identifier = f"intent-sync-{number:04d}"
        first.create_intent(
            {
                "id": identifier,
                "author": "benchmark",
                "problem": "Verify a pushed Backbone change reaches a second clone",
                "proposed_outcome": "The receiving clone has the exact new Git state",
            }
        )
        started = time.perf_counter()
        pushed = first.sync()
        push_seconds = time.perf_counter() - started

        second = Conductor(receiver)
        started = time.perf_counter()
        refreshed = second.refresh()
        refresh_seconds = time.perf_counter() - started

        sender_state, receiver_state = first.state(), second.state()
        sender_head, receiver_head = (
            _git(sender, "rev-parse", "HEAD"),
            _git(receiver, "rev-parse", "HEAD"),
        )
        remote_head = _git(remote, "rev-parse", "HEAD")
        return {
            "run": number,
            "push_seconds": round(push_seconds, 6),
            "refresh_seconds": round(refresh_seconds, 6),
            "integrity_ok": (
                pushed["ok"]
                and refreshed["status"] == "fast_forwarded"
                and refreshed["updated"]
                and sender_head == receiver_head == remote_head
                and sender_state["version"] == receiver_state["version"]
                and identifier in receiver_state["intents"]
                and not _git(sender, "status", "--porcelain")
                and not _git(receiver, "status", "--porcelain")
            ),
            "push_status": pushed["ok"],
            "refresh_status": refreshed["status"],
        }


def _metrics(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "median": round(statistics.median(ordered), 6),
        "p95": round(ordered[math.ceil(len(ordered) * 0.95) - 1], 6),
        "max": round(ordered[-1], 6),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be between 1 and 20")
    reports = [benchmark(number) for number in range(args.runs)]
    report = {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "runs": args.runs,
        "integrity_ok": all(item["integrity_ok"] for item in reports),
        "push_seconds": _metrics([item["push_seconds"] for item in reports]),
        "refresh_seconds": _metrics([item["refresh_seconds"] for item in reports]),
        "per_run": reports,
        "measurement_notes": [
            "Each run uses a new bare Git remote and two independent local clones.",
            "Push and refresh are timed separately; repository setup, the write, and final integrity checks are excluded.",
            "The remote is local, so results exclude network, hosting, TLS and human delay.",
            "p95 uses nearest rank; these local measurements do not establish a production SLA.",
        ],
    }
    print(json.dumps(report, indent=2))
    return 0 if report["integrity_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
