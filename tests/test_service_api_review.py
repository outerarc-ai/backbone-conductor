"""Regression cases identified by an independent final service/interface audit."""

import hashlib
import subprocess

import pytest

from backbone_conductor.service import Conductor


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def audit_project(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Audit Test")
    git(tmp_path, "config", "user.email", "audit@example.invalid")
    (tmp_path / "README.md").write_text("Test project\n")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "Initial commit")
    service = Conductor(tmp_path)
    service.initialize()
    return service, tmp_path


def prepare_artifact(service, repo):
    intent = service.create_intent(
        {
            "author": "owner",
            "problem": "Need a database client",
            "proposed_outcome": "Implement the database client",
            "affected_symbols": ["database"],
            "affected_paths": ["database.py"],
        }
    )
    service.transition_intent(intent["id"], "accepted")
    task = service.dispatch_task(intent["id"], "alice")
    service.start_task(task["id"], "alice")
    git(repo, "switch", "-c", "feature/database")
    (repo / "database.py").write_text("DATABASE = {}\n")
    git(repo, "add", "database.py")
    git(repo, "commit", "-m", "Add database client")
    git(repo, "switch", "main")
    return (
        intent,
        task,
        {
            "intent_id": intent["id"],
            "branch": "feature/database",
            "base_ref": "main",
            "summary": "Add database client",
        },
    )


def approval_anchor(service, task_id):
    packet = service.inspect_task(task_id)
    return {
        "expected_version": packet["version"],
        "expected_target_sha": packet["git"]["target_sha"],
    }


def test_merge_approval_must_be_recorded_on_assigned_base_branch(audit_project):
    service, repo = audit_project
    intent, task, artifact = prepare_artifact(service, repo)
    assert service.submit_artifact("alice", artifact)["accepted"]
    git(repo, "merge", "--no-edit", "feature/database")
    git(repo, "switch", "-c", "unrelated-work")
    before = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="base|target|branch"):
        service.merge_task(task["id"], "reviewer", **approval_anchor(service, task["id"]))
    assert git(repo, "rev-parse", "HEAD") == before
    assert service.state()["intents"][intent["id"]]["status"] == "in_progress"
    git(repo, "switch", "main")
    assert (
        service.merge_task(task["id"], "reviewer", **approval_anchor(service, task["id"]))["task"][
            "status"
        ]
        == "merged"
    )


