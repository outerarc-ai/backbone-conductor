from datetime import UTC, datetime

import pytest

from backbone_conductor.conflicts import detect_conflicts, paths_overlap
from backbone_conductor.models import Artifact, BackboneState, Decision, Intent, Severity, Task

NOW = datetime(2026, 9, 16, tzinfo=UTC)


def intent(identifier, **changes):
    return Intent(
        id=identifier,
        author=identifier,
        problem="Change service",
        proposed_outcome="Compatible service",
        created_at=NOW,
        **changes,
    )


def state_with(*intents, decisions=(), tasks=()):
    return BackboneState(
        intents={item.id: item for item in intents},
        decisions={item.id: item for item in decisions},
        tasks={item.id: item for item in tasks},
    )


def test_nonoverlapping_intents_have_no_conflicts():
    state = state_with(
        intent("a", affected_symbols=["A"], affected_paths=["src/a.py"]),
        intent("b", affected_symbols=["B"], affected_paths=["src/b.py"]),
    )
    assert detect_conflicts(state) == []


def test_shared_symbols_generate_advisory_with_review_packet():
    state = state_with(
        intent("a", affected_symbols=["A", "A"]), intent("b", affected_symbols=["A"])
    )
    (conflict,) = detect_conflicts(state)
    assert conflict.rule == "symbol_scope_overlap"
    assert conflict.evidence == {"symbols": ["A"]}
    assert conflict.severity == Severity.ADVISORY
    assert conflict.arbitration_packet["human_required"] is False
    assert conflict.arbitration_packet["detection"]["evidence"] == conflict.evidence
    assert len(conflict.arbitration_packet["options"]) == 3


@pytest.mark.parametrize(
    "operation,severity", [("remove", Severity.CRITICAL), ("replace", Severity.BLOCKING)]
)
@pytest.mark.parametrize("reverse", [False, True])
def test_destructive_operation_against_extension_requires_human(operation, severity, reverse):
    left, right = ({"A": operation}, {"A": "extend"})
    if reverse:
        left, right = right, left
    state = state_with(intent("a", operations=left), intent("b", operations=right))
    (conflict,) = detect_conflicts(state)
    assert conflict.rule == "replace_vs_extend"
    assert conflict.severity == severity
    assert conflict.arbitration_packet["human_required"] is True
    assert conflict.evidence["symbols"][0]["operations"] == {"a": left["A"], "b": right["A"]}


def test_shared_symbols_already_in_severe_conflict_not_duplicated_as_advisory():
    state = state_with(
        intent("a", affected_symbols=["A", "B"], operations={"A": "remove"}),
        intent("b", affected_symbols=["A", "B"], operations={"A": "extend"}),
    )
    conflicts = detect_conflicts(state)
    assert len(conflicts) == 2
    assert next(item for item in conflicts if item.severity == Severity.ADVISORY).evidence == {
        "symbols": ["B"]
    }


@pytest.mark.parametrize("status", ["completed", "superseded", "rejected"])
def test_terminal_intents_do_not_generate_symbol_or_resource_conflicts(status):
    state = state_with(
        intent("a", affected_symbols=["A"], affected_paths=["src/"], status=status),
        intent("b", affected_symbols=["A"], affected_paths=["src/a.py"]),
    )
    assert detect_conflicts(state) == []


def test_dependency_conflict_preserves_direction_and_requires_review():
    dependencies = [
        Decision(
            id="a",
            author="alice",
            decision_type="api",
            summary="Extend",
            rationale="New requirement",
            depends_on=["Service"],
            created_at=NOW,
        ),
        Decision(
            id="b",
            author="bob",
            decision_type="api",
            summary="Remove",
            rationale="Obsolete",
            removes_symbols=["Service"],
            created_at=NOW,
        ),
    ]
    state = state_with(decisions=dependencies)
    (conflict,) = detect_conflicts(state)
    assert conflict.rule == "dependency_conflict"
    assert conflict.evidence == {
        "dependencies": [{"symbol": "Service", "dependent_decision": "a", "removing_decision": "b"}]
    }
    assert conflict.severity == Severity.BLOCKING
    assert conflict.arbitration_packet["human_required"] is True


@pytest.mark.parametrize("status", ["superseded", "reverted"])
def test_retired_decisions_do_not_generate_dependency_conflicts(status):
    state = state_with(
        decisions=[
            Decision(
                id="a",
                author="alice",
                decision_type="api",
                summary="Extend",
                rationale="Needed",
                depends_on=["A"],
            ),
            Decision(
                id="b",
                author="bob",
                decision_type="api",
                summary="Remove",
                rationale="Old",
                removes_symbols=["A"],
                status=status,
            ),
        ]
    )
    assert detect_conflicts(state) == []


@pytest.mark.parametrize(
    "left,right,expected",
    [
        ("src/a.py", "src/a.py", True),
        ("src/", "src/a.py", True),
        ("src/a.py", "src", True),
        ("src/auth", "src/auth/models.py", True),
        ("src/auth", "src/authentication.py", False),
        ("src/a.py", "src/a.pyc", False),
        ("src/a.py", "src/b.py", False),
        ("./src//a.py", "src/a.py", True),
    ],
)
def test_path_scope_comparison_respects_directory_boundaries(left, right, expected):
    assert paths_overlap(left, right) is expected
    assert paths_overlap(right, left) is expected


