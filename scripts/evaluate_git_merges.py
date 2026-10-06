#!/usr/bin/env python3
"""Compare Backbone path warnings with observed Git textual merge conflicts."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from backbone_conductor.conflicts import detect_conflicts  # noqa: E402
from backbone_conductor.models import BackboneState, Intent  # noqa: E402

DEFAULT_DATASET = REPOSITORY_ROOT / "evals" / "git_merge_cases.json"
SHA = re.compile(r"[0-9a-f]{40}\Z")
LIMITATION = (
    "Purposively selected historical Git merges, not a representative sample. Labels are "
    "textual merge conflicts, not human-adjudicated intent conflicts; changed paths are "
    "retrospective inputs rather than pre-work declarations. These metrics do not establish "
    "real-world intent-conflict precision or recall."
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120
    )
    if check and result.returncode:
        raise ValueError(f"git {args[0]} failed in {repo}: {result.stderr.strip()}")
    return result


def _paths(repo: Path, base: str, parent: str) -> list[str]:
    return _git(repo, "diff", "--name-only", "--no-renames", base, parent).stdout.splitlines()


def _verify_case(case: dict[str, Any], repo: Path) -> None:
    parents = _git(repo, "show", "-s", "--format=%P", case["merge_commit"]).stdout.split()
    if parents != case["parents"]:
        raise ValueError(f"{case['id']}: upstream merge parents changed or do not match")
    base = _git(repo, "merge-base", *parents).stdout.strip()
    for side, parent in (("left_paths", parents[0]), ("right_paths", parents[1])):
        if _paths(repo, base, parent) != case[side]:
            raise ValueError(f"{case['id']}: {side} differs from upstream Git history")
    replay = _git(repo, "merge-tree", "--write-tree", *parents, check=False)
    if replay.returncode not in {0, 1}:
        raise ValueError(f"{case['id']}: Git merge replay failed: {replay.stderr.strip()}")
    if (replay.returncode == 1) != case["textual_conflict"]:
        raise ValueError(f"{case['id']}: textual conflict label differs from Git merge replay")


def _validate_case(case: Any, seen_ids: set[str]) -> tuple[Intent, Intent]:
    required = {
        "id",
        "upstream",
        "merge_commit",
        "parents",
        "source_url",
        "left_paths",
        "right_paths",
        "textual_conflict",
        "label_note",
    }
    if not isinstance(case, dict) or set(case) != required:
        raise ValueError("each Git merge case must contain exactly the documented fields")
    identifier = case["id"]
    if not isinstance(identifier, str) or not identifier or identifier in seen_ids:
        raise ValueError("Git merge case IDs must be nonempty and unique")
    seen_ids.add(identifier)
    if not isinstance(case["upstream"], str) or not re.fullmatch(
        r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", case["upstream"]
    ):
        raise ValueError(f"{identifier}: upstream must be a GitHub owner/repo")
    if not isinstance(case["merge_commit"], str) or not SHA.fullmatch(case["merge_commit"]):
        raise ValueError(f"{identifier}: merge_commit must be a full Git SHA")
    if (
        not isinstance(case["parents"], list)
        or len(case["parents"]) != 2
        or len(set(case["parents"])) != 2
        or any(not isinstance(item, str) or not SHA.fullmatch(item) for item in case["parents"])
    ):
        raise ValueError(f"{identifier}: parents must be two distinct full Git SHAs")
    source = f"https://github.com/{case['upstream']}/commit/{case['merge_commit']}"
    if case["source_url"] != source:
        raise ValueError(f"{identifier}: source_url does not match upstream commit")
    if type(case["textual_conflict"]) is not bool or not isinstance(case["label_note"], str):
        raise ValueError(f"{identifier}: conflict label or note is invalid")
    if not case["label_note"].strip():
        raise ValueError(f"{identifier}: label note must be nonempty")
    for side in ("left_paths", "right_paths"):
        paths = case[side]
        if not isinstance(paths, list) or not paths or len(set(paths)) != len(paths):
            raise ValueError(f"{identifier}: {side} must contain unique changed paths")
    intents = []
    for side in ("left_paths", "right_paths"):
        intents.append(
            Intent(
                id=f"{identifier}-{side}",
                author=side,
                problem="Replay a historical branch's changed paths",
                proposed_outcome="Check path coordination warning",
                affected_paths=case[side],
            )
        )
    return intents[0], intents[1]


def evaluate_dataset(path: Path = DEFAULT_DATASET, repos: dict[str, Path] | None = None) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"schema_version", "label", "sampling", "cases"}:
        raise ValueError("Git merge dataset has an invalid top-level schema")
    if data["schema_version"] != 1 or data["label"] != "git_merge_tree_textual_conflict":
        raise ValueError("Git merge dataset label or schema version is unsupported")
    if not isinstance(data["sampling"], str) or not data["sampling"].strip():
        raise ValueError("Git merge dataset must describe sampling")
    if not isinstance(data["cases"], list) or not data["cases"]:
        raise ValueError("Git merge dataset must contain cases")
    if repos is not None and set(repos) != {case["upstream"] for case in data["cases"]}:
        raise ValueError("--verify-source must map every upstream repository in the dataset")

    seen_ids: set[str] = set()
    rows = []
    totals = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    for case in data["cases"]:
        left, right = _validate_case(case, seen_ids)
        if repos is not None:
            _verify_case(case, repos[case["upstream"]])
        state = BackboneState(intents={left.id: left, right.id: right})
        warning = any(
            item.rule == "resource_contention" and not item.resolved
            for item in detect_conflicts(state)
        )
        observed = case["textual_conflict"]
        outcome = "tp" if warning and observed else "fp" if warning else "fn" if observed else "tn"
        totals[outcome] += 1
        rows.append(
            {
                "id": case["id"],
                "source_url": case["source_url"],
                "textual_conflict": observed,
                "path_warning": warning,
                "outcome": outcome,
                "overlapping_paths": sorted(set(case["left_paths"]) & set(case["right_paths"])),
            }
        )
    positives = totals["tp"] + totals["fn"]
    predicted = totals["tp"] + totals["fp"]
    precision = totals["tp"] / predicted if predicted else None
    recall = totals["tp"] / positives if positives else None
    return {
        "dataset": str(path.resolve()),
        "sample_count": len(rows),
        "label": data["label"],
        "sampling": data["sampling"],
        "upstream_verified": repos is not None,
        "counts": totals,
        "precision_against_textual_merge": precision,
        "recall_against_textual_merge": recall,
        "cases": rows,
        "limitation": LIMITATION,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--verify-source",
        action="append",
        default=[],
        metavar="OWNER/REPO=PATH",
        help="Replay upstream Git history in a local clone (repeat for every repository)",
    )
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        repos = None
        if args.verify_source:
            repos = {}
            for mapping in args.verify_source:
                upstream, separator, directory = mapping.partition("=")
                if not separator or not upstream or not directory or upstream in repos:
                    raise ValueError("Invalid or duplicate --verify-source mapping")
                repos[upstream] = Path(directory)
        report = evaluate_dataset(args.dataset, repos)
    except (OSError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as exc:
        parser.error(f"invalid Git merge evaluation: {exc}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        counts = report["counts"]
        precision = report["precision_against_textual_merge"]
        recall = report["recall_against_textual_merge"]
        precision_text = f"{precision:.1%}" if precision is not None else "n/a"
        recall_text = f"{recall:.1%}" if recall is not None else "n/a"
        print(
            f"Git merge cases: {report['sample_count']} cases; "
            f"TP {counts['tp']}, FP {counts['fp']}, FN {counts['fn']}, TN {counts['tn']}"
        )
        print(
            f"Precision / recall against textual merge conflicts: {precision_text} / {recall_text}"
        )
        print(LIMITATION)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
