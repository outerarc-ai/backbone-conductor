from datetime import UTC, datetime
from itertools import product
from uuid import UUID

import pytest
from pydantic import ValidationError

from backbone_conductor.models import (
    Artifact,
    BackboneState,
    Conflict,
    Decision,
    DecisionStatus,
    Intent,
    IntentStatus,
    ReviewAnchor,
    Task,
    TaskStatus,
    normalize_path,
    transition_decision,
    transition_intent,
    transition_task,
)


def intent(**changes):
    return Intent(
        author="alice",
        problem="Duplicated payment logic",
        proposed_outcome="One service",
        **changes,
    )


def decision(**changes):
    return Decision(
        author="bob",
        decision_type="api_design",
        summary="One entry point",
        rationale="Consistency",
        **changes,
    )


def task(**changes):
    return Task(member_id="alice", intent_id="intent-1", **changes)


def artifact(**changes):
    return Artifact(
        member_id="alice",
        intent_id="intent-1",
        branch="work/alice",
        summary="Implemented",
        **changes,
    )


def test_generated_ids_and_timestamps_are_valid():
    objects = [intent(), decision(), task(), artifact()]
    for obj, prefix in zip(objects, ["intent", "decision", "task", "artifact"], strict=True):
        assert obj.id.startswith(prefix + "-")
        assert UUID(obj.id.removeprefix(prefix + "-")).version == 4
        assert obj.created_at.tzinfo is UTC
    assert intent().id != intent().id


def test_json_roundtrip_preserves_nested_models_and_datetimes():
    original_intent = intent(id="intent-1", operations={"PaymentService": "extend"})
    original_artifact = artifact(
        commit_sha="a" * 40, base_sha="b" * 40, checks={"diff_check": True}
    )
    original_task = task(artifact=original_artifact)
    state = BackboneState(
        intents={original_intent.id: original_intent},
        tasks={original_task.id: original_task},
        sessions={"alice": {"active_task": original_task.id}},
        version="c" * 40,
    )
    restored = BackboneState.model_validate_json(state.model_dump_json())
    assert restored == state
    assert isinstance(restored.tasks[original_task.id].artifact, Artifact)
    assert restored.intents["intent-1"].created_at.utcoffset().total_seconds() == 0


def test_approval_anchor_requires_completed_task_with_artifact():
    anchor = ReviewAnchor(
        decision_id="decision-review", reviewed_version="a" * 40, target_sha="b" * 40
    )
    merged = task(status=TaskStatus.MERGED, artifact=artifact(), approval=anchor)
    assert Task.model_validate_json(merged.model_dump_json()).approval == anchor
    with pytest.raises(ValidationError, match="approval anchor requires"):
        task(status=TaskStatus.SUBMITTED, artifact=artifact(), approval=anchor)
    with pytest.raises(ValidationError, match="approval anchor requires"):
        task(status=TaskStatus.MERGED, approval=anchor)


@pytest.mark.parametrize("factory", [intent, decision, task, artifact])
def test_naive_timestamps_are_rejected(factory):
    with pytest.raises(ValidationError, match="timezone"):
        factory(created_at=datetime(2026, 9, 16))


@pytest.mark.parametrize(
    "bad_id", ["../secret", "/absolute", ".", "..", "space name", "a\\b", "", "-prefix"]
)
def test_ids_cannot_escape_storage_paths(bad_id):
    with pytest.raises(ValidationError):
        intent(id=bad_id)


def test_unknown_fields_and_invalid_assignments_are_rejected():
    with pytest.raises(ValidationError, match="Extra inputs"):
        intent(unrecognized=True)
    value = intent()
    with pytest.raises(ValidationError):
        value.status = "unexpected"
    assert value.status == IntentStatus.DRAFT
    with pytest.raises(ValidationError):
        value.created_at = datetime(2026, 9, 16)
    with pytest.raises(ValidationError):
        intent(operations={"PaymentService": "erase"})
    with pytest.raises(ValidationError):
        intent(operations={" ": "remove"})