def test_resource_contention_has_precise_path_evidence():
    state = state_with(
        intent("a", affected_paths=["src/", "other.txt"]),
        intent("b", affected_paths=["src/auth.py", "different.txt"]),
    )
    (conflict,) = detect_conflicts(state)
    assert conflict.rule == "resource_contention"
    assert conflict.evidence == {"overlaps": [{"left": "src", "right": "src/auth.py"}]}
    assert conflict.severity == Severity.BLOCKING


def test_findings_are_stable_deduplicated_and_leave_inputs_untouched():
    a = intent("a", affected_symbols=["B", "A", "A"], affected_paths=["src/"])
    b = intent("b", affected_symbols=["A", "B"], affected_paths=["src/auth.py"])
    state = state_with(a, b)
    before = state.model_dump_json()
    first = detect_conflicts(state)
    second = detect_conflicts(state_with(b, a))
    assert first == second
    assert state.model_dump_json() == before
    assert len(first) == len({item.id for item in first}) == 2


def test_resolutions_are_preserved_but_new_evidence_requires_new_arbitration():
    state = state_with(intent("a", affected_symbols=["A"]), intent("b", affected_symbols=["A"]))
    (original,) = detect_conflicts(state)
    original.resolved = True
    original.resolution = {"action": "coordinate", "author": "alice"}
    state.conflicts[original.id] = original
    (repeated,) = detect_conflicts(state)
    assert repeated == original
    assert repeated is not original
    repeated.resolution["author"] = "bob"
    assert original.resolution["author"] == "alice"
    state.intents["a"].affected_symbols = ["B"]
    state.intents["b"].affected_symbols = ["B"]
    (updated,) = detect_conflicts(state)
    assert updated.id != original.id
    assert updated.resolved is False


def test_automatically_retired_conflict_reopens_when_evidence_returns():
    state = state_with(
        intent("a", affected_paths=["src/a.py"]), intent("b", affected_paths=["src/a.py"])
    )
    (previous,) = detect_conflicts(state)
    previous.resolved = True
    previous.resolution = {"action": "no_longer_applicable", "author": "system"}
    state.conflicts[previous.id] = previous
    (reopened,) = detect_conflicts(state)
    assert reopened.id == previous.id
    assert reopened.resolved is False
    assert reopened.resolution is None
    assert previous.resolved is True


def test_task_artifact_collision_is_reported_even_without_declared_scope():
    a, b = intent("a"), intent("b")
    tasks = [
        Task(
            id="task-a",
            member_id="alice",
            intent_id="a",
            artifact=Artifact(
                member_id="alice",
                intent_id="a",
                branch="alice",
                summary="Work",
                changed_paths=["src/auth.py"],
                created_at=NOW,
            ),
            created_at=NOW,
        ),
        Task(
            id="task-b",
            member_id="bob",
            intent_id="b",
            artifact=Artifact(
                member_id="bob",
                intent_id="b",
                branch="bob",
                summary="Work",
                changed_paths=["src/auth.py"],
                created_at=NOW,
            ),
            created_at=NOW,
        ),
    ]
    (conflict,) = detect_conflicts(state_with(a, b, tasks=tasks))
    assert conflict.parties == ["task-a", "task-b"]
    assert conflict.rule == "resource_contention"
    tasks[0].status = "merged"
    assert detect_conflicts(state_with(a, b, tasks=tasks)) == []


def test_declared_intent_scopes_are_not_duplicated_for_their_tasks():
    a, b = intent("a", affected_paths=["src/"]), intent("b", affected_paths=["src/auth.py"])
    tasks = [
        Task(id="task-b", member_id="alice", intent_id="a"),
        Task(id="task-a", member_id="bob", intent_id="b"),
    ]
    (conflict,) = detect_conflicts(state_with(a, b, tasks=tasks))
    assert conflict.parties == ["a", "b"]


def test_parallel_tasks_for_one_intent_still_collide_on_shared_scope():
    a = intent("a", affected_paths=["src/auth.py"])
    tasks = [
        Task(id="task-a", member_id="alice", intent_id="a"),
        Task(id="task-b", member_id="bob", intent_id="a"),
    ]
    (conflict,) = detect_conflicts(state_with(a, tasks=tasks))
    assert conflict.parties == ["task-a", "task-b"]


def test_additional_artifact_collision_cannot_reuse_resolved_intent_collision():
    a, b = intent("a", affected_paths=["a.py"]), intent("b", affected_paths=["a.py"])
    tasks = [
        Task(
            id="task-a",
            member_id="alice",
            intent_id="a",
            artifact=Artifact(
                member_id="alice",
                intent_id="a",
                branch="alice",
                summary="Work",
                changed_paths=["b.py"],
            ),
        ),
        Task(
            id="task-b",
            member_id="bob",
            intent_id="b",
            artifact=Artifact(
                member_id="bob", intent_id="b", branch="bob", summary="Work", changed_paths=["b.py"]
            ),
        ),
    ]
    state = state_with(a, b, tasks=tasks)
    conflicts = detect_conflicts(state)
    assert len(conflicts) == 2
    task_conflict = next(item for item in conflicts if item.parties == ["task-a", "task-b"])
    assert task_conflict.evidence["overlaps"] == [{"left": "b.py", "right": "b.py"}]
