"""Validated, runtime-independent Backbone protocol objects and lifecycles."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[
    str,
    StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$", min_length=1, max_length=200),
]
Operation = Literal["remove", "replace", "extend", "modify", "add"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4()}"


def normalize_path(value: str) -> str:
    """Normalize a repository-relative path without consulting the filesystem.

    A trailing slash denotes a directory scope. Parent traversal is rejected,
    even when it could be simplified to a location inside the repository.
    """
    value = value.strip()
    if not value or value.startswith("/") or "\\" in value:
        raise ValueError("paths must be nonempty, repository-relative POSIX paths")
    if re.match(r"^[A-Za-z]:", value) or any(ord(char) < 32 for char in value):
        raise ValueError("paths cannot contain a drive prefix or control characters")
    parts = value.split("/")
    if ".." in parts:
        raise ValueError("parent traversal is not allowed in paths")
    normalized = "/".join(part for part in parts if part not in {"", "."})
    if not normalized:
        raise ValueError("paths must identify a file or directory below the repository root")
    return normalized + ("/" if value.endswith("/") else "")


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    @field_validator("affected_paths", "changed_paths", "forbidden_paths", check_fields=False)
    @classmethod
    def validate_paths(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(normalize_path(value) for value in values))


class IntentStatus(StrEnum):
    DRAFT = "draft"
    ACCEPTED = "accepted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    SUPERSEDED = "superseded"
    REJECTED = "rejected"


class DecisionStatus(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    SUPERSEDED = "superseded"
    REVERTED = "reverted"


class TaskStatus(StrEnum):
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    SUBMITTED = "submitted"
    MERGED = "merged"
    CANCELLED = "cancelled"


class ConflictType(StrEnum):
    INTENT_OVERLAP = "intent_overlap"
    DECISION_CONFLICT = "decision_conflict"
    SEMANTIC_MERGE = "semantic_merge"
    RESOURCE_CONTENTION = "resource_contention"


class Severity(StrEnum):
    ADVISORY = "advisory"
    BLOCKING = "blocking"
    CRITICAL = "critical"


class IntentReview(ProtocolModel):
    reviewer: Text
    outcome: Literal["accepted", "rejected"]
    rationale: Text
    reviewed_version: Text
    created_at: AwareDatetime = Field(default_factory=utc_now)


class Intent(ProtocolModel):
    id: Identifier = Field(default_factory=lambda: new_id("intent"))
    author: Text
    problem: Text
    proposed_outcome: Text
    affected_symbols: list[Text] = Field(default_factory=list)
    constraints: list[Text] = Field(default_factory=list)
    status: IntentStatus = IntentStatus.DRAFT
    parent_intent: Identifier | None = None
    supersedes: Identifier | None = None
    change_reason: Text | None = None
    reviews: list[IntentReview] = Field(default_factory=list)
    created_at: AwareDatetime = Field(default_factory=utc_now)
    artifacts: list[Identifier] = Field(default_factory=list)
    operations: dict[Text, Operation] = Field(default_factory=dict)
    affected_paths: list[str] = Field(default_factory=list)


class DecisionReversion(ProtocolModel):
    author: Text
    rationale: Text
    reviewed_version: Text
    created_at: AwareDatetime = Field(default_factory=utc_now)


class Decision(ProtocolModel):
    id: Identifier = Field(default_factory=lambda: new_id("decision"))
    author: Text
    decision_type: Text
    summary: Text
    rationale: Text
    supersedes: Identifier | None = None
    related_intents: list[Identifier] = Field(default_factory=list)
    status: DecisionStatus = DecisionStatus.PROPOSED
    affected_symbols: list[Text] = Field(default_factory=list)
    removes_symbols: list[Text] = Field(default_factory=list)
    depends_on: list[Text] = Field(default_factory=list)
    reversion: DecisionReversion | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_reversion(self) -> Decision:
        if self.reversion is not None and self.status != DecisionStatus.REVERTED:
            raise ValueError("Reversion evidence requires reverted decision status")
        return self


class Artifact(ProtocolModel):
    id: Identifier = Field(default_factory=lambda: new_id("artifact"))
    member_id: Text
    intent_id: Identifier
    branch: Text
    base_ref: Text = "main"
    summary: Text
    changed_paths: list[str] = Field(default_factory=list)
    commit_sha: Text | None = None
    base_sha: Text | None = None
    checks: dict[str, Any] = Field(default_factory=dict)
    created_at: AwareDatetime = Field(default_factory=utc_now)


class ReviewAnchor(ProtocolModel):
    decision_id: Identifier
    reviewed_version: Text
    target_sha: Text


class Task(ProtocolModel):
    id: Identifier = Field(default_factory=lambda: new_id("task"))
    member_id: Text
    intent_id: Identifier
    spec: str = ""
    base_ref: Text = "main"
    base_sha: Text | None = None
    constraints: list[Text] = Field(default_factory=list)
    forbidden_paths: list[str] = Field(default_factory=list)
    status: TaskStatus = TaskStatus.DISPATCHED
    decisions_at_fork: list[Identifier] = Field(default_factory=list)
    backbone_version: str | None = None
    artifact: Artifact | None = None
    approval: ReviewAnchor | None = None
    cancelled_by: Text | None = None
    cancel_reason: Text | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def validate_artifact_owner(self) -> Task:
        if self.artifact is not None and (
            self.artifact.member_id != self.member_id or self.artifact.intent_id != self.intent_id
        ):
            raise ValueError("artifact must belong to the task's member and intent")
        if self.approval is not None and (
            self.status != TaskStatus.MERGED or self.artifact is None
        ):
            raise ValueError("approval anchor requires a merged task with an artifact")
        return self


class Conflict(ProtocolModel):
    id: Identifier = Field(default_factory=lambda: new_id("conflict"))
    conflict_type: ConflictType
    parties: list[Identifier] = Field(min_length=2)
    severity: Severity
    detection_method: Text = "deterministic"
    rule: Text
    evidence: dict[str, Any] = Field(default_factory=dict)
    resolved: bool = False
    resolution: dict[str, Any] | None = None
    arbitration_packet: dict[str, Any] | None = None
    created_at: AwareDatetime = Field(default_factory=utc_now)

    @field_validator("parties")
    @classmethod
    def unique_parties(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("conflict parties must be distinct")
        return values


class BackboneState(ProtocolModel):
    schema_version: Literal[1] = 1
    intents: dict[Identifier, Intent] = Field(default_factory=dict)
    decisions: dict[Identifier, Decision] = Field(default_factory=dict)
    conflicts: dict[Identifier, Conflict] = Field(default_factory=dict)
    tasks: dict[Identifier, Task] = Field(default_factory=dict)
    sessions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    version: str | None = None
    parent_version: str | None = None
    merged_parent_version: str | None = None

    @model_validator(mode="after")
    def validate_mapping_ids(self) -> BackboneState:
        for name in ("intents", "decisions", "conflicts", "tasks"):
            for key, value in getattr(self, name).items():
                if key != value.id:
                    raise ValueError(f"{name} key {key!r} must match object id {value.id!r}")
        return self


INTENT_TRANSITIONS = {
    IntentStatus.DRAFT: {IntentStatus.ACCEPTED, IntentStatus.REJECTED},
    IntentStatus.ACCEPTED: {
        IntentStatus.IN_PROGRESS,
        IntentStatus.REJECTED,
        IntentStatus.SUPERSEDED,
    },
    IntentStatus.IN_PROGRESS: {
        IntentStatus.ACCEPTED,
        IntentStatus.COMPLETED,
        IntentStatus.SUPERSEDED,
    },
    IntentStatus.COMPLETED: set(),
    IntentStatus.SUPERSEDED: set(),
    IntentStatus.REJECTED: set(),
}
DECISION_TRANSITIONS = {
    DecisionStatus.PROPOSED: {DecisionStatus.ACCEPTED},
    DecisionStatus.ACCEPTED: {DecisionStatus.SUPERSEDED, DecisionStatus.REVERTED},
    DecisionStatus.SUPERSEDED: set(),
    DecisionStatus.REVERTED: set(),
}
TASK_TRANSITIONS = {
    TaskStatus.DISPATCHED: {TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.IN_PROGRESS: {TaskStatus.SUBMITTED, TaskStatus.CANCELLED},
    TaskStatus.SUBMITTED: {TaskStatus.MERGED, TaskStatus.IN_PROGRESS, TaskStatus.CANCELLED},
    TaskStatus.MERGED: set(),
    TaskStatus.CANCELLED: set(),
}


def _transition[Stateful: (Intent, Decision, Task)](
    model: Stateful, status: str | Enum, enum: type[Enum], allowed: dict
) -> Stateful:
    target = enum(status)
    if target not in allowed[model.status]:
        raise ValueError(
            f"invalid {type(model).__name__} transition: {model.status.value} -> {target.value}"
        )
    return type(model).model_validate({**model.model_dump(), "status": target})


def transition_intent(intent: Intent, status: IntentStatus | str) -> Intent:
    return _transition(intent, status, IntentStatus, INTENT_TRANSITIONS)


def transition_decision(decision: Decision, status: DecisionStatus | str) -> Decision:
    return _transition(decision, status, DecisionStatus, DECISION_TRANSITIONS)


def transition_task(task: Task, status: TaskStatus | str) -> Task:
    return _transition(task, status, TaskStatus, TASK_TRANSITIONS)