@pytest.mark.parametrize("recovery", ["cancel", "rebase"])
def test_ancestry_only_ours_merge_cannot_complete_task(audit_project, recovery):
    service, repo = audit_project
    intent, task, artifact = prepare_artifact(service, repo)
    assert service.submit_artifact("alice", artifact)["accepted"]
    git(repo, "merge", "-s", "ours", "--no-edit", "feature/database")
    assert "database.py" not in git(repo, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    with pytest.raises(ValueError, match="discarded all declared artifact changes"):
        service.merge_task(
            task["id"], "reviewer", "Approved", **approval_anchor(service, task["id"])
        )
    assert service.state()["tasks"][task["id"]]["status"] == "submitted"
    assert service.state()["intents"][intent["id"]]["status"] == "in_progress"
    if recovery == "cancel":
        cancelled = service.cancel_task(task["id"], "owner", "Merge omitted the implementation")
        assert cancelled["task"]["status"] == "cancelled"
    else:
        rebased = service.rebase_task(task["id"], "alice", service.state()["version"])
        assert rebased["requires_resubmission"] is True
        assert rebased["task"]["status"] == "in_progress"


def test_divergent_integrated_content_requires_review_reason(audit_project):
    service, repo = audit_project
    _intent, task, artifact = prepare_artifact(service, repo)
    submitted = service.submit_artifact("alice", artifact)
    assert submitted["accepted"]
    git(repo, "merge", "--no-edit", "feature/database")
    (repo / "database.py").write_text("DATABASE = {'reviewed': True}\n")
    git(repo, "add", "database.py")
    git(repo, "commit", "-m", "Adapt database integration")
    target_sha = git(repo, "rev-parse", "HEAD")
    packet = service.inspect_task(task["id"])
    assert packet["git"]["integrated_into_target"] is True
    assert packet["git"]["net_changed_paths"] == ["database.py"]
    assert packet["git"]["divergent_paths"] == ["database.py"]
    target_diff = packet["target_diff"]
    assert target_diff["base_sha"] == submitted["artifact"]["base_sha"]
    assert target_diff["target_sha"] == target_sha
    assert target_diff["changed_paths"] == ["database.py"]
    assert "+DATABASE = {'reviewed': True}" in target_diff["patch"]
    assert target_diff["sha256"] == hashlib.sha256(target_diff["patch"].encode()).hexdigest()
    assert target_diff["truncated"] is False
    with pytest.raises(ValueError, match="explicit review rationale"):
        service.merge_task(task["id"], "reviewer", **approval_anchor(service, task["id"]))
    merged = service.merge_task(
        task["id"],
        "reviewer",
        "Reviewed the adapted database implementation",
        **approval_anchor(service, task["id"]),
    )
    rationale = merged["review_decision"]["rationale"]
    assert target_sha in rationale
    assert submitted["artifact"]["commit_sha"] in rationale
    assert "database.py" in rationale


def test_completion_rejects_target_commit_changed_after_inspection(audit_project):
    service, repo = audit_project
    _intent, task, artifact = prepare_artifact(service, repo)
    assert service.submit_artifact("alice", artifact)["accepted"]
    git(repo, "merge", "--no-edit", "feature/database")
    observed = approval_anchor(service, task["id"])
    (repo / "README.md").write_text("Additional unrelated source change\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "Change target after inspection")
    with pytest.raises(ValueError, match="Target branch changed since task inspection"):
        service.merge_task(task["id"], "reviewer", **observed)
    assert service.state()["tasks"][task["id"]]["status"] == "submitted"
    assert (
        service.merge_task(task["id"], "reviewer", **approval_anchor(service, task["id"]))["task"][
            "status"
        ]
        == "merged"
    )


def test_completion_rejects_ledger_change_after_inspection(audit_project):
    service, repo = audit_project
    _intent, task, artifact = prepare_artifact(service, repo)
    assert service.submit_artifact("alice", artifact)["accepted"]
    git(repo, "merge", "--no-edit", "feature/database")
    observed = approval_anchor(service, task["id"])
    service.create_intent(
        {"author": "bob", "problem": "New plan", "proposed_outcome": "A second task"}
    )
    with pytest.raises(ValueError, match="Backbone changed since task inspection"):
        service.merge_task(task["id"], "reviewer", **observed)
    assert service.state()["tasks"][task["id"]]["status"] == "submitted"
    assert (
        service.merge_task(task["id"], "reviewer", **approval_anchor(service, task["id"]))["task"][
            "status"
        ]
        == "merged"
    )


def test_reverted_integration_can_be_cancelled(audit_project):
    service, repo = audit_project
    _intent, task, artifact = prepare_artifact(service, repo)
    assert service.submit_artifact("alice", artifact)["accepted"]
    git(repo, "merge", "--no-ff", "--no-edit", "feature/database")
    with pytest.raises(ValueError, match="Revert integrated artifact code"):
        service.cancel_task(task["id"], "owner", "Plan changed")
    git(repo, "revert", "-m", "1", "--no-edit", "HEAD")
    cancelled = service.cancel_task(task["id"], "owner", "Merged implementation was reverted")
    assert cancelled["task"]["status"] == "cancelled"


def test_review_packet_is_pinned_to_submitted_diff_and_read_only(audit_project):
    service, repo = audit_project
    _intent, task, artifact = prepare_artifact(service, repo)
    with pytest.raises(ValueError, match="submitted"):
        service.inspect_task(task["id"])
    submitted = service.submit_artifact("alice", artifact)
    assert submitted["accepted"]
    state_before = service.state()
    head_before = git(repo, "rev-parse", "HEAD")
    packet = service.inspect_task(task["id"])
    pinned_sha = submitted["artifact"]["commit_sha"]
    assert packet["version"] == state_before["version"]
    assert packet["git"]["artifact_sha"] == pinned_sha
    assert packet["git"]["branch_unchanged"] is True
    assert packet["git"]["integrated_into_target"] is False
    assert packet["target_diff"] is None
    assert packet["diff"]["changed_paths"] == ["database.py"]
    assert "+DATABASE = {}" in packet["diff"]["patch"]
    assert packet["diff"]["truncated"] is False
    assert packet["diff"]["sha256"] == hashlib.sha256(packet["diff"]["patch"].encode()).hexdigest()
    assert packet["requires_human_review"] is True
    assert service.state() == state_before
    assert git(repo, "rev-parse", "HEAD") == head_before

    git(repo, "switch", "feature/database")
    (repo / "database.py").write_text("DATABASE = {'changed': True}\n")
    git(repo, "add", "database.py")
    git(repo, "commit", "-m", "Change after submission")
    git(repo, "switch", "main")
    changed = service.inspect_task(task["id"])
    assert changed["git"]["branch_unchanged"] is False
    assert changed["git"]["artifact_sha"] == pinned_sha
    assert changed["diff"]["patch"] == packet["diff"]["patch"]


def test_completed_task_reopens_approved_snapshot_after_target_moves(audit_project):
    service, repo = audit_project
    _intent, task, artifact = prepare_artifact(service, repo)
    assert service.submit_artifact("alice", artifact)["accepted"]
    git(repo, "merge", "--no-ff", "--no-edit", "feature/database")
    reviewed = service.inspect_task(task["id"], full_patch=True)
    approved = service.merge_task(
        task["id"],
        "reviewer",
        "Checked the final target tree",
        expected_version=reviewed["version"],
        expected_target_sha=reviewed["git"]["target_sha"],
    )
    anchor = approved["task"]["approval"]
    assert anchor == {
        "decision_id": approved["review_decision"]["id"],
        "reviewed_version": reviewed["version"],
        "target_sha": reviewed["git"]["target_sha"],
    }
    recorded = service.inspect_task(task["id"], full_patch=True)
    assert recorded["inspection_kind"] == "approval"
    assert recorded["current_task_status"] == "merged"
    assert recorded["task"] == reviewed["task"]
    assert recorded["intent"] == reviewed["intent"]
    assert recorded["version"] == reviewed["version"]
    assert recorded["current_version"] != reviewed["version"]
    assert recorded["git"]["target_sha"] == reviewed["git"]["target_sha"]
    assert recorded["target_diff"] == reviewed["target_diff"]
    assert recorded["approval"]["decision"]["id"] == anchor["decision_id"]

    (repo / "database.py").write_text("DATABASE = {'later': True}\n")
    git(repo, "add", "database.py")
    git(repo, "commit", "-m", "Change database after approval")
    state_before = service.state()
    moved = service.inspect_task(task["id"])
    assert moved["git"]["target_sha"] == anchor["target_sha"]
    assert moved["git"]["current_target_sha"] == git(repo, "rev-parse", "main")
    assert moved["git"]["current_target_sha"] != anchor["target_sha"]
    assert moved["target_diff"]["sha256"] == reviewed["target_diff"]["sha256"]
    assert service.state() == state_before


def test_legacy_merged_task_without_anchor_does_not_claim_historical_review(audit_project):
    service, repo = audit_project
    _intent, task, artifact = prepare_artifact(service, repo)
    assert service.submit_artifact("alice", artifact)["accepted"]
    git(repo, "merge", "--no-edit", "feature/database")
    reviewed = service.inspect_task(task["id"])
    service.merge_task(
        task["id"],
        "reviewer",
        expected_version=reviewed["version"],
        expected_target_sha=reviewed["git"]["target_sha"],
    )

    def remove_anchor(state):
        state.tasks[task["id"]].approval = None

    service.store.mutate(remove_anchor, "test: emulate pre-anchor merged task")
    with pytest.raises(ValueError, match="predates structured approval"):
        service.inspect_task(task["id"])


@pytest.mark.parametrize("line_count,too_large", [(12_000, False), (60_000, True)])
def test_review_packet_bounds_remote_patch_size(audit_project, line_count, too_large):
    service, repo = audit_project
    _intent, task, artifact = prepare_artifact(service, repo)
    git(repo, "switch", "feature/database")
    (repo / "database.py").write_text(
        "DATABASE = {\n"
        + "".join(f"    'entry-{index:06d}': {index},\n" for index in range(line_count))
        + "}\n"
    )
    git(repo, "add", "database.py")
    git(repo, "commit", "-m", "Expand database fixture")
    git(repo, "switch", "main")
    assert service.submit_artifact("alice", artifact)["accepted"]
    if too_large:
        with pytest.raises(ValueError, match="1 MB review limit"):
            service.inspect_task(task["id"])
        with pytest.raises(ValueError, match="1 MB review limit"):
            service.inspect_task(task["id"], full_patch=True)
        with pytest.raises(ValueError, match="1 MB review limit"):
            service.review_task(task["id"], str(repo.parent / "review-home"), "mock-model")
    else:
        packet = service.inspect_task(task["id"])
        assert packet["diff"]["truncated"] is True
        assert len(packet["diff"]["patch"].encode()) <= 131_072
        full = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "diff",
                "--no-ext-diff",
                "--no-color",
                "--binary",
                f"{packet['git']['base_sha']}...{packet['git']['artifact_sha']}",
                "--",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert packet["diff"]["sha256"] == hashlib.sha256(full.encode()).hexdigest()
        complete = service.inspect_task(task["id"], full_patch=True)
        assert complete["diff"]["patch"] == full
        assert complete["diff"]["truncated"] is False
        assert complete["diff"]["sha256"] == packet["diff"]["sha256"]
        assert complete["version"] == packet["version"]


def test_integrated_target_diff_over_limit_keeps_preview_and_review_anchors(audit_project):
    service, repo = audit_project
    _intent, task, artifact = prepare_artifact(service, repo)
    assert service.submit_artifact("alice", artifact)["accepted"]
    git(repo, "merge", "--no-edit", "feature/database")
    (repo / "database.py").write_text(
        "DATABASE = {\n"
        + "".join(f"    'entry-{index:06d}': {index},\n" for index in range(60_000))
        + "}\n"
    )
    git(repo, "add", "database.py")
    git(repo, "commit", "-m", "Expand integrated database")
    packet = service.inspect_task(task["id"])
    assert packet["git"]["integrated_into_target"] is True
    assert packet["target_diff"]["truncated"] is True
    assert len(packet["target_diff"]["patch"].encode()) <= 131_072
    assert packet["version"] == service.state()["version"]
    assert packet["git"]["target_sha"] == git(repo, "rev-parse", "HEAD")
    target_patch = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "diff",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--no-renames",
            "--binary",
            packet["target_diff"]["base_sha"],
            packet["target_diff"]["target_sha"],
            "--",
            "database.py",
        ],
        check=True,
        capture_output=True,
    ).stdout
    assert packet["target_diff"]["sha256"] == hashlib.sha256(target_patch).hexdigest()
    with pytest.raises(ValueError, match="Integrated target diff exceeds the 1 MB"):
        service.inspect_task(task["id"], full_patch=True)


