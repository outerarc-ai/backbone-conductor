"""Deterministic conflict rules with auditable evidence and stable identities.

Detection describes declared scopes; it does not claim to understand arbitrary
code semantics. Historical records and arbitration decisions are kept by the
storage/service layer. This module neither mutates the supplied state nor Git.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from itertools import combinations
from typing import Any

from .models import (
    BackboneState,
    Conflict,
    ConflictType,
    DecisionStatus,
    Intent,
    IntentStatus,
    Severity,
    TaskStatus,
    normalize_path,
)

TERMINAL_INTENTS = {IntentStatus.COMPLETED, IntentStatus.SUPERSEDED, IntentStatus.REJECTED}
TERMINAL_DECISIONS = {DecisionStatus.SUPERSEDED, DecisionStatus.REVERTED}


def paths_overlap(left: str, right: str) -> bool:
    """Compare complete path segments, including a directory and descendants."""
    first = normalize_path(left).rstrip("/")
    second = normalize_path(right).rstrip("/")
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")


def _path_overlaps(left: list[str], right: list[str]) -> list[dict[str, str]]:
    # Slash suffixes distinguish input directories but are irrelevant to identity.
    return [
        {"left": first, "right": second}
        for first in sorted({path.rstrip("/") for path in left})
        for second in sorted({path.rstrip("/") for path in right})
        if paths_overlap(first, second)
    ]


def build_arbitration_packet(conflict: Conflict) -> dict[str, Any]:
    """Offer review choices without pretending to decide competing intentions."""
    return {
        "conflict_id": conflict.id,
        "conflict_type": conflict.conflict_type.value,
        "severity": conflict.severity.value,
        "parties": list(conflict.parties),
        "detection": {
            "method": conflict.detection_method,
            "rule": conflict.rule,
            "evidence": conflict.evidence,
        },
        "suggestion": (
            "Review the shared scope before integration."
            if conflict.severity == Severity.ADVISORY
            else "Coordinate the competing changes and record a human arbitration decision."
        ),
        "options": [
            {"action": "coordinate", "impact": "Agree compatible scopes or sequence the changes."},
            {"action": "accept_existing", "impact": "Keep one plan and revise the competing plan."},
            {
                "action": "override_existing",
                "impact": "Replace the earlier plan and update its dependants.",
                "requires": "human_approval",
            },
        ],
        "human_required": conflict.severity != Severity.ADVISORY,
    }


def _make_conflict(
    state: BackboneState,
    rule: str,
    conflict_type: ConflictType,
    severity: Severity,
    parties: list[str],
    evidence: dict[str, Any],
    created_at: datetime,
) -> Conflict:
    signature = {
        "rule": rule,
        "conflict_type": conflict_type.value,
        "severity": severity.value,
        "parties": sorted(parties),
        "evidence": evidence,
    }
    serialized = json.dumps(signature, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    conflict_id = "conflict-" + hashlib.sha256(serialized.encode()).hexdigest()[:24]
    previous = state.conflicts.get(conflict_id)
    if previous is not None:
        # Preserve human arbitration, but a previously inactive conflict must
        # reopen when its exact scope becomes active again (for example retries).
        restored = previous.model_copy(deep=True)
        if restored.resolution and restored.resolution.get("action") == "no_longer_applicable":
            restored.resolved = False
            restored.resolution = None
        return restored
    conflict = Conflict(id=conflict_id, created_at=created_at, **signature)
    conflict.arbitration_packet = build_arbitration_packet(conflict)
    return conflict


def _intent_conflicts(state: BackboneState, intents: list[Intent]) -> list[Conflict]:
    found: list[Conflict] = []
    for left, right in combinations(intents, 2):
        parties = [left.id, right.id]
        timestamp = max(left.created_at, right.created_at)
        shared = (set(left.affected_symbols) | left.operations.keys()) & (
            set(right.affected_symbols) | right.operations.keys()
        )
        destructive = []
        for symbol in sorted(shared):
            operations = (left.operations.get(symbol), right.operations.get(symbol))
            if "extend" in operations and ({"remove", "replace"} & set(operations)):
                destructive.append(
                    {
                        "symbol": symbol,
                        "operations": {left.id: operations[0], right.id: operations[1]},
                    }
                )
        if destructive:
            severity = (
                Severity.CRITICAL
                if any("remove" in item["operations"].values() for item in destructive)
                else Severity.BLOCKING
            )
            found.append(
                _make_conflict(
                    state,
                    "replace_vs_extend",
                    ConflictType.INTENT_OVERLAP,
                    severity,
                    parties,
                    {"symbols": destructive},
                    timestamp,
                )
            )
        advisory = sorted(shared - {item["symbol"] for item in destructive})
        if advisory:
            found.append(
                _make_conflict(
                    state,
                    "symbol_scope_overlap",
                    ConflictType.INTENT_OVERLAP,
                    Severity.ADVISORY,
                    parties,
                    {"symbols": advisory},
                    timestamp,
                )
            )
        overlaps = _path_overlaps(left.affected_paths, right.affected_paths)
        if overlaps:
            found.append(
                _make_conflict(
                    state,
                    "resource_contention",
                    ConflictType.RESOURCE_CONTENTION,
                    Severity.BLOCKING,
                    parties,
                    {"overlaps": overlaps},
                    timestamp,
                )
            )
    return found


def _decision_conflicts(state: BackboneState) -> list[Conflict]:
    found: list[Conflict] = []
    decisions = sorted(
        (item for item in state.decisions.values() if item.status not in TERMINAL_DECISIONS),
        key=lambda item: item.id,
    )
    for left, right in combinations(decisions, 2):
        dependencies = [
            {"symbol": symbol, "dependent_decision": dependent.id, "removing_decision": removing.id}
            for dependent, removing in ((left, right), (right, left))
            for symbol in sorted(set(dependent.depends_on) & set(removing.removes_symbols))
        ]
        if dependencies:
            found.append(
                _make_conflict(
                    state,
                    "dependency_conflict",
                    ConflictType.DECISION_CONFLICT,
                    Severity.BLOCKING,
                    [left.id, right.id],
                    {"dependencies": dependencies},
                    max(left.created_at, right.created_at),
                )
            )
    return found


def _task_conflicts(state: BackboneState, intents: list[Intent]) -> list[Conflict]:
    found: list[Conflict] = []
    by_id = {intent.id: intent for intent in intents}
    tasks = sorted(
        (
            task
            for task in state.tasks.values()
            if task.status not in {TaskStatus.MERGED, TaskStatus.CANCELLED}
            and task.intent_id in by_id
        ),
        key=lambda task: task.id,
    )
    for left, right in combinations(tasks, 2):
        left_intent, right_intent = by_id[left.intent_id], by_id[right.intent_id]
        left_paths = left_intent.affected_paths + (
            left.artifact.changed_paths if left.artifact else []
        )
        right_paths = right_intent.affected_paths + (
            right.artifact.changed_paths if right.artifact else []
        )
        overlaps = _path_overlaps(left_paths, right_paths)
        if left.intent_id != right.intent_id:
            # Intent declarations already generated a record for these exact
            # scopes. Still surface any additional collision from an artifact.
            declared = _path_overlaps(left_intent.affected_paths, right_intent.affected_paths)
            overlaps = [overlap for overlap in overlaps if overlap not in declared]
        if overlaps:
            found.append(
                _make_conflict(
                    state,
                    "resource_contention",
                    ConflictType.RESOURCE_CONTENTION,
                    Severity.BLOCKING,
                    [left.id, right.id],
                    {"overlaps": overlaps, "intent_ids": sorted({left.intent_id, right.intent_id})},
                    max(
                        left.created_at,
                        right.created_at,
                        left.artifact.created_at if left.artifact else left.created_at,
                        right.artifact.created_at if right.artifact else right.created_at,
                    ),
                )
            )
    return found


def detect_conflicts(state: BackboneState) -> list[Conflict]:
    """Return current findings, preserving exact previously arbitrated findings.

    Results are independent of dictionary insertion order. Terminal intents,
    retired decisions and merged tasks cannot generate new conflicts. IDs include
    concrete evidence, so changing a scope cannot inherit an old resolution.
    Existing resolutions are returned intact; callers filter ``resolved`` when
    deciding whether a finding blocks an operation.
    """
    intents = sorted(
        (intent for intent in state.intents.values() if intent.status not in TERMINAL_INTENTS),
        key=lambda intent: intent.id,
    )
    findings = (
        _intent_conflicts(state, intents)
        + _decision_conflicts(state)
        + _task_conflicts(state, intents)
    )
    return sorted(
        {finding.id: finding for finding in findings}.values(), key=lambda finding: finding.id
    )


def refresh_conflicts(state: BackboneState) -> list[Conflict]:
    """Recompute generated findings while retaining exact human resolutions."""
    detected = detect_conflicts(state)
    active = {conflict.id for conflict in detected}
    for conflict in detected:
        existing = state.conflicts.get(conflict.id)
        if (
            existing
            and existing.resolved
            and (existing.resolution or {}).get("action") != "no_longer_applicable"
        ):
            conflict = existing
        state.conflicts[conflict.id] = conflict
    for conflict in state.conflicts.values():
        if conflict.id not in active and not conflict.resolved:
            conflict.resolved = True
            conflict.resolution = {
                "action": "no_longer_applicable",
                "author": "conductor",
                "rationale": "The rule no longer detects this evidence in active work.",
            }
    return [state.conflicts[conflict.id] for conflict in detected]
