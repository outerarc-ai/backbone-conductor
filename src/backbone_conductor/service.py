"""Transactional application layer shared by CLI, HTTP and MCP."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from time import monotonic_ns
from typing import Any

from .conflicts import refresh_conflicts
from .models import (
    Artifact,
    BackboneState,
    Decision,
    DecisionReversion,
    DecisionStatus,
    Intent,
    IntentReview,
    IntentStatus,
    ReviewAnchor,
    Severity,
    Task,
    TaskStatus,
    transition_decision,
    transition_intent,
    transition_task,
)
from .storage import GitStore, StorageError

_TERMINAL_TASKS = {TaskStatus.MERGED, TaskStatus.CANCELLED}
_EDITABLE_INTENT_FIELDS = {
    "problem",
    "proposed_outcome",
    "affected_symbols",
    "constraints",
    "parent_intent",
    "operations",
    "affected_paths",
}


def _dump(value: Any) -> dict:
    return value.model_dump(mode="json")


def _actor(value: str) -> str:
    if not value.strip() or any(ord(c) < 32 for c in value):
        raise ValueError("Author/member must be non-empty and contain no control characters")
    return value.strip()


def _overlap(path: str, scope: str) -> bool:
    return path.rstrip("/") == scope.rstrip("/") or path.startswith(scope.rstrip("/") + "/")


class Conductor:
    def __init__(self, repo: str | Path, *, ledger_branch: str | None = None):
        self.code_store = GitStore(repo)
        if ledger_branch is None:
            self.store = self.code_store
        else:
            from .ledger import open_ledger

            self.store = open_ledger(self.code_store, ledger_branch)

    def initialize(self) -> dict:
        return _dump(self.store.init())

    def state(self) -> dict:
        return _dump(self.store.read())

    @staticmethod
    def _refresh(state: BackboneState) -> list:
        return refresh_conflicts(state)

    @staticmethod
    def _blockers(state: BackboneState, intent_id: str, task_id: str | None = None) -> list:
        relevant = {intent_id}
        if task_id:
            relevant.add(task_id)
        relevant.update(
            d.id
            for d in state.decisions.values()
            if not d.related_intents or intent_id in d.related_intents
        )
        return [
            c
            for c in state.conflicts.values()
            if not c.resolved
            and c.severity != Severity.ADVISORY
            and relevant.intersection(c.parties)
        ]

    @staticmethod
    def _decision_delta(state: BackboneState, task: Task) -> dict[str, list[str]]:
        accepted = {d.id for d in state.decisions.values() if d.status == DecisionStatus.ACCEPTED}
        at_fork = set(task.decisions_at_fork)
        return {
            "new_decisions": sorted(accepted - at_fork),
            "withdrawn_decisions": sorted(at_fork - accepted),
        }

    def create_intent(self, data: dict) -> dict:
        intent = Intent.model_validate(data)
        _actor(intent.author)
        if (
            intent.status != IntentStatus.DRAFT
            or intent.artifacts
            or intent.supersedes
            or intent.change_reason
            or intent.reviews
        ):
            raise ValueError("New intents must be draft with no artifacts; use lifecycle actions")

        def change(state: BackboneState):
            if intent.id in state.intents:
                raise ValueError(f"Intent already exists: {intent.id}")
            if intent.parent_intent == intent.id:
                raise ValueError("An intent cannot be its own parent")
            if intent.parent_intent and intent.parent_intent not in state.intents:
                raise KeyError(intent.parent_intent)
            state.intents[intent.id] = intent
            self._refresh(state)
            return _dump(intent)

        return self.store.mutate(change, f"backbone: intent {intent.id} created by {intent.author}")

    def transition_intent(self, intent_id: str, status: str) -> dict:
        target = IntentStatus(status)
        if target == IntentStatus.COMPLETED:
            raise ValueError("Complete an intent by recording a verified task merge")
        if target == IntentStatus.SUPERSEDED:
            raise ValueError("Supersede an intent by creating an audited replacement")

        def change(state: BackboneState):
            intent = state.intents[intent_id]
            active = [
                t
                for t in state.tasks.values()
                if t.intent_id == intent_id and t.status not in _TERMINAL_TASKS
            ]
            if active and target == IntentStatus.ACCEPTED:
                raise ValueError("Cannot return an intent to accepted with an active task")
            if active and target in (IntentStatus.SUPERSEDED, IntentStatus.REJECTED):
                raise ValueError("Cannot close an intent with an active task")
            updated = transition_intent(intent, target)
            state.intents[intent_id] = updated
            self._refresh(state)
            return _dump(updated)

        return self.store.mutate(change, f"backbone: intent {intent_id} {target.value}")

    def review_intent(
        self,
        intent_id: str,
        outcome: str,
        reviewer: str,
        rationale: str,
        expected_version: str,
    ) -> dict:
        """Record a version-bound human decision on a draft intent."""
        reviewer = _actor(reviewer)
        if outcome not in {"accepted", "rejected"}:
            raise ValueError("Intent review outcome must be accepted or rejected")
        if not rationale.strip():
            raise ValueError("Intent review requires a rationale")
        if not expected_version:
            raise ValueError("expected_version is required for safe review")

        def change(state: BackboneState):
            if state.version != expected_version:
                raise ValueError("Backbone changed; refresh the intent and retry")
            intent = state.intents[intent_id]
            if intent.status != IntentStatus.DRAFT:
                raise ValueError("Only draft intents can be reviewed")
            if intent.author == reviewer:
                raise PermissionError("Reviewers cannot approve or reject their own intent")
            updated = transition_intent(intent, IntentStatus(outcome))
            updated.reviews.append(
                IntentReview(
                    reviewer=reviewer,
                    outcome=outcome,
                    rationale=rationale,
                    reviewed_version=state.version,
                )
            )
            state.intents[intent_id] = updated
            self._refresh(state)
            return _dump(updated)

        return self.store.mutate(change, f"backbone: intent {intent_id} {outcome} by {reviewer}")

    def revise_intent(
        self,
        intent_id: str,
        patch: dict,
        author: str,
        expected_version: str,
    ) -> dict:
        """Revise a draft or undispatched intent; accepted work needs fresh approval."""
        author = _actor(author)
        if not isinstance(patch, dict) or not patch or set(patch) - _EDITABLE_INTENT_FIELDS:
            raise ValueError("Provide nonempty editable intent fields only")
        if not expected_version:
            raise ValueError("expected_version is required for safe revision")

        def change(state: BackboneState):
            if state.version != expected_version:
                raise ValueError("Backbone changed; refresh the intent and retry")
            current = state.intents[intent_id]
            if current.status not in {IntentStatus.DRAFT, IntentStatus.ACCEPTED}:
                raise ValueError("Only draft or undispatched accepted intents can be revised")
            if any(t.intent_id == intent_id for t in state.tasks.values()):
                raise ValueError(
                    "An intent with task history cannot be revised; create a child intent"
                )
            revised = Intent.model_validate(
                {
                    **current.model_dump(mode="json"),
                    **patch,
                    "status": IntentStatus.DRAFT,
                }
            )
            if revised.parent_intent:
                if revised.parent_intent == intent_id or revised.parent_intent not in state.intents:
                    raise ValueError("parent_intent must reference another existing intent")
                ancestor = revised.parent_intent
                visited = {intent_id}
                while ancestor:
                    if ancestor in visited:
                        raise ValueError("parent_intent cannot create a cycle")
                    visited.add(ancestor)
                    ancestor = state.intents[ancestor].parent_intent
            state.intents[intent_id] = revised
            self._refresh(state)
            return _dump(revised)

        return self.store.mutate(change, f"backbone: intent {intent_id} revised by {author}")

    def replace_intent(
        self,
        intent_id: str,
        patch: dict,
        author: str,
        reason: str,
        expected_version: str,
    ) -> dict:
        """Atomically close accepted work and draft a linked successor for fresh approval."""
        author = _actor(author)
        if not isinstance(patch, dict) or not patch or set(patch) - _EDITABLE_INTENT_FIELDS:
            raise ValueError("Provide nonempty editable intent fields only")
        if not reason.strip():
            raise ValueError("A replacement reason is required")
        if not expected_version:
            raise ValueError("expected_version is required for safe replacement")

        def change(state: BackboneState):
            if state.version != expected_version:
                raise ValueError("Backbone changed; refresh the intent and retry")
            current = state.intents[intent_id]
            if current.status != IntentStatus.ACCEPTED:
                raise ValueError("Only accepted intents can be replaced; cancel active tasks first")
            if any(
                task.intent_id == intent_id and task.status not in _TERMINAL_TASKS
                for task in state.tasks.values()
            ):
                raise ValueError("Cancel active tasks before replacing their intent")
            replacement = Intent.model_validate(
                {
                    **{key: getattr(current, key) for key in _EDITABLE_INTENT_FIELDS},
                    **patch,
                    "author": author,
                    "supersedes": intent_id,
                    "change_reason": reason.strip(),
                }
            )
            if replacement.parent_intent:
                if (
                    replacement.parent_intent == intent_id
                    or replacement.parent_intent not in state.intents
                ):
                    raise ValueError("parent_intent must reference another existing intent")
                ancestor = replacement.parent_intent
                visited = {replacement.id}
                while ancestor:
                    if ancestor in visited:
                        raise ValueError("parent_intent cannot create a cycle")
                    visited.add(ancestor)
                    ancestor = state.intents[ancestor].parent_intent
            state.intents[intent_id] = transition_intent(current, IntentStatus.SUPERSEDED)
            state.intents[replacement.id] = replacement
            self._refresh(state)
            return {"previous": _dump(state.intents[intent_id]), "replacement": _dump(replacement)}

        return self.store.mutate(change, f"backbone: intent {intent_id} replaced by {author}")

    def log_decision(self, data: dict) -> dict:
        decision = Decision.model_validate(data)
        _actor(decision.author)
        if decision.status != DecisionStatus.PROPOSED or decision.reversion is not None:
            raise ValueError("New decisions must be proposed without reversion evidence")

        def change(state: BackboneState):
            if decision.id in state.decisions:
                raise ValueError(f"Decision already exists: {decision.id}")
            for intent_id in decision.related_intents:
                if intent_id not in state.intents:
                    raise KeyError(intent_id)
            if decision.supersedes:
                previous = state.decisions[decision.supersedes]
                if previous.status != DecisionStatus.ACCEPTED:
                    raise ValueError("Only accepted decisions may be superseded")
            state.decisions[decision.id] = decision
            self._refresh(state)
            return _dump(decision)

        return self.store.mutate(
            change, f"backbone: decision {decision.id} proposed by {decision.author}"
        )

    def transition_decision(self, decision_id: str, status: str) -> dict:
        target = DecisionStatus(status)
        if target == DecisionStatus.REVERTED:
            raise ValueError("Revert decisions with an audited author, rationale and version")

        def change(state: BackboneState):
            updated = transition_decision(state.decisions[decision_id], target)
            if target == DecisionStatus.ACCEPTED and updated.supersedes:
                previous = state.decisions[updated.supersedes]
                state.decisions[previous.id] = transition_decision(
                    previous, DecisionStatus.SUPERSEDED
                )
            state.decisions[decision_id] = updated
            self._refresh(state)
            return _dump(updated)

        return self.store.mutate(change, f"backbone: decision {decision_id} {target.value}")

    def revert_decision(
        self, decision_id: str, author: str, rationale: str, expected_version: str
    ) -> dict:
        """Withdraw an accepted decision in a new, version-bound audit commit."""
        author = _actor(author)
        if not rationale.strip():
            raise ValueError("Decision reversion requires a rationale")
        if not expected_version:
            raise ValueError("Decision reversion requires an observed Backbone version")

        def change(state: BackboneState):
            if state.version != expected_version:
                raise ValueError("Backbone changed; refresh the decision and retry")
            current = state.decisions[decision_id]
            if current.status != DecisionStatus.ACCEPTED:
                raise ValueError("Only accepted decisions can be reverted")
            updated = transition_decision(current, DecisionStatus.REVERTED)
            updated.reversion = DecisionReversion(
                author=author,
                rationale=rationale,
                reviewed_version=state.version,
            )
            state.decisions[decision_id] = updated
            self._refresh(state)
            return _dump(updated)

        return self.store.mutate(change, f"backbone: decision {decision_id} reverted by {author}")

    def dispatch_task(
        self,
        intent_id: str,
        member_id: str,
        spec: str = "",
        forbidden_paths: list[str] | None = None,
    ) -> dict:
        member_id = _actor(member_id)

        def change(state: BackboneState):
            intent = state.intents[intent_id]
            if intent.status != IntentStatus.ACCEPTED:
                raise ValueError("Dispatch requires an accepted intent")
            if any(
                t.intent_id == intent_id and t.status not in _TERMINAL_TASKS
                for t in state.tasks.values()
            ):
                raise ValueError("Intent already has an active task")
            task = Task(
                intent_id=intent_id,
                member_id=member_id,
                spec=spec or intent.proposed_outcome,
                constraints=intent.constraints,
                forbidden_paths=forbidden_paths or [],
                decisions_at_fork=sorted(
                    d.id for d in state.decisions.values() if d.status == DecisionStatus.ACCEPTED
                ),
                backbone_version=state.version,
                base_ref=self._git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip(),
                base_sha=self._commit("HEAD"),
            )
            state.tasks[task.id] = task
            state.intents[intent_id] = transition_intent(intent, IntentStatus.IN_PROGRESS)
            self._refresh(state)
            return {
                **_dump(task),
                "conflicts": [_dump(c) for c in self._blockers(state, intent_id, task.id)],
            }

        return self.store.mutate(
            change, f"backbone: task for {intent_id} dispatched to {member_id}"
        )

    def get_my_task(self, member_id: str) -> dict:
        state = self.store.read()
        member_id = _actor(member_id)
        tasks = []
        for task in state.tasks.values():
            if task.member_id != member_id or task.status in _TERMINAL_TASKS:
                continue
            tasks.append(
                {
                    **_dump(task),
                    "intent": _dump(state.intents[task.intent_id]),
                    "decisions": [
                        _dump(d)
                        for d in state.decisions.values()
                        if d.status == DecisionStatus.ACCEPTED
                    ],
                    "conflicts": [_dump(c) for c in self._blockers(state, task.intent_id, task.id)],
                }
            )
        return {"member_id": member_id, "version": state.version, "tasks": tasks}

    def start_task(self, task_id: str, member_id: str) -> dict:
        def change(state: BackboneState):
            task = state.tasks[task_id]
            if task.member_id != member_id:
                raise PermissionError("Task belongs to another member")
            task = transition_task(task, TaskStatus.IN_PROGRESS)
            state.tasks[task_id] = task
            return _dump(task)

        return self.store.mutate(change, f"backbone: task {task_id} started by {_actor(member_id)}")

    def cancel_task(self, task_id: str, author: str, reason: str) -> dict:
        """Close active work and return its intent to the accepted dispatch queue."""
        author = _actor(author)
        if not reason.strip():
            raise ValueError("A cancellation reason is required")

        def change(state: BackboneState):
            task = state.tasks[task_id]
            if task.status in _TERMINAL_TASKS:
                raise ValueError("A completed or cancelled task cannot be cancelled")
            if task.artifact and self._artifact_has_integrated_code(task.artifact, task.base_ref):
                raise ValueError("Revert integrated artifact code before cancelling the task")
            intent = state.intents[task.intent_id]
            if intent.status != IntentStatus.IN_PROGRESS:
                raise ValueError("Active task has an inconsistent intent status")
            task = transition_task(task, TaskStatus.CANCELLED)
            task.cancelled_by = author
            task.cancel_reason = reason.strip()
            state.tasks[task_id] = task
            state.intents[intent.id] = transition_intent(intent, IntentStatus.ACCEPTED)
            self._refresh(state)
            return {"task": _dump(task), "intent": _dump(state.intents[intent.id])}

        return self.store.mutate(change, f"backbone: task {task_id} cancelled by {author}")

    def rebase_task(self, task_id: str, member_id: str, expected_version: str) -> dict:
        """Refresh task context against current decisions and target branch.

        This records a semantic rebase; it does not execute git rebase on source code.
        Submitted artifacts lose approval and must be checked again.
        """
        member_id = _actor(member_id)
        if not expected_version:
            raise ValueError("expected_version is required for safe context refresh")

        def change(state: BackboneState):
            if state.version != expected_version:
                raise ValueError("Backbone changed; refresh the task and retry")
            task = state.tasks[task_id]
            if task.member_id != member_id:
                raise PermissionError("Task belongs to another member")
            if task.status in _TERMINAL_TASKS:
                raise ValueError("A completed or cancelled task cannot be rebased")
            if task.artifact and self._artifact_has_integrated_code(task.artifact, task.base_ref):
                raise ValueError("Artifact is already integrated; record merge review or revert it")
            current_branch = self._git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
            if current_branch != task.base_ref:
                raise ValueError(f"Refresh task context on its target branch: {task.base_ref}")
            new_base = self._commit(task.base_ref)
            if (
                task.base_sha
                and self._git("merge-base", "--is-ancestor", task.base_sha, new_base).returncode
            ):
                raise ValueError("Target branch history was rewritten; inspect it before rebasing")
            task.base_sha = new_base
            task.backbone_version = state.version
            task.decisions_at_fork = sorted(
                d.id for d in state.decisions.values() if d.status == DecisionStatus.ACCEPTED
            )
            task.constraints = list(state.intents[task.intent_id].constraints)
            had_artifact = task.artifact is not None
            if task.status == TaskStatus.SUBMITTED:
                task = transition_task(task, TaskStatus.IN_PROGRESS)
            if had_artifact:
                task.artifact = None
            state.tasks[task_id] = task
            self._refresh(state)
            return {
                "task": _dump(task),
                "requires_resubmission": had_artifact,
                "conflicts": [_dump(c) for c in self._blockers(state, task.intent_id, task.id)],
            }

        return self.store.mutate(change, f"backbone: task {task_id} context rebased by {member_id}")

    def _git(self, *args: str) -> subprocess.CompletedProcess:
        return self.code_store._git(*args, check=False)

    def _commit(self, ref: str) -> str:
        if not ref or ref.startswith("-") or any(ord(c) < 32 for c in ref):
            raise ValueError("Invalid Git reference")
        result = self._git("rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        if result.returncode:
            raise ValueError(f"Unknown commit reference: {ref}")
        return result.stdout.strip()

    def _integration_paths(
        self, artifact: Artifact, target_sha: str
    ) -> tuple[list[str], list[str]]:
        """Compare declared artifact paths with the original base and target tree."""
        if not artifact.base_sha or not artifact.commit_sha or not artifact.changed_paths:
            raise ValueError("Submitted artifact lacks pinned Git evidence")

        def changed_from(source_sha: str) -> list[str]:
            result = self._git(
                "--literal-pathspecs",
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                source_sha,
                target_sha,
                "--",
                *artifact.changed_paths,
            )
            if result.returncode:
                raise ValueError("Could not compare artifact paths with target tree")
            return sorted(path for path in result.stdout.split("\x00") if path)

        return changed_from(artifact.base_sha), changed_from(artifact.commit_sha)

    def _artifact_has_integrated_code(self, artifact: Artifact, base_ref: str) -> bool:
        """A retained ancestor alone does not imply the artifact changed the target tree."""
        if (
            not artifact.commit_sha
            or self._git("merge-base", "--is-ancestor", artifact.commit_sha, base_ref).returncode
        ):
            return False
        net_paths, _ = self._integration_paths(artifact, self._commit(base_ref))
        return bool(net_paths)

    def fetch_artifact_branch(
        self, task_id: str, member_id: str, branch: str, expected_sha: str, remote: str = "origin"
    ) -> dict:
        """Fetch the assigned member's exact pushed branch into a remote-tracking ref."""
        member_id = _actor(member_id)
        task = self.store.read().tasks[task_id]
        if task.member_id != member_id:
            raise PermissionError("Task belongs to another member")
        if task.status != TaskStatus.IN_PROGRESS:
            raise ValueError("Start the assigned task before fetching its code branch")
        if branch == task.base_ref:
            raise ValueError("Artifact must name a feature branch separate from its base")
        return self.code_store.fetch_code_branch(remote, branch, expected_sha)

    def submit_artifact(self, member_id: str, artifact: dict) -> dict:
        member_id = _actor(member_id)
        incoming = dict(artifact)
        if incoming.get("member_id", member_id) != member_id:
            raise PermissionError("Artifact member does not match caller")
        expected_sha = incoming.get("commit_sha")
        incoming["member_id"] = member_id
        # Evidence comes from Git, never from the submitting agent's assertions.
        incoming.update(commit_sha=None, base_sha=None, checks={}, changed_paths=[])
        item = Artifact.model_validate(incoming)

        def change(state: BackboneState):
            matching = [
                t
                for t in state.tasks.values()
                if t.intent_id == item.intent_id
                and t.member_id == member_id
                and t.status not in _TERMINAL_TASKS
            ]
            if len(matching) != 1:
                raise ValueError("Artifact must match exactly one assigned active task")
            task = matching[0]
            if task.status != TaskStatus.IN_PROGRESS:
                raise ValueError("Start the task before submitting (restart to resubmit)")
            if item.base_ref != task.base_ref:
                raise ValueError(
                    f"Artifact base_ref must match the assigned target: {task.base_ref}"
                )
            if item.branch == item.base_ref:
                raise ValueError("Artifact must name a feature branch separate from its base")
            item.commit_sha = self._commit(item.branch)
            if expected_sha is not None and expected_sha != item.commit_sha:
                raise ValueError("Artifact commit differs from the expected Git commit")
            item.base_sha = self._commit(item.base_ref)
            diff = self.code_store.check_diff(item.base_sha, item.commit_sha)
            paths = diff.get("changed_paths", [])
            metadata_changes = [p for p in paths if _overlap(p, ".backbone")]
            # Metadata updates never count as implementation work.
            item.changed_paths = [p for p in paths if not _overlap(p, ".backbone")]
            forbidden = [
                p
                for p in item.changed_paths
                if any(_overlap(p, scope) for scope in task.forbidden_paths)
            ]
            intent = state.intents[item.intent_id]
            outside = [
                p
                for p in item.changed_paths
                if intent.affected_paths
                and not any(_overlap(p, scope) for scope in intent.affected_paths)
            ]
            task.artifact = item
            conflicts = self._refresh(state)
            blocking = self._blockers(state, item.intent_id, task.id)
            delta = self._decision_delta(state, task)
            context_stale = bool(delta["new_decisions"] or delta["withdrawn_decisions"])
            code_ok = bool(diff["ok"]) and bool(item.changed_paths) and not metadata_changes
            checks = {
                "code": {
                    "status": "passed" if code_ok else "failed",
                    "detail": diff.get("detail", ""),
                    "metadata_changes": metadata_changes,
                    "empty_diff": not item.changed_paths,
                },
                "scope": {
                    "status": "failed" if forbidden or outside else "passed",
                    "forbidden_paths": forbidden,
                    "undeclared_paths": outside,
                },
                "intent": {
                    "status": "requires_human_review",
                    "detail": "Semantic review is not configured; no automatic intent approval.",
                },
                "decisions": {
                    "status": "failed" if blocking else "passed",
                    "conflict_ids": [c.id for c in blocking],
                    "detail": "Deterministic rules only; human semantic review required.",
                },
                "context": {
                    "status": "failed" if context_stale else "passed",
                    **delta,
                    "detail": "Refresh task context and resubmit when decisions change."
                    if context_stale
                    else "Task decision snapshot is current.",
                },
            }
            item.checks = checks
            ready = code_ok and not forbidden and not outside and not blocking and not context_stale
            if ready:
                state.tasks[task.id] = transition_task(task, TaskStatus.SUBMITTED)
                if item.id not in intent.artifacts:
                    intent.artifacts.append(item.id)
            return {
                "task_id": task.id,
                "artifact": _dump(item),
                "checks": checks,
                "accepted": ready,
                "requires_human_review": True,
                "conflicts": [
                    _dump(c)
                    for c in conflicts
                    if task.id in c.parties or item.intent_id in c.parties
                ],
            }

        return self.store.mutate(change, f"backbone: artifact {item.id} checked by {member_id}")

    def check_backbone_sync(
        self,
        member_id: str | None = None,
        since_version: str | None = None,
    ) -> dict:
        state = self.store.read()
        tasks = [
            t
            for t in state.tasks.values()
            if t.status not in _TERMINAL_TASKS and (member_id is None or t.member_id == member_id)
        ]
        return {
            "version": state.version,
            "changed": state.version != since_version,
            "updates": [
                {
                    "task_id": t.id,
                    **self._decision_delta(state, t),
                    "conflicts": [_dump(c) for c in self._blockers(state, t.intent_id, t.id)],
                }
                for t in tasks
            ],
        }

    def detect_conflicts(self) -> dict:
        def change(state: BackboneState):
            conflicts = self._refresh(state)
            return {
                "conflicts": [_dump(c) for c in conflicts],
                "blocking": sum(
                    not c.resolved and c.severity != Severity.ADVISORY for c in conflicts
                ),
            }

        return self.store.mutate(change, "backbone: conflicts checked")

    def advise_intent_conflict(
        self,
        left_id: str,
        right_id: str,
        dsh_home: str,
        model: str,
        provider: str = "deepseek-official",
    ) -> dict:
        """Return version-bound model advice without changing the authoritative ledger."""
        from .runtime import DSHReviewer

        if left_id == right_id:
            raise ValueError("Conflict advice requires two different intents")
        home = Path(dsh_home).expanduser().absolute().resolve()
        if home.is_relative_to(self.code_store.root) or home.is_relative_to(self.store.root):
            raise ValueError("Conflict advice DSH home must be outside the repository")
        snapshot = self.store.read()
        pair = [snapshot.intents[left_id], snapshot.intents[right_id]]
        terminal = {IntentStatus.COMPLETED, IntentStatus.SUPERSEDED, IntentStatus.REJECTED}
        if any(intent.status in terminal for intent in pair):
            raise ValueError("Conflict advice requires two live intents")
        conflicts = [
            {
                "id": conflict.id,
                "rule": conflict.rule,
                "severity": conflict.severity.value,
                "evidence": conflict.evidence,
            }
            for conflict in self._refresh(snapshot)
            if not conflict.resolved and set(conflict.parties) == {left_id, right_id}
        ]
        relevant = {left_id, right_id}
        context = {
            "version": snapshot.version,
            "intents": [
                intent.model_dump(
                    mode="json",
                    include={
                        "id",
                        "author",
                        "status",
                        "problem",
                        "proposed_outcome",
                        "constraints",
                        "affected_symbols",
                        "operations",
                        "affected_paths",
                    },
                )
                for intent in pair
            ],
            "accepted_decisions": [
                decision.model_dump(
                    mode="json",
                    include={
                        "id",
                        "summary",
                        "rationale",
                        "related_intents",
                        "depends_on",
                        "removes_symbols",
                    },
                )
                for decision in snapshot.decisions.values()
                if decision.status == DecisionStatus.ACCEPTED
                and (
                    not decision.related_intents or relevant.intersection(decision.related_intents)
                )
            ],
            "deterministic_conflicts": conflicts,
        }
        context_bytes = json.dumps(context, sort_keys=True, ensure_ascii=False).encode()
        if len(context_bytes) > 1_000_000:
            raise ValueError("Conflict advice context exceeds 1 MB; narrow the intent pair")
        context_sha256 = hashlib.sha256(context_bytes).hexdigest()
        advice = DSHReviewer(dsh_home, model, provider).advise_conflict(context)
        current_version = self.store.read().version
        return {
            "advisory": True,
            "human_review_required": True,
            "pair": [left_id, right_id],
            "observed_version": snapshot.version,
            "current_version": current_version,
            "stale": current_version != snapshot.version,
            "context_sha256": context_sha256,
            "deterministic_conflicts": conflicts,
            "blocking_conflict_ids": [
                conflict["id"]
                for conflict in conflicts
                if conflict["severity"] != Severity.ADVISORY.value
            ],
            "model_advice": advice,
        }

    def inspect_task(self, task_id: str, *, full_patch: bool = False) -> dict:
        """Build a read-only packet pinned to the artifact's checked Git commits."""
        current_state = self.store.read()
        current_task = current_state.tasks[task_id]
        approval = None
        if current_task.status == TaskStatus.MERGED:
            anchor = current_task.approval
            if anchor is None:
                raise ValueError(
                    "Merged task predates structured approval; inspect Git audit history"
                )
            try:
                state = self.store.read_version(anchor.reviewed_version)
            except StorageError as exc:
                raise ValueError("Could not read the approved Backbone version") from exc
            task = state.tasks.get(task_id)
            if (
                task is None
                or task.status != TaskStatus.SUBMITTED
                or task.artifact != current_task.artifact
                or task.base_ref != current_task.base_ref
            ):
                raise ValueError("Approval anchor does not match the submitted task")
            decision = current_state.decisions.get(anchor.decision_id)
            if (
                decision is None
                or decision.decision_type != "human_review"
                or task.intent_id not in decision.related_intents
            ):
                raise ValueError("Approval review decision is missing or inconsistent")
            target_sha = self._commit(anchor.target_sha)
            if target_sha != anchor.target_sha:
                raise ValueError("Approval target commit does not match its anchor")
            try:
                current_target_sha = self._commit(task.base_ref)
            except ValueError:
                current_target_sha = None
            approval = {
                "decision": _dump(decision),
                "reviewed_version": anchor.reviewed_version,
                "target_sha": anchor.target_sha,
            }
        else:
            state = current_state
            task = current_task
            target_sha = (
                self._commit(task.base_ref) if task.status == TaskStatus.SUBMITTED else None
            )
            current_target_sha = target_sha
        artifact = task.artifact
        if (
            task.status != TaskStatus.SUBMITTED
            or artifact is None
            or artifact.commit_sha is None
            or artifact.base_sha is None
        ):
            raise ValueError("Only a successfully submitted task has a code review packet")
        revision = f"{artifact.base_sha}...{artifact.commit_sha}"
        preview_limit = 1_000_000 if full_patch else 131_072
        try:
            diff = self.code_store._git_output_digest(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--binary",
                revision,
                "--",
                preview_limit=preview_limit,
                max_bytes=1_000_000,
            )
        except StorageError as exc:
            raise ValueError("Could not read the submitted Git diff") from exc
        if not diff.complete:
            raise ValueError("Artifact diff exceeds the 1 MB review limit; split the task")
        preview = diff.preview.decode(
            "utf-8", errors="ignore" if diff.size > preview_limit else "strict"
        )
        try:
            branch_sha = self._commit(artifact.branch)
        except ValueError:
            branch_sha = None
        integrated = (
            self._git("merge-base", "--is-ancestor", artifact.commit_sha, target_sha).returncode
            == 0
        )
        net_paths, divergent_paths = (
            self._integration_paths(artifact, target_sha) if integrated else ([], [])
        )
        target_diff = None
        if integrated:
            try:
                target = self.code_store._git_output_digest(
                    "--literal-pathspecs",
                    "diff",
                    "--no-ext-diff",
                    "--no-textconv",
                    "--no-color",
                    "--no-renames",
                    "--binary",
                    artifact.base_sha,
                    target_sha,
                    "--",
                    *artifact.changed_paths,
                    preview_limit=preview_limit,
                    max_bytes=1_000_000 if full_patch else None,
                )
            except StorageError as exc:
                raise ValueError("Could not read the integrated target diff") from exc
            if not target.complete:
                raise ValueError(
                    "Integrated target diff exceeds the 1 MB review limit; inspect in a Git checkout"
                )
            target_preview = target.preview.decode(
                "utf-8", errors="ignore" if target.size > preview_limit else "strict"
            )
            target_diff = {
                "base_sha": artifact.base_sha,
                "target_sha": target_sha,
                "changed_paths": artifact.changed_paths,
                "patch": target_preview,
                "truncated": target.size > preview_limit,
                "sha256": target.sha256,
            }
        blockers = self._blockers(state, task.intent_id, task_id)
        return {
            "version": state.version,
            "current_version": current_state.version,
            "inspection_kind": "approval" if approval else "current",
            "current_task_status": current_task.status.value,
            "approval": approval,
            "task": _dump(task),
            "intent": _dump(state.intents[task.intent_id]),
            "decision_ids_at_fork": task.decisions_at_fork,
            "accepted_decisions": [
                _dump(decision)
                for decision in state.decisions.values()
                if decision.status == DecisionStatus.ACCEPTED
            ],
            "decision_delta": self._decision_delta(state, task),
            "blocking_conflicts": [_dump(conflict) for conflict in blockers],
            "git": {
                "base_sha": artifact.base_sha,
                "artifact_sha": artifact.commit_sha,
                "branch_sha": branch_sha,
                "target_sha": target_sha,
                "current_target_sha": current_target_sha,
                "branch_unchanged": branch_sha == artifact.commit_sha,
                "integrated_into_target": integrated,
                "net_changed_paths": net_paths,
                "divergent_paths": divergent_paths,
            },
            "diff": {
                "patch": preview,
                "truncated": diff.size > preview_limit,
                "sha256": diff.sha256,
                "changed_paths": artifact.changed_paths,
            },
            "target_diff": target_diff,
            "requires_human_review": True,
        }

    def resolve_conflict(
        self, conflict_id: str, author: str, action: str, rationale: str, expected_version: str
    ) -> dict:
        author = _actor(author)
        if action not in {"accept_existing", "override_existing", "coordinate", "accept_risk"}:
            raise ValueError("Unknown arbitration action")
        if not rationale.strip():
            raise ValueError("A human arbitration rationale is required")
        if not expected_version:
            raise ValueError("Conflict resolution requires an observed Backbone version")

        def change(state: BackboneState):
            if state.version != expected_version:
                raise ValueError("Backbone changed; refresh the conflict and retry")
            conflict = state.conflicts[conflict_id]
            if conflict.resolved:
                raise ValueError("Conflict is already resolved")
            related = {p for p in conflict.parties if p in state.intents}
            related.update(state.tasks[p].intent_id for p in conflict.parties if p in state.tasks)
            decision = Decision(
                author=author,
                decision_type="arbitration",
                summary=f"{action}: {conflict.rule}",
                rationale=rationale,
                related_intents=sorted(related),
                status=DecisionStatus.ACCEPTED,
            )
            state.decisions[decision.id] = decision
            conflict.resolved = True
            conflict.resolution = {
                "action": action,
                "author": author,
                "rationale": rationale,
                "decision_id": decision.id,
                "reviewed_version": state.version,
            }
            return {"conflict": _dump(conflict), "decision": _dump(decision)}

        return self.store.mutate(change, f"backbone: conflict {conflict_id} resolved by {author}")

    def merge_task(
        self,
        task_id: str,
        author: str,
        rationale: str | None = None,
        *,
        expected_version: str,
        expected_target_sha: str,
    ) -> dict:
        author = _actor(author)
        if not expected_version or not expected_target_sha:
            raise ValueError("Merge approval requires the inspected version and target SHA")

        def change(state: BackboneState):
            if state.version != expected_version:
                raise ValueError("Backbone changed since task inspection; inspect again")
            task = state.tasks[task_id]
            artifact = task.artifact
            if task.status != TaskStatus.SUBMITTED or not artifact or not artifact.commit_sha:
                raise ValueError("Only a successfully submitted task can be marked merged")
            current_branch = self._git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
            if current_branch != task.base_ref:
                raise ValueError(
                    f"Record completion on the assigned target branch: {task.base_ref}"
                )
            if self._commit(artifact.branch) != artifact.commit_sha:
                raise ValueError("Artifact branch changed after submission; restart and resubmit")
            if self._git("merge-base", "--is-ancestor", artifact.commit_sha, "HEAD").returncode:
                raise ValueError("Merge the reviewed artifact commit into the current branch first")
            if self._git(
                "merge-base", "--is-ancestor", artifact.commit_sha, artifact.base_ref
            ).returncode:
                raise ValueError("Artifact must be merged into its declared base branch first")
            target_sha = self._commit("HEAD")
            if target_sha != expected_target_sha:
                raise ValueError("Target branch changed since task inspection; inspect again")
            net_paths, divergent_paths = self._integration_paths(artifact, target_sha)
            if not net_paths:
                raise ValueError(
                    "Merged ancestry discarded all declared artifact changes; restore them or resubmit"
                )
            if divergent_paths and not (rationale and rationale.strip()):
                raise ValueError(
                    "Integrated paths differ from the reviewed artifact; give an explicit review rationale"
                )
            self._refresh(state)
            blockers = self._blockers(state, task.intent_id, task_id)
            if blockers:
                raise ValueError(
                    "Unresolved blocking conflicts: " + ", ".join(c.id for c in blockers)
                )
            delta = self._decision_delta(state, task)
            if (delta["new_decisions"] or delta["withdrawn_decisions"]) and not (
                rationale and rationale.strip()
            ):
                raise ValueError(
                    "Decisions changed after submission; give an explicit review rationale"
                )
            review_rationale = (
                f"Reviewed new decisions {delta['new_decisions']} and withdrawn decisions "
                f"{delta['withdrawn_decisions']}: {rationale.strip()}"
                if delta["new_decisions"] or delta["withdrawn_decisions"]
                else rationale.strip()
                if rationale and rationale.strip()
                else "Human records intent, constraints and decision review after Git integration."
            )
            review_rationale = (
                f"Inspection version {expected_version}; artifact {artifact.commit_sha} "
                f"integrated into {task.base_ref} at {target_sha}; "
                f"net paths {net_paths}; divergent paths {divergent_paths}. {review_rationale}"
            )
            review = Decision(
                author=author,
                decision_type="human_review",
                summary=f"Approve merged artifact {artifact.id}",
                rationale=review_rationale,
                related_intents=[task.intent_id],
                status=DecisionStatus.ACCEPTED,
            )
            state.decisions[review.id] = review
            merged_task = transition_task(task, TaskStatus.MERGED)
            merged_task.approval = ReviewAnchor(
                decision_id=review.id,
                reviewed_version=expected_version,
                target_sha=target_sha,
            )
            state.tasks[task_id] = merged_task
            state.intents[task.intent_id] = transition_intent(
                state.intents[task.intent_id], IntentStatus.COMPLETED
            )
            self._refresh(state)
            return {"task": _dump(state.tasks[task_id]), "review_decision": _dump(review)}

        return self.store.mutate(
            change, f"backbone: task {task_id} merged and reviewed by {author}"
        )

    def log(
        self,
        limit: int = 50,
        *,
        author: str | None = None,
        http_principal: str | None = None,
        event_type: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict]:
        return self.store.log(
            limit,
            author=author,
            http_principal=http_principal,
            event_type=event_type,
            since=since,
            until=until,
        )

    def verify_audit_signatures(self, limit: int = 50) -> dict:
        return self.store.verify_audit_signatures(limit)

    def verify_current_snapshot(self) -> dict:
        return self.store.verify_current_snapshot()

    def verify_audit_history(
        self, limit: int = 50, offset: int = 0, expected_head: str | None = None
    ) -> dict:
        return self.store.verify_audit_history(limit, offset, expected_head)

    def review_task(
        self,
        task_id: str,
        dsh_home: str,
        model: str,
        provider: str = "deepseek-official",
        *,
        attempt_log: str | Path | None = None,
    ) -> dict:
        from .review_attempts import ReviewAttemptLog
        from .runtime import DSHReviewer

        snapshot = self.store.read()
        task = snapshot.tasks[task_id]
        artifact = task.artifact
        if task.status != TaskStatus.SUBMITTED or not artifact or not artifact.commit_sha:
            raise ValueError("Semantic review requires a successfully submitted artifact")
        try:
            diff = self.code_store._git_output_digest(
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-color",
                "--binary",
                f"{artifact.base_sha}...{artifact.commit_sha}",
                "--",
                ".",
                ":(exclude).backbone",
                preview_limit=1_000_000,
                max_bytes=1_000_000,
            )
        except StorageError as exc:
            raise ValueError("Cannot read artifact diff for semantic review") from exc
        if not diff.complete:
            raise ValueError("Artifact diff exceeds the 1 MB review limit; split the task")
        patch = diff.preview.decode("utf-8")
        context = {
            "task": _dump(task),
            "intent": _dump(snapshot.intents[task.intent_id]),
            "decisions": [
                _dump(d) for d in snapshot.decisions.values() if d.status == DecisionStatus.ACCEPTED
            ],
        }
        log = (
            ReviewAttemptLog(
                attempt_log,
                (
                    self.code_store.root,
                    self.code_store.git_dir,
                    self.store.root,
                    self.store.git_dir,
                ),
            )
            if attempt_log is not None
            else None
        )
        started_ns = monotonic_ns()

        def record_failure(phase: str, error: Exception) -> None:
            if log is None:
                return
            try:
                log.record(
                    task_id=task_id,
                    model=model,
                    provider=provider,
                    observed_version=snapshot.version or "",
                    artifact_sha=artifact.commit_sha or "",
                    phase=phase,
                    elapsed_ms=round((monotonic_ns() - started_ns) / 1_000_000, 3),
                    error_type=type(error).__name__,
                )
            except Exception as log_error:
                raise RuntimeError(
                    f"Review failed and attempt logging failed ({type(log_error).__name__})"
                ) from error

        try:
            review = DSHReviewer(dsh_home, model, provider).review(context, patch)
        except Exception as exc:
            record_failure("runtime", exc)
            raise

        def change(state: BackboneState):
            if state.version != snapshot.version:
                raise ValueError("Backbone changed during semantic review; run review again")
            current = state.tasks[task_id].artifact
            if not current or self._commit(current.branch) != artifact.commit_sha:
                raise ValueError("Artifact changed during semantic review; resubmit")
            current.checks["semantic_review"] = {
                **review,
                "model": model,
                "provider": provider,
                "reviewed_version": snapshot.version,
                "advisory": True,
            }
            return current.checks["semantic_review"]

        try:
            result = self.store.mutate(
                change, f"backbone: task {task_id} reviewed with {provider}/{model}"
            )
        except Exception as exc:
            record_failure("commit", exc)
            raise
        if log is not None:
            try:
                log.record(
                    task_id=task_id,
                    model=model,
                    provider=provider,
                    observed_version=snapshot.version or "",
                    artifact_sha=artifact.commit_sha or "",
                    phase="commit",
                    elapsed_ms=round((monotonic_ns() - started_ns) / 1_000_000, 3),
                    status="committed",
                )
            except Exception as exc:
                raise RuntimeError(
                    "Semantic review was committed, but attempt logging failed; inspect the ledger before retrying"
                ) from exc
        return result

    def review_stats(self, attempt_log: str | Path) -> dict:
        from .review_attempts import ReviewAttemptLog

        return ReviewAttemptLog(
            attempt_log,
            (
                self.code_store.root,
                self.code_store.git_dir,
                self.store.root,
                self.store.git_dir,
            ),
            create=False,
        ).summary()

    def sync(self, remote: str = "origin", branch: str | None = None) -> dict:
        return self.store.sync(remote, branch)

    def refresh(self, remote: str = "origin", branch: str | None = None) -> dict:
        return self.store.refresh(remote, branch)

    def reconcile(
        self,
        expected_local_head: str,
        expected_remote_head: str,
        author: str,
        rationale: str,
        remote: str = "origin",
        branch: str | None = None,
        resolutions: dict[str, dict[str, dict[str, Any]]] | None = None,
    ) -> dict:
        return self.store.reconcile(
            remote,
            branch,
            expected_local_head,
            expected_remote_head,
            author,
            rationale,
            resolutions,
        )