def test_review_diffs_ignore_configured_textconv(audit_project, monkeypatch):
    service, repo = audit_project
    _intent, task, artifact = prepare_artifact(service, repo)
    assert service.submit_artifact("alice", artifact)["accepted"]
    git(repo, "merge", "--no-edit", "feature/database")
    (repo / ".git/info/attributes").write_text("database.py diff=display\n")
    git(repo, "config", "diff.display.textconv", "sed s/DATABASE/CONVERTED/g")
    packet = service.inspect_task(task["id"], full_patch=True)
    assert packet["git"]["integrated_into_target"] is True
    for field in ("diff", "target_diff"):
        assert "+DATABASE = {}" in packet[field]["patch"]
        assert "CONVERTED" not in packet[field]["patch"]

    observed = {}

    def capture_review(_reviewer, context, patch):
        observed["context"] = context
        observed["patch"] = patch
        return {
            "verdict": "uncertain",
            "rationale": "Test stub only",
            "concerns": [],
            "runtime": {"elapsed_ms": 0, "session_id": "stub", "finish_reason": "completed"},
        }

    monkeypatch.setattr("backbone_conductor.runtime.DSHReviewer.review", capture_review)
    result = service.review_task(task["id"], str(repo.parent / "review-home"), "mock-model")
    assert result["advisory"] is True
    assert observed["context"]["task"]["id"] == task["id"]
    assert "+DATABASE = {}" in observed["patch"]
    assert "CONVERTED" not in observed["patch"]
    display_diff = git(
        repo,
        "diff",
        "--textconv",
        packet["git"]["base_sha"],
        packet["git"]["target_sha"],
        "--",
        "database.py",
    )
    assert "+CONVERTED = {}" in display_diff


