#!/usr/bin/env python3
"""Measure simultaneous local metadata writes without a timing pass/fail gate.

Run from the checkout: PYTHONPATH=src python scripts/benchmark.py
Each spawned process creates one intent in a disposable Git repository. Workers
wait at a barrier so the reported operation latencies include lock contention.
"""

from __future__ import annotations

import argparse
import json
import math
import multiprocessing
import os
import platform
import queue
import statistics
import subprocess
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from backbone_conductor.service import Conductor


def _git(repo: Path, *args: str) -> str:
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"):
        env.pop(key, None)
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    ).stdout.strip()


def _worker(repo: str, number: int, barrier, results) -> None:
    try:
        conductor = Conductor(repo)
        barrier.wait(timeout=30)
        started = time.perf_counter()
        intent = conductor.create_intent(
            {
                "id": f"intent-bench-{number:04d}",
                "author": f"worker-{number:04d}",
                "problem": f"Independent benchmark operation {number}",
                "proposed_outcome": "Persist exactly one independently identifiable intent",
            }
        )
        finished = time.perf_counter()
        results.put(
            {
                "worker": number,
                "id": intent["id"],
                "started": started,
                "finished": finished,
                "latency_seconds": finished - started,
            }
        )
    except Exception as exc:
        results.put({"worker": number, "error": f"{type(exc).__name__}: {exc}"})


def benchmark(processes: int = 10) -> dict:
    if not 1 <= processes <= 32:
        raise ValueError("Process count must be between 1 and 32")
    context = multiprocessing.get_context("spawn")
    started_at = datetime.now(UTC).isoformat()
    with tempfile.TemporaryDirectory(prefix="backbone-benchmark-") as directory:
        repo = Path(directory)
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Backbone Benchmark")
        _git(repo, "config", "user.email", "benchmark@example.invalid")
        # Do not depend on global signing settings in this disposable repository.
        _git(repo, "config", "commit.gpgsign", "false")
        conductor = Conductor(repo)
        conductor.initialize()
        results = context.Queue()
        barrier = context.Barrier(processes)
        workers = [
            context.Process(target=_worker, args=(str(repo), number, barrier, results))
            for number in range(processes)
        ]
        reports: list[dict] = []
        total_started = time.perf_counter()
        try:
            for worker in workers:
                worker.start()
            deadline = time.monotonic() + 60
            for _ in workers:
                try:
                    reports.append(results.get(timeout=max(0.01, deadline - time.monotonic())))
                except queue.Empty:
                    reports.append(
                        {"error": "Worker results exceeded the 60-second harness timeout"}
                    )
                    break
        finally:
            for worker in workers:
                if worker.pid is not None:
                    worker.join(timeout=1)
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(timeout=5)
            results.close()
            results.join_thread()

        state = conductor.state()
        commits = int(_git(repo, "rev-list", "--count", "HEAD"))
        clean = not _git(repo, "status", "--porcelain")
        total_elapsed = time.perf_counter() - total_started
        expected = {f"intent-bench-{number:04d}" for number in range(processes)}
        actual = set(state["intents"])
        successful = [report for report in reports if "latency_seconds" in report]
        latencies = sorted(report["latency_seconds"] for report in successful)
        errors = [report for report in reports if "error" in report]
        errors.extend(
            {"worker": number, "error": f"Worker exit code {worker.exitcode}"}
            for number, worker in enumerate(workers)
            if worker.exitcode != 0
        )
        metrics = (
            {
                "median": round(statistics.median(latencies), 6),
                "p95": round(latencies[math.ceil(len(latencies) * 0.95) - 1], 6),
                "max": round(max(latencies), 6),
            }
            if latencies
            else None
        )
        write_elapsed = (
            max(report["finished"] for report in successful)
            - min(report["started"] for report in successful)
            if successful
            else None
        )
        return {
            "started_at": started_at,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "processes": processes,
            "writes_per_process": 1,
            "successful_writes": len(successful),
            "persisted_intents": len(actual),
            "missing_intent_ids": sorted(expected - actual),
            "unexpected_intent_ids": sorted(actual - expected),
            "git_commits": commits,
            "expected_git_commits": processes + 1,
            "working_tree_clean": clean,
            "integrity_ok": (
                actual == expected
                and len(successful) == processes
                and commits == processes + 1
                and clean
                and not errors
            ),
            "latency_seconds": metrics,
            "write_window_seconds": round(write_elapsed, 6) if write_elapsed is not None else None,
            "total_elapsed_seconds": round(total_elapsed, 6),
            "errors": errors,
            "measurement_notes": [
                "Latency measures create_intent including lock waiting and Git persistence.",
                "Workers use a start barrier; process/conductor startup is excluded from latency.",
                "Total elapsed includes process startup, operation execution, shutdown and verification; repository setup is excluded.",
                "p95 uses nearest rank; with 10 samples it equals the maximum.",
                "This is one local run, not a production SLA; no timing threshold is asserted.",
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--processes", type=int, default=10)
    args = parser.parse_args()
    if not 1 <= args.processes <= 32:
        parser.error("--processes must be between 1 and 32")
    report = benchmark(args.processes)
    print(json.dumps(report, indent=2))
    return 0 if report["integrity_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