def test_mutable_defaults_are_independent():
    left, right = intent(), intent()
    left.affected_symbols.append("PaymentService")
    left.operations["PaymentService"] = "extend"
    assert right.affected_symbols == []
    assert right.operations == {}
    first, second = BackboneState(), BackboneState()
    first.sessions["alice"] = {}
    assert second.sessions == {}


@pytest.mark.parametrize(
    "bad_path",
    [
        "../outside",
        "src/../outside",
        "/tmp/outside",
        "C:/outside",
        "src\\code.py",
        ".",
        "./",
        "",
        "src/\x00bad",
        "src/\nbad",
    ],
)
@pytest.mark.parametrize(
    "factory,field",
    [(intent, "affected_paths"), (task, "forbidden_paths"), (artifact, "changed_paths")],
)
def test_repository_paths_reject_unsafe_inputs(factory, field, bad_path):
    with pytest.raises(ValidationError):
        factory(**{field: [bad_path]})


def test_paths_normalize_and_deduplicate_without_removing_directory_scope():
    value = intent(affected_paths=["./src//auth.py", "src/auth.py", "./src/services//"])
    assert value.affected_paths == ["src/auth.py", "src/services/"]
    assert normalize_path("docs/./guide.md") == "docs/guide.md"


@pytest.mark.parametrize("field,value", [("member_id", "bob"), ("intent_id", "intent-other")])
def test_task_rejects_another_members_or_intents_artifact(field, value):
    data = artifact().model_dump()
    data[field] = value
    with pytest.raises(ValidationError, match="belong"):
        task(artifact=Artifact.model_validate(data))


def test_state_rejects_misindexed_entities_and_unknown_schema():
    with pytest.raises(ValidationError, match="must match"):
        BackboneState(intents={"intent-wrong": intent(id="intent-right")})
    with pytest.raises(ValidationError):
        BackboneState(schema_version=2)


def test_conflict_requires_distinct_parties():
    with pytest.raises(ValidationError, match="distinct"):
        Conflict(
            conflict_type="intent_overlap",
            parties=["intent-1", "intent-1"],
            severity="advisory",
            rule="symbol_scope_overlap",
        )


@pytest.mark.parametrize("current,target", list(product(IntentStatus, repeat=2)))
def test_intent_lifecycle(current, target):
    legal = {
        ("draft", "accepted"),
        ("draft", "rejected"),
        ("accepted", "in_progress"),
        ("accepted", "rejected"),
        ("accepted", "superseded"),
        ("in_progress", "completed"),
        ("in_progress", "superseded"),
        ("in_progress", "accepted"),
    }
    original = intent(status=current)
    if (current.value, target.value) in legal:
        updated = transition_intent(original, target.value)
        assert updated.status == target
        assert updated.id == original.id
        assert updated.created_at == original.created_at
    else:
        with pytest.raises(ValueError, match="transition"):
            transition_intent(original, target)
    assert original.status == current


@pytest.mark.parametrize("current,target", list(product(DecisionStatus, repeat=2)))
def test_decision_lifecycle(current, target):
    legal = {("proposed", "accepted"), ("accepted", "superseded"), ("accepted", "reverted")}
    original = decision(status=current)
    if (current.value, target.value) in legal:
        assert transition_decision(original, target.value).status == target
    else:
        with pytest.raises(ValueError, match="transition"):
            transition_decision(original, target)
    assert original.status == current


@pytest.mark.parametrize("current,target", list(product(TaskStatus, repeat=2)))
def test_task_lifecycle(current, target):
    legal = {
        ("dispatched", "in_progress"),
        ("in_progress", "submitted"),
        ("submitted", "in_progress"),
        ("submitted", "merged"),
        ("dispatched", "cancelled"),
        ("in_progress", "cancelled"),
        ("submitted", "cancelled"),
    }
    original = task(status=current)
    if (current.value, target.value) in legal:
        assert transition_task(original, target.value).status == target
    else:
        with pytest.raises(ValueError, match="transition"):
            transition_task(original, target)
    assert original.status == current