def test_global_dependency_conflict_blocks_artifact_until_human_arbitration(audit_project):
    service, repo = audit_project
    dependent = service.log_decision(
        {
            "author": "owner",
            "decision_type": "storage",
            "summary": "Database client requires database",
            "rationale": "Persist records",
            "depends_on": ["database"],
        }
    )
    service.transition_decision(dependent["id"], "accepted")
    removing = service.log_decision(
        {
            "author": "owner",
            "decision_type": "storage",
            "summary": "Remove database",
            "rationale": "Use stateless storage",
            "removes_symbols": ["database"],
        }
    )
    service.transition_decision(removing["id"], "accepted")
    _, task, artifact = prepare_artifact(service, repo)
    result = service.submit_artifact("alice", artifact)
    assert not result["accepted"]
    assert result["checks"]["decisions"]["status"] == "failed"
    assert service.state()["tasks"][task["id"]]["status"] == "in_progress"
    conflict = next(
        value
        for value in service.state()["conflicts"].values()
        if value["rule"] == "dependency_conflict"
    )
    service.resolve_conflict(
        conflict["id"],
        "owner",
        "accept_risk",
        "Staged migration is reviewed",
        service.state()["version"],
    )
    assert not service.submit_artifact("alice", artifact)["accepted"]
    service.rebase_task(task["id"], "alice", service.state()["version"])
    assert service.submit_artifact("alice", artifact)["accepted"]
