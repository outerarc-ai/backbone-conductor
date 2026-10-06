#!/usr/bin/env python3
"""Verify paged audit history against a disposable repository of real Git commits.

Run from the checkout: PYTHONPATH=src python scripts/benchmark_audit_history.py
The default creates 1001 metadata commits, crossing the 1000-commit page limit.
Timing is local and is not a production latency commitment.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from backbone_conductor.audit_history import verify_all_history_pages
from backbone_conductor.storage import GitStore


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


def benchmark(updates: int = 1000, page_size: int = 1000) -> dict:
    if not 1 <= updates <= 5000:
        raise ValueError("Updates must be between 1 and 5000")
    if not 1 <= page_size <= 1000:
        raise ValueError("Page size must be between 1 and 1000")
    with tempfile.TemporaryDirectory(prefix="backbone-audit-benchmark-") as directory:
        repo = Path(directory)
        _git(repo, "init", "-b", "main")
        _git(repo, "config", "user.name", "Backbone Audit Benchmark")
        _git(repo, "config", "user.email", "audit-benchmark@example.invalid")
        _git(repo, "config", "commit.gpgsign", "false")
        store = GitStore(repo)
        store.init()

        started = time.perf_counter()
        for index in range(updates):
            store.mutate(
                lambda state, number=index: state.sessions.update(
                    {"benchmark-counter": {"value": number}}
                ),
                "Advance audit benchmark counter",
            )
            if (index + 1) % 100 == 0:
                print(
                    f"Created {index + 1}/{updates} metadata updates", file=sys.stderr, flush=True
                )
        write_seconds = time.perf_counter() - started

        started = time.perf_counter()
        first_page = store.verify_audit_history(limit=page_size)
        first_page_seconds = time.perf_counter() - started
        started = time.perf_counter()
        complete = verify_all_history_pages(
            lambda offset, head: store.verify_audit_history(page_size, offset, head), page_size
        )
        complete_seconds = time.perf_counter() - started

        commit_count = int(_git(repo, "rev-list", "--count", "HEAD", "--", ".backbone"))
        state = store.read()
        integrity_ok = (
            commit_count == updates + 1
            and first_page["checked"] == min(page_size, commit_count)
            and first_page["total_metadata_commits"] == commit_count
            and first_page["page_ok"]
            and first_page["truncated"] == (commit_count > page_size)
            and first_page["next_offset"] == (page_size if commit_count > page_size else None)
            and first_page["ok"] == (commit_count <= page_size)
            and complete["ok"]
            and complete["checked"] == commit_count
            and complete["pages"] == math.ceil(commit_count / page_size)
            and complete["head"] == first_page["head"] == state.version
            and complete["invalid_commits"] == []
            and state.sessions["benchmark-counter"]["value"] == updates - 1
            and not _git(repo, "status", "--porcelain")
        )
        return {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "updates": updates,
            "metadata_commits": commit_count,
            "page_size": page_size,
            "pages": complete["pages"],
            "first_page_truncated": first_page["truncated"],
            "write_seconds": round(write_seconds, 6),
            "first_page_seconds": round(first_page_seconds, 6),
            "complete_seconds": round(complete_seconds, 6),
            "integrity_ok": integrity_ok,
            "measurement_notes": [
                "All state changes use GitStore in a disposable local repository.",
                "The complete check repeats the first page; its timing includes every page.",
                "Timings exclude repository setup, final integrity checks, and cleanup.",
                "This one-machine sample does not establish a production latency target.",
            ],
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--updates", type=int, default=1000)
    parser.add_argument("--page-size", type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.updates <= 5000:
        parser.error("--updates must be between 1 and 5000")
    if not 1 <= args.page_size <= 1000:
        parser.error("--page-size must be between 1 and 1000")
    report = benchmark(args.updates, args.page_size)
    print(json.dumps(report, indent=2))
    return 0 if report["integrity_ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
