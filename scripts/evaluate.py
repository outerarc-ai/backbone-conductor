#!/usr/bin/env python3
"""Evaluate deterministic conflict rules against labeled synthetic scenarios."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT / "src"))

from backbone_conductor.conflicts import detect_conflicts  # noqa: E402
from backbone_conductor.models import (  # noqa: E402
    BackboneState,
    Conflict,
    Decision,
    Intent,
    Task,
)

LIMITATION = (
    "Synthetic, hand-labeled deterministic scenarios only. These results do not establish "
    "real-world conflict detection rates or semantic understanding."
)
DEFAULT_DATASET = REPOSITORY_ROOT / "evals" / "conflicts.json"


def finding_key(finding: Conflict | dict[str, Any]) -> tuple[str, str, tuple[str, ...]]:
    if isinstance(finding, Conflict):
        return finding.rule, finding.severity.value, tuple(sorted(finding.parties))
    if set(finding) != {"rule", "severity", "parties"}:
        raise ValueError("expected findings require exactly rule, severity, and parties")
    if not isinstance(finding["rule"], str) or not finding["rule"]:
        raise ValueError("expected rule must be a nonempty string")
    if finding["severity"] not in {"advisory", "blocking", "critical"}:
        raise ValueError("expected severity must be advisory, blocking, or critical")
    parties = finding["parties"]
    if (
        not isinstance(parties, list)
        or len(parties) < 2
        or any(not isinstance(party, str) or not party for party in parties)
        or len(set(parties)) != len(parties)
    ):
        raise ValueError("expected parties must contain at least two distinct string IDs")
    return finding["rule"], finding["severity"], tuple(sorted(parties))


def _finding_dict(key: tuple[str, str, tuple[str, ...]]) -> dict[str, Any]:
    return {"rule": key[0], "severity": key[1], "parties": list(key[2])}


def _state(scenario: dict[str, Any]) -> BackboneState:
    intents = [Intent.model_validate(item) for item in scenario.get("intents", [])]
    decisions = [Decision.model_validate(item) for item in scenario.get("decisions", [])]
    tasks = [Task.model_validate(item) for item in scenario.get("tasks", [])]
    for objects in (intents, decisions, tasks):
        if len({item.id for item in objects}) != len(objects):
            raise ValueError("scenario contains duplicate entity IDs")
    return BackboneState(
        intents={item.id: item for item in intents},
        decisions={item.id: item for item in decisions},
        tasks={item.id: item for item in tasks},
    )


def evaluate_dataset(path: Path = DEFAULT_DATASET) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("dataset must have schema_version 1")
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, list) or not scenarios:
        raise ValueError("dataset must contain a nonempty scenarios list")
    seen_ids: set[str] = set()
    true_positives = false_positives = false_negatives = true_negative_scenarios = 0
    failures = []
    by_rule: dict[str, dict[str, int]] = {}
    for scenario in scenarios:
        if not isinstance(scenario, dict) or not isinstance(scenario.get("id"), str):
            raise ValueError("each scenario must have a string id")
        identifier = scenario["id"]
        if not identifier or identifier in seen_ids:
            raise ValueError("scenario IDs must be nonempty and unique")
        seen_ids.add(identifier)
        if not isinstance(scenario.get("expected"), list):
            raise ValueError(f"{identifier}: expected must be a list")
        expected = {finding_key(item) for item in scenario["expected"]}
        if len(expected) != len(scenario["expected"]):
            raise ValueError(f"{identifier}: duplicate expected findings")
        actual = {finding_key(item) for item in detect_conflicts(_state(scenario))}
        correct, extra, missing = expected & actual, actual - expected, expected - actual
        true_positives += len(correct)
        false_positives += len(extra)
        false_negatives += len(missing)
        true_negative_scenarios += int(not actual and not expected)
        for category, findings in (("tp", correct), ("fp", extra), ("fn", missing)):
            for key in findings:
                by_rule.setdefault(key[0], {"tp": 0, "fp": 0, "fn": 0})[category] += 1
        if extra or missing:
            failures.append(
                {
                    "scenario": identifier,
                    "unexpected": [_finding_dict(item) for item in sorted(extra)],
                    "missing": [_finding_dict(item) for item in sorted(missing)],
                }
            )
    predicted = true_positives + false_positives
    relevant = true_positives + false_negatives
    precision = true_positives / predicted if predicted else 1.0
    recall = true_positives / relevant if relevant else 1.0
    return {
        "dataset": str(path.resolve()),
        "provenance": data.get("provenance", "unspecified"),
        "scenario_count": len(scenarios),
        "passed_scenarios": len(scenarios) - len(failures),
        "true_negative_scenarios": true_negative_scenarios,
        "true_positives": true_positives,
        "false_positives": false_positives,
        "false_negatives": false_negatives,
        "precision": precision,
        "recall": recall,
        "f1": 2 * precision * recall / (precision + recall) if precision + recall else 0.0,
        "by_rule": dict(sorted(by_rule.items())),
        "failures": failures,
        "limitation": LIMITATION,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--json", action="store_true", help="Print a machine-readable report")
    args = parser.parse_args(argv)
    try:
        report = evaluate_dataset(args.dataset)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(f"invalid evaluation dataset: {error}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(
            f"Synthetic conflict evaluation: {report['passed_scenarios']}/{report['scenario_count']} scenarios passed"
        )
        print(
            f"Precision: {report['precision']:.1%} | Recall: {report['recall']:.1%} | "
            f"F1: {report['f1']:.1%}"
        )
        print(
            f"TP: {report['true_positives']} | FP: {report['false_positives']} | "
            f"FN: {report['false_negatives']} | Negative scenarios: {report['true_negative_scenarios']}"
        )
        for failure in report["failures"]:
            print(f"FAIL {failure['scenario']}: {json.dumps(failure, sort_keys=True)}")
        print(LIMITATION)
    return int(bool(report["failures"]))


if __name__ == "__main__":
    raise SystemExit(main())
