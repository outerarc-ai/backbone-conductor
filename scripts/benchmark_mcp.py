#!/usr/bin/env python3
"""Exercise concurrent, separate MCP stdio sessions against one Git ledger.

Run from the checkout: PYTHONPATH=src python scripts/benchmark_mcp.py
Use --runs for repeated independent repositories and a larger latency sample.
This checks integrity, not a latency service-level agreement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from backbone_conductor.service import Conductor

SOURCE_ROOT = str(Path(__file__).resolve().parents[1] / "src")


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


async def _exercise(repo: Path, clients: int) -> tuple[list[dict], int]:
    ready = 0
    start = asyncio.Event()
    lock = asyncio.Lock()

    async def worker(number: int) -> dict:
        nonlocal ready
        member = f"member-{number:04d}"
        intent_id = f"intent-mcp-bench-{number:04d}"
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "backbone_conductor", "--repo", str(repo), "mcp", "--member", member],
            env={"PYTHONPATH": SOURCE_ROOT},
        )
        try:
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                async with lock:
                    ready += 1
                    if ready == clients:
                        start.set()
                await start.wait()
                started = time.perf_counter()
                result = await session.call_tool(
                    "create_intent",
                    {
                        "intent_data": {
                            "id": intent_id,
                            "problem": f"Independent MCP operation {number}",
                            "proposed_outcome": "Persist one member-bound intent",
                        }
                    },
                )
                finished = time.perf_counter()
                if result.isError:
                    raise RuntimeError(
                        result.content[0].text if result.content else "MCP tool error"
                    )
                created = json.loads(result.content[0].text)
                if created["id"] != intent_id or created["author"] != member:
                    raise ValueError("MCP response changed intent identity or member binding")
                return {
                    "member": member,
                    "intent_id": intent_id,
                    "started": started,
                    "finished": finished,
                    "latency_seconds": finished - started,
                }
        except Exception as exc:
            start.set()
            return {"member": member, "error": f"{type(exc).__name__}: {exc}"}

    tasks = [asyncio.create_task(worker(number)) for number in range(clients)]
    try:
        reports = await asyncio.wait_for(asyncio.gather(*tasks), timeout=90)
    except TimeoutError:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        reports = [
            task.result()
            if task.done() and not task.cancelled() and task.exception() is None
            else {"member": f"member-{number:04d}", "error": "90-second harness timeout"}
            for number, task in enumerate(tasks)
        ]
    return reports, ready


def benchmark(clients: int = 10) -> dict:
    if not 1 <= clients <= 20:
        raise ValueError("Client count must be between 1 and 20")
    with tempfile.TemporaryDirectory(prefix="backbone-mcp-benchmark-") as directory:
        repo = Path(directory)
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Backbone MCP Benchmark")
        _git(repo, "config", "user.email", "mcp-benchmark@example.invalid")
        _git(repo, "config", "commit.gpgsign", "false")
        Conductor(repo).initialize()

        total_started = time.perf_counter()
        reports, ready = asyncio.run(_exercise(repo, clients))
        elapsed = time.perf_counter() - total_started
        state = Conductor(repo).state()
        expected = {f"intent-mcp-bench-{number:04d}" for number in range(clients)}
        actual = set(state["intents"])
        authors_match = all(
            state["intents"][f"intent-mcp-bench-{number:04d}"]["author"] == f"member-{number:04d}"
            for number in range(clients)
            if f"intent-mcp-bench-{number:04d}" in state["intents"]
        )
        commits = int(_git(repo, "rev-list", "--count", "HEAD"))
        clean = not _git(repo, "status", "--porcelain")
        successful = [item for item in reports if "latency_seconds" in item]
        latencies = sorted(item["latency_seconds"] for item in successful)
        errors = [item for item in reports if "error" in item]
        metrics = (
            {
                "median": round(statistics.median(latencies), 6),
                "p95": round(latencies[math.ceil(len(latencies) * 0.95) - 1], 6),
                "max": round(max(latencies), 6),
            }
            if latencies
            else None
        )
        return {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "clients": clients,
            "simultaneously_ready": ready,
            "successful_calls": len(successful),
            "persisted_intents": len(actual),
            "missing_intent_ids": sorted(expected - actual),
            "unexpected_intent_ids": sorted(actual - expected),
            "member_authors_match": authors_match,
            "git_commits": commits,
            "expected_git_commits": clients + 1,
            "working_tree_clean": clean,
            "integrity_ok": (
                ready == clients
                and len(successful) == clients
                and actual == expected
                and authors_match
                and commits == clients + 1
                and clean
                and not errors
            ),
            "latency_seconds": metrics,
            "latency_samples_seconds": [round(value, 6) for value in latencies],
            "total_elapsed_seconds": round(elapsed, 6),
            "errors": errors,
            "measurement_notes": [
                "Each client owns an independent MCP stdio server process and member binding.",
                "Tool-call latency includes MCP transport, Git locking and persistence, but excludes session startup.",
                "Clients start tool calls after every session initializes; no latency threshold is asserted.",
                "This is a single local run, not a production service-level agreement.",
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=int, default=10)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.clients <= 20:
        parser.error("--clients must be between 1 and 20")
    if not 1 <= args.runs <= 20:
        parser.error("--runs must be between 1 and 20")
    reports = [benchmark(args.clients) for _ in range(args.runs)]
    if args.runs == 1:
        report = reports[0]
    else:
        samples = sorted(latency for item in reports for latency in item["latency_samples_seconds"])
        report = {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "clients_per_run": args.clients,
            "runs": args.runs,
            "successful_calls": sum(item["successful_calls"] for item in reports),
            "latency_samples": len(samples),
            "integrity_ok": all(item["integrity_ok"] for item in reports),
            "latency_seconds": {
                "median": round(statistics.median(samples), 6),
                "p95": round(samples[math.ceil(len(samples) * 0.95) - 1], 6),
                "max": round(max(samples), 6),
            }
            if samples
            else None,
            "per_run": [
                {
                    "integrity_ok": item["integrity_ok"],
                    "latency_seconds": item["latency_seconds"],
                    "total_elapsed_seconds": item["total_elapsed_seconds"],
                    "errors": item["errors"],
                }
                for item in reports
            ],
            "measurement_notes": [
                "Each run uses a new Git repository and independent MCP stdio sessions.",
                "Latency includes MCP transport, lock waiting and Git persistence, but not client startup.",
                "p95 uses nearest rank across all successful calls in these runs; this is not a production SLA.",
            ],
        }
    print(json.dumps(report, indent=2))
    return 0 if report["integrity_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
