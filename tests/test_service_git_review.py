"""Regressions for integrity boundaries found during independent review."""

import pytest

from backbone_conductor.models import Artifact, BackboneState, Intent, Task
from backbone_conductor.service import Conductor


@pytest.mark.parametrize("resolution", ["automatic", "human"])
def test_reappearing_artifact_collision_requires_explicit_arbitration(resolution):
    first = Intent(author="alice", problem="First task", proposed_outcome="First outcome")
    second = Intent(author="bob", problem="Second task", proposed_outcome="Second outcome")
    tasks = []
    for intent in (first, second):
        tasks.append(
            Task(
                member_id=intent.author,
                intent_id=intent.id,
                artifact=Artifact(
                    member_id=intent.author,
                    intent_id=intent.id,
                    branch=f"feature/{intent.author}",
                    summary="Implementation",
                    changed_paths=["shared.py"],
                ),
            )
        )
    state = BackboneState(
        intents={item.id: item for item in (first, second)},
        tasks={item.id: item for item in tasks},
    )
    findings = Conductor._refresh(state)
    assert len(findings) == 1
    original = findings[0]
    assert not original.resolved

    if resolution == "human":
        original.resolved = True
        original.resolution = {"action": "coordinate", "author": "owner", "rationale": "Agreed"}
    tasks[1].artifact.changed_paths = ["different.py"]
    assert not Conductor._refresh(state)
    assert state.conflicts[original.id].resolved

    tasks[1].artifact.changed_paths = ["shared.py"]
    repeated = Conductor._refresh(state)
    assert len(repeated) == 1
    assert repeated[0].id == original.id
    assert repeated[0].resolved is (resolution == "human")
    assert bool(Conductor._blockers(state, second.id, tasks[1].id)) is (resolution == "automatic")
