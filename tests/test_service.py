import hashlib
import json
import subprocess

import pytest

from backbone_conductor.service import Conductor


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def project(tmp_path):
    git(tmp_path, "init", "-b", "main")
    git(tmp_path, "config", "user.name", "Test User")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    (tmp_path / "README.md").write_text("# Test project\n")
    git(tmp_path, "add", "README.md")
    git(tmp_path, "commit", "-m", "Initial source")
    service = Conductor(tmp_path)
    service.initialize()
    return service, tmp_path


def assigned(service, **extra):
    intent = service.create_intent(
        {
            "author": "owner",
            "problem": "No greeting",
            "proposed_outcome": "Implement greeting",
            **extra,
        }
    )
    service.transition_intent(intent["id"], "accepted")
    task = service.dispatch_task(intent["id"], "alice", forbidden_paths=["secrets/"])
    service.start_task(task["id"], "alice")
    return intent, task


def feature(repo, path="greeting.py", content="def hello():\n    return 'hello'\n"):
    git(repo, "switch", "-c", "feature/greeting")
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", path)
    git(repo, "commit", "-m", "Implement greeting")
    commit = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    return commit


def submission(service, intent, **extra):
    return service.submit_artifact(
        "alice",
        {
            "intent_id": intent["id"],
            "branch": "feature/greeting",
            "summary": "Implemented greeting",
            **extra,
        },
    )


def approval_anchor(service, task_id):
    packet = service.inspect_task(task_id)
    return {
        "expected_version": packet["version"],
        "expected_target_sha": packet["git"]["target_sha"],
    }


def test_complete_workflow_survives_restart_and_records_human_review(project):
    service, repo = project
    intent, task = assigned(service, affected_paths=["greeting.py"])
    commit = feature(repo)
    result = submission(service, intent, changed_paths=["fake.py"], checks={"code": "passed"})
    assert result["accepted"] and result["requires_human_review"]
    assert result["artifact"]["changed_paths"] == ["greeting.py"]
    assert result["artifact"]["commit_sha"] == commit
    assert result["checks"]["intent"]["status"] == "requires_human_review"
    with pytest.raises(ValueError, match="Merge the reviewed"):
        service.merge_task(task["id"], "owner", **approval_anchor(service, task["id"]))
    git(repo, "merge", "--no-edit", "feature/greeting")
    result = service.merge_task(task["id"], "owner", **approval_anchor(service, task["id"]))
    assert result["task"]["status"] == "merged"
    restored = Conductor(repo).state()
    assert restored["intents"][intent["id"]]["status"] == "completed"
    assert result["review_decision"]["id"] in restored["decisions"]
    assert service.get_my_task("alice")["tasks"] == []


@pytest.mark.parametrize(
    "path,content,gate",
    [
        ("secrets/token.txt", "secret\n", "scope"),
        ("elsewhere.py", "ok\n", "scope"),
        ("greeting.py", "trailing spaces   \n", "code"),
        (".backbone/rogue.txt", "rewrite metadata\n", "code"),
    ],
)
def test_failed_checks_do_not_advance_task(project, path, content, gate):
    service, repo = project
    intent, task = assigned(service, affected_paths=["greeting.py"])
    feature(repo, path, content)
    result = submission(service, intent)
    assert not result["accepted"]
    assert result["checks"][gate]["status"] == "failed"
    assert service.state()["tasks"][task["id"]]["status"] == "in_progress"


def test_member_and_lifecycle_enforcement(project):
    service, repo = project
    intent, task = assigned(service)
    with pytest.raises(PermissionError):
        service.start_task(task["id"], "mallory")
    with pytest.raises(PermissionError):
        submission(service, intent, member_id="mallory")
    with pytest.raises(ValueError):
        service.transition_intent(intent["id"], "completed")
    with pytest.raises(ValueError):
        service.create_intent(
            {"author": "alice", "problem": "x", "proposed_outcome": "y", "status": "accepted"}
        )
    with pytest.raises(ValueError):
        service.dispatch_task(intent["id"], "bob")
    with pytest.raises(ValueError):
        service.transition_intent(intent["id"], "superseded")
    feature(repo)
    with pytest.raises(ValueError, match="assigned target"):
        submission(service, intent, base_ref="feature/greeting")
    with pytest.raises(ValueError, match="separate"):
        submission(service, intent, branch="main")


def test_branch_change_invalidates_submission(project):
    service, repo = project
    intent, task = assigned(service)
    feature(repo)
    assert submission(service, intent)["accepted"]
    git(repo, "switch", "feature/greeting")
    (repo / "additional.py").write_text("# Not reviewed\n")
    git(repo, "add", "additional.py")
    git(repo, "commit", "-m", "Change after submission")
    git(repo, "switch", "main")
    git(repo, "merge", "--no-edit", "feature/greeting")
    with pytest.raises(ValueError, match="changed after"):
        service.merge_task(task["id"], "owner", **approval_anchor(service, task["id"]))


def test_arbitration_is_audited_and_new_evidence_is_not_waived(project):
    service, repo = project
    intent, task = assigned(
        service, affected_symbols=["Greeting"], operations={"Greeting": "extend"}
    )
    other = service.create_intent(
        {
            "author": "bob",
            "problem": "Remove old API",
            "proposed_outcome": "Remove it",
            "affected_symbols": ["Greeting"],
            "operations": {"Greeting": "remove"},
        }
    )
    service.transition_intent(other["id"], "accepted")
    feature(repo)
    result = submission(service, intent)
    assert not result["accepted"]
    conflict = next(c for c in result["conflicts"] if c["severity"] != "advisory")
    stale_version = service.state()["version"]
    service.log_decision(
        {
            "author": "owner",
            "decision_type": "process",
            "summary": "Review schedule",
            "rationale": "Plan",
        }
    )
    with pytest.raises(ValueError, match="Backbone changed"):
        service.resolve_conflict(
            conflict["id"], "owner", "coordinate", "Stale review", stale_version
        )
    assert not service.state()["conflicts"][conflict["id"]]["resolved"]
    version = service.state()["version"]
    resolution = service.resolve_conflict(
        conflict["id"], "owner", "coordinate", "Keep compatibility until next release", version
    )
    assert resolution["decision"]["status"] == "accepted"
    assert resolution["conflict"]["resolution"]["reviewed_version"] == version
    assert not submission(service, intent)["accepted"]
    service.rebase_task(task["id"], "alice", service.state()["version"])
    assert submission(service, intent)["accepted"]
    assert service.detect_conflicts()["blocking"] == 0
    with pytest.raises(ValueError, match="already resolved"):
        service.resolve_conflict(
            conflict["id"], "owner", "coordinate", "Again", service.state()["version"]
        )


def test_decision_supersession_and_task_sync(project):
    service, _ = project
    _, task = assigned(service)
    first = service.log_decision(
        {"author": "owner", "decision_type": "storage", "summary": "Use Git", "rationale": "Audit"}
    )
    service.transition_decision(first["id"], "accepted")
    updates = service.check_backbone_sync("alice")
    assert first["id"] in updates["updates"][0]["new_decisions"]
    second = service.log_decision(
        {
            "author": "owner",
            "decision_type": "storage",
            "summary": "Use Git and cache",
            "rationale": "Speed",
            "supersedes": first["id"],
        }
    )
    service.transition_decision(second["id"], "accepted")
    assert service.state()["decisions"][first["id"]]["status"] == "superseded"
    service.revert_decision(
        second["id"], "owner", "The cache introduces stale reads", service.state()["version"]
    )
    state = service.state()
    assert state["decisions"][first["id"]]["status"] == "superseded"
    assert not service.check_backbone_sync("alice", state["version"])["changed"]
    assert task["id"] in state["tasks"]


def test_decision_reversion_requires_reason_current_version_and_accepted_state(project):
    service, repo = project
    with pytest.raises(ValueError, match="Reversion evidence requires reverted"):
        service.log_decision(
            {
                "author": "owner",
                "decision_type": "storage",
                "summary": "Forged history",
                "rationale": "Invalid",
                "reversion": {
                    "author": "reviewer",
                    "rationale": "Forged",
                    "reviewed_version": "not-a-real-version",
                },
            }
        )
    decision = service.log_decision(
        {
            "author": "owner",
            "decision_type": "storage",
            "summary": "Use a cache",
            "rationale": "Faster reads",
        }
    )
    proposed_version = service.state()["version"]
    with pytest.raises(ValueError, match="Only accepted"):
        service.revert_decision(decision["id"], "reviewer", "Invalidated", proposed_version)
    with pytest.raises(ValueError, match="audited author"):
        service.transition_decision(decision["id"], "reverted")
    service.transition_decision(decision["id"], "accepted")
    accepted_version = service.state()["version"]
    head = git(repo, "rev-parse", "HEAD")
    with pytest.raises(ValueError, match="requires a rationale"):
        service.revert_decision(decision["id"], "reviewer", " ", accepted_version)
    with pytest.raises(ValueError, match="refresh the decision"):
        service.revert_decision(decision["id"], "reviewer", "Changed plan", proposed_version)
    assert git(repo, "rev-parse", "HEAD") == head
    reverted = service.revert_decision(
        decision["id"], "reviewer", "The cache is unsafe", accepted_version
    )
    assert reverted["status"] == "reverted"
    assert reverted["reversion"]["author"] == "reviewer"
    assert reverted["reversion"]["rationale"] == "The cache is unsafe"
    assert reverted["reversion"]["reviewed_version"] == accepted_version
    assert git(repo, "rev-parse", "HEAD") != head
    assert "reverted by reviewer" in git(repo, "log", "-1", "--format=%s")
    assert (
        "The cache is unsafe"
        in (repo / ".backbone" / "decisions" / f"{decision['id']}.md").read_text()
    )
    with pytest.raises(ValueError, match="Only accepted"):
        service.revert_decision(decision["id"], "reviewer", "Again", service.state()["version"])


def test_reverted_decision_requires_active_task_context_refresh(project):
    service, repo = project
    decision = service.log_decision(
        {
            "author": "owner",
            "decision_type": "api",
            "summary": "Use the old API",
            "rationale": "Compatibility",
        }
    )
    service.transition_decision(decision["id"], "accepted")
    intent, task = assigned(service, affected_paths=["greeting.py"])
    assert decision["id"] in task["decisions_at_fork"]
    service.revert_decision(
        decision["id"], "owner", "The old API is unsafe", service.state()["version"]
    )
    updates = service.check_backbone_sync("alice")
    assert decision["id"] in updates["updates"][0]["withdrawn_decisions"]
    feature(repo)
    blocked = submission(service, intent)
    assert not blocked["accepted"]
    assert blocked["checks"]["context"]["status"] == "failed"
    service.rebase_task(task["id"], "alice", service.state()["version"])
    assert submission(service, intent)["accepted"]


def test_advisory_semantic_review_and_stale_review_rejected(project, monkeypatch):
    from backbone_conductor.runtime import DSHReviewer

    service, repo = project
    intent, task = assigned(service)
    feature(repo)
    submission(service, intent)
    monkeypatch.setattr(
        DSHReviewer,
        "review",
        lambda *args: {
            "verdict": "aligned",
            "rationale": "Meets task",
            "concerns": [],
            "runtime": {
                "elapsed_ms": 250.0,
                "session_id": "session-review-001",
                "finish_reason": "completed",
            },
        },
    )
    review = service.review_task(task["id"], str(repo / "dsh-home"), "test-model")
    assert review["advisory"]
    assert review["runtime"]["elapsed_ms"] == 250.0
    assert review["runtime"]["session_id"] == "session-review-001"
    assert service.state()["tasks"][task["id"]]["artifact"]["checks"]["semantic_review"] == review
    assert service.state()["tasks"][task["id"]]["status"] == "submitted"

    def stale(*args):
        service.detect_conflicts()
        return {"verdict": "aligned", "rationale": "Meets task", "concerns": []}

    monkeypatch.setattr(DSHReviewer, "review", stale)
    with pytest.raises(ValueError, match="changed during"):
        service.review_task(task["id"], str(repo / "dsh-home"), "test-model")


def test_intent_conflict_advice_is_read_only_and_version_bound(project, monkeypatch):
    from backbone_conductor.runtime import DSHReviewer

    service, repo = project
    left = service.create_intent(
        {
            "author": "alice",
            "problem": "Replace the export API",
            "proposed_outcome": "New export format",
            "affected_paths": ["src/export.py"],
        }
    )
    right = service.create_intent(
        {
            "author": "bob",
            "problem": "Extend the export API",
            "proposed_outcome": "Old callers keep working",
            "affected_paths": ["src/export.py"],
        }
    )
    observed = {}

    def advise(_self, context):
        observed.update(context)
        return {
            "verdict": "compatible",
            "rationale": "The model missed the overlapping path",
            "evidence": [],
            "coordination": [],
            "runtime": {"session_id": "session-advice-1", "finish_reason": "completed"},
        }

    monkeypatch.setattr(DSHReviewer, "advise_conflict", advise)
    before = service.state()
    head = git(repo, "rev-parse", "HEAD")
    home = repo.parent / f"{repo.name}-conflict-advice-home"
    result = service.advise_intent_conflict(left["id"], right["id"], str(home), "test-model")
    assert result["advisory"] and result["human_review_required"]
    assert result["model_advice"]["verdict"] == "compatible"
    assert not result["stale"]
    assert result["observed_version"] == result["current_version"] == before["version"]
    assert (
        result["context_sha256"]
        == hashlib.sha256(
            json.dumps(observed, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
    )
    assert any(item["rule"] == "resource_contention" for item in result["deterministic_conflicts"])
    assert result["blocking_conflict_ids"]
    assert observed["version"] == before["version"]
    assert {item["id"] for item in observed["intents"]} == {left["id"], right["id"]}
    assert all("artifacts" not in item and "reviews" not in item for item in observed["intents"])
    assert git(repo, "rev-parse", "HEAD") == head
    assert service.state() == before
    with pytest.raises(ValueError, match="different intents"):
        service.advise_intent_conflict(left["id"], left["id"], str(home), "test-model")
    with pytest.raises(ValueError, match="outside the repository"):
        service.advise_intent_conflict(
            left["id"], right["id"], str(repo / "advice-home"), "test-model"
        )

    def change_state(_self, _context):
        service.create_intent(
            {
                "author": "carol",
                "problem": "New requirement while advice runs",
                "proposed_outcome": "Version changes",
            }
        )
        return {"verdict": "uncertain", "rationale": "State changed"}

    monkeypatch.setattr(DSHReviewer, "advise_conflict", change_state)
    stale = service.advise_intent_conflict(left["id"], right["id"], str(home), "test-model")
    assert stale["stale"]
    assert stale["observed_version"] != stale["current_version"]


def test_failed_semantic_review_has_redacted_private_attempt_log(project, monkeypatch, tmp_path):
    from backbone_conductor.runtime import DSHReviewer

    service, repo = project
    intent, task = assigned(service)
    feature(repo)
    submission(service, intent)
    before = service.state()["version"]
    private_dir = tmp_path.parent / f"{tmp_path.name}-review-attempts"
    private_dir.mkdir(mode=0o700)
    attempt_log = private_dir / "attempts.jsonl"

    def failed(*_args):
        raise ValueError("provider-secret-should-not-appear")

    monkeypatch.setattr(DSHReviewer, "review", failed)
    for _ in range(2):
        with pytest.raises(ValueError, match="provider-secret"):
            service.review_task(
                task["id"], str(repo / "dsh-home"), "test-model", attempt_log=attempt_log
            )
    assert service.state()["version"] == before
    assert attempt_log.stat().st_mode & 0o777 == 0o600
    contents = attempt_log.read_text()
    assert "provider-secret" not in contents
    assert "diff --git" not in contents
    events = [json.loads(line) for line in contents.splitlines()]
    assert len(events) == 2
    assert all(event["status"] == "failed" and event["phase"] == "runtime" for event in events)
    assert all(event["task_id"] == task["id"] for event in events)
    assert all(event["observed_version"] == before for event in events)
    assert all(event["elapsed_ms"] >= 0 for event in events)
    assert all(event["error_type"] == "ValueError" for event in events)


def test_stale_semantic_review_logs_commit_phase_without_approval(project, monkeypatch, tmp_path):
    from backbone_conductor.runtime import DSHReviewer

    service, repo = project
    intent, task = assigned(service)
    feature(repo)
    submission(service, intent)
    private_dir = tmp_path.parent / f"{tmp_path.name}-review-attempts"
    private_dir.mkdir(mode=0o700)
    attempt_log = private_dir / "attempts.jsonl"

    def stale(*_args):
        service.detect_conflicts()
        return {"verdict": "aligned", "rationale": "Looks aligned", "concerns": []}

    monkeypatch.setattr(DSHReviewer, "review", stale)
    with pytest.raises(ValueError, match="changed during"):
        service.review_task(
            task["id"], str(repo / "dsh-home"), "test-model", attempt_log=attempt_log
        )
    event = json.loads(attempt_log.read_text())
    assert event["phase"] == "commit"
    assert "semantic_review" not in service.state()["tasks"][task["id"]]["artifact"]["checks"]


def test_private_review_stats_include_committed_and_failed_attempts(project, monkeypatch, tmp_path):
    from backbone_conductor.runtime import DSHReviewer

    service, repo = project
    intent, task = assigned(service)
    feature(repo)
    submission(service, intent)
    private_dir = tmp_path.parent / f"{tmp_path.name}-review-stats"
    private_dir.mkdir(mode=0o700)
    attempt_log = private_dir / "attempts.jsonl"

    def failed(*_args):
        raise TimeoutError("provider-detail-must-stay-private")

    monkeypatch.setattr(DSHReviewer, "review", failed)
    with pytest.raises(TimeoutError):
        service.review_task(
            task["id"], str(repo / "dsh-home"), "test-model", attempt_log=attempt_log
        )

    monkeypatch.setattr(
        DSHReviewer,
        "review",
        lambda *_args: {"verdict": "aligned", "rationale": "Meets task", "concerns": []},
    )
    service.review_task(task["id"], str(repo / "dsh-home"), "test-model", attempt_log=attempt_log)
    events = [json.loads(line) for line in attempt_log.read_text().splitlines()]
    assert [event["status"] for event in events] == ["failed", "committed"]
    assert "provider-detail-must-stay-private" not in attempt_log.read_text()
    assert "error_type" not in events[1]
    assert service.review_stats(attempt_log) == {
        "recorded_attempts": 2,
        "committed": 1,
        "failed": 1,
        "legacy_failure_records": 0,
        "failure_by_phase": {"runtime": 1},
        "recorded_commit_rate": 0.5,
        "median_elapsed_ms": pytest.approx(
            round(sum(event["elapsed_ms"] for event in events) / 2, 3), abs=0.001
        ),
        "token_usage": None,
        "cost": None,
    }


def test_review_reports_when_telemetry_fails_after_git_commit(project, monkeypatch, tmp_path):
    from backbone_conductor.review_attempts import ReviewAttemptLog
    from backbone_conductor.runtime import DSHReviewer

    service, repo = project
    intent, task = assigned(service)
    feature(repo)
    submission(service, intent)
    private_dir = tmp_path.parent / f"{tmp_path.name}-review-log-failure"
    private_dir.mkdir(mode=0o700)
    monkeypatch.setattr(
        DSHReviewer,
        "review",
        lambda *_args: {"verdict": "aligned", "rationale": "Meets task", "concerns": []},
    )

    def failed_record(self, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(ReviewAttemptLog, "record", failed_record)
    with pytest.raises(RuntimeError, match="was committed.*inspect the ledger"):
        service.review_task(
            task["id"],
            str(repo / "dsh-home"),
            "test-model",
            attempt_log=private_dir / "attempts.jsonl",
        )
    assert (
        service.state()["tasks"][task["id"]]["artifact"]["checks"]["semantic_review"]["verdict"]
        == "aligned"
    )


def test_intent_revision_requires_current_version_and_reapproval(project):
    service, _ = project
    parent = service.create_intent(
        {"author": "owner", "problem": "Parent", "proposed_outcome": "Plan"}
    )
    child = service.create_intent(
        {
            "author": "owner",
            "problem": "Original",
            "proposed_outcome": "Build",
            "parent_intent": parent["id"],
        }
    )
    original = service.state()["version"]
    revised = service.revise_intent(
        child["id"], {"problem": "Updated", "affected_paths": ["src/"]}, "owner", original
    )
    assert revised["status"] == "draft"
    assert revised["problem"] == "Updated"
    with pytest.raises(ValueError, match="changed"):
        service.revise_intent(child["id"], {"problem": "Stale"}, "owner", original)
    with pytest.raises(ValueError, match="editable"):
        service.revise_intent(
            child["id"], {"status": "completed"}, "owner", service.state()["version"]
        )
    service.transition_intent(child["id"], "accepted")
    version = service.state()["version"]
    assert (
        service.revise_intent(child["id"], {"proposed_outcome": "Better"}, "owner", version)[
            "status"
        ]
        == "draft"
    )
    with pytest.raises(ValueError, match="cycle"):
        service.revise_intent(
            parent["id"], {"parent_intent": child["id"]}, "owner", service.state()["version"]
        )
    assert service.state()["intents"][parent["id"]]["parent_intent"] is None


def test_cancel_reopens_intent_and_allows_new_assignment(project):
    service, repo = project
    intent, task = assigned(service, affected_paths=["greeting.py"])
    feature(repo)
    assert submission(service, intent)["accepted"]
    cancellation = service.cancel_task(task["id"], "owner", "Scope changed")
    assert cancellation["task"]["status"] == "cancelled"
    assert cancellation["task"]["cancel_reason"] == "Scope changed"
    assert cancellation["intent"]["status"] == "accepted"
    assert service.get_my_task("alice")["tasks"] == []
    with pytest.raises(ValueError, match="cannot be cancelled"):
        service.cancel_task(task["id"], "owner", "Again")
    with pytest.raises(ValueError, match="cannot be revised"):
        service.revise_intent(intent["id"], {"problem": "New"}, "owner", service.state()["version"])
    replacement = service.dispatch_task(intent["id"], "bob")
    assert replacement["id"] != task["id"]
    assert service.get_my_task("bob")["tasks"][0]["id"] == replacement["id"]


def test_cancel_then_replace_intent_preserves_history_and_requires_fresh_acceptance(project):
    service, repo = project
    original, task = assigned(service, affected_paths=["greeting.py"])
    before = service.state()["version"]
    with pytest.raises(ValueError, match="Only accepted"):
        service.replace_intent(original["id"], {"problem": "New scope"}, "owner", "Changed", before)
    assert service.state()["version"] == before
    service.cancel_task(task["id"], "owner", "Scope changed")
    version = service.state()["version"]
    with pytest.raises(ValueError, match="changed"):
        service.replace_intent(original["id"], {"problem": "New"}, "owner", "Changed", before)
    with pytest.raises(ValueError, match="editable"):
        service.replace_intent(original["id"], {"status": "completed"}, "owner", "Changed", version)
    with pytest.raises(ValueError, match="reason"):
        service.replace_intent(original["id"], {"problem": "New"}, "owner", " ", version)
    result = service.replace_intent(
        original["id"],
        {"problem": "New scope", "affected_paths": ["src/"]},
        "owner",
        "Requirements changed",
        version,
    )
    successor = result["replacement"]
    assert result["previous"]["status"] == "superseded"
    assert successor["status"] == "draft"
    assert successor["supersedes"] == original["id"]
    assert successor["change_reason"] == "Requirements changed"
    assert successor["problem"] == "New scope"
    assert successor["affected_paths"] == ["src/"]
    assert successor["proposed_outcome"] == original["proposed_outcome"]
    assert service.state()["tasks"][task["id"]]["status"] == "cancelled"
    assert service.state()["intents"][original["id"]]["problem"] == original["problem"]
    assert git(repo, "log", "-1", "--format=%s").endswith("replaced by owner")
    with pytest.raises(ValueError, match="invalid Intent transition"):
        service.transition_intent(original["id"], "accepted")
    with pytest.raises(ValueError, match="accepted"):
        service.dispatch_task(successor["id"], "bob")
    service.transition_intent(successor["id"], "accepted")
    assert service.dispatch_task(successor["id"], "bob")["intent_id"] == successor["id"]


def test_direct_supersession_and_forged_replacement_are_rejected(project):
    service, _ = project
    original = service.create_intent(
        {"author": "owner", "problem": "Original", "proposed_outcome": "Build"}
    )
    with pytest.raises(ValueError, match="lifecycle"):
        service.create_intent(
            {
                "author": "owner",
                "problem": "Fake replacement",
                "proposed_outcome": "Build",
                "supersedes": original["id"],
                "change_reason": "Changed",
            }
        )
    service.transition_intent(original["id"], "accepted")
    with pytest.raises(ValueError, match="audited replacement"):
        service.transition_intent(original["id"], "superseded")


def test_intent_review_records_reason_version_and_requires_fresh_draft(project):
    service, repo = project
    intent = service.create_intent(
        {"author": "alice", "problem": "Need export", "proposed_outcome": "Export records"}
    )
    version = service.state()["version"]
    with pytest.raises(PermissionError, match="own intent"):
        service.review_intent(intent["id"], "accepted", "alice", "Looks good", version)
    with pytest.raises(ValueError, match="rationale"):
        service.review_intent(intent["id"], "accepted", "carol", " ", version)
    with pytest.raises(ValueError, match="outcome"):
        service.review_intent(intent["id"], "completed", "carol", "Looks good", version)
    assert service.state()["version"] == version
    accepted = service.review_intent(
        intent["id"], "accepted", "carol", "Scope and constraints are clear", version
    )
    assert accepted["status"] == "accepted"
    assert accepted["reviews"][-1]["reviewer"] == "carol"
    assert accepted["reviews"][-1]["reviewed_version"] == version
    assert accepted["reviews"][-1]["rationale"] == "Scope and constraints are clear"
    assert git(repo, "log", "-1", "--format=%s").endswith("accepted by carol")
    view = (repo / ".backbone" / "intents" / f"{intent['id']}.md").read_text()
    assert '"rationale": "Scope and constraints are clear"' in view
    with pytest.raises(ValueError, match="changed"):
        service.review_intent(intent["id"], "rejected", "bob", "No", version)
    revised = service.revise_intent(
        intent["id"], {"problem": "Need export with schema"}, "alice", service.state()["version"]
    )
    assert revised["status"] == "draft"
    rejected = service.review_intent(
        intent["id"], "rejected", "carol", "Schema remains undefined", service.state()["version"]
    )
    assert rejected["status"] == "rejected"
    assert [review["outcome"] for review in rejected["reviews"]] == ["accepted", "rejected"]
    with pytest.raises(ValueError, match="Only draft"):
        service.review_intent(
            intent["id"], "accepted", "carol", "Again", service.state()["version"]
        )


def test_intent_creation_cannot_forge_review(project):
    service, _ = project
    with pytest.raises(ValueError, match="lifecycle"):
        service.create_intent(
            {
                "author": "alice",
                "problem": "Need export",
                "proposed_outcome": "Export records",
                "reviews": [
                    {
                        "reviewer": "carol",
                        "outcome": "accepted",
                        "rationale": "Looks good",
                        "reviewed_version": "fake",
                    }
                ],
            }
        )


def test_task_rebase_refreshes_decisions_and_invalidates_submitted_artifact(project):
    service, repo = project
    intent, task = assigned(service)
    feature(repo)
    assert submission(service, intent)["accepted"]
    decision = service.log_decision(
        {
            "author": "owner",
            "decision_type": "api",
            "summary": "Keep greeting",
            "rationale": "Compatibility",
        }
    )
    service.transition_decision(decision["id"], "accepted")
    old_version = service.state()["version"]
    with pytest.raises(PermissionError):
        service.rebase_task(task["id"], "mallory", old_version)
    rebased = service.rebase_task(task["id"], "alice", old_version)
    assert rebased["requires_resubmission"]
    assert rebased["task"]["status"] == "in_progress"
    assert rebased["task"]["artifact"] is None
    assert decision["id"] in rebased["task"]["decisions_at_fork"]
    assert service.check_backbone_sync("alice")["updates"][0]["new_decisions"] == []
    with pytest.raises(ValueError, match="changed"):
        service.rebase_task(task["id"], "alice", old_version)
    assert submission(service, intent)["accepted"]


def test_task_rebase_rejects_wrong_branch(project):
    service, repo = project
    _, task = assigned(service)
    git(repo, "switch", "-c", "feature")
    with pytest.raises(ValueError, match="target branch"):
        service.rebase_task(task["id"], "alice", service.state()["version"])


def test_new_decision_blocks_submission_until_context_rebase(project):
    service, repo = project
    intent, task = assigned(service)
    feature(repo)
    decision = service.log_decision(
        {
            "author": "owner",
            "decision_type": "api",
            "summary": "Greet by name",
            "rationale": "User needs it",
        }
    )
    service.transition_decision(decision["id"], "accepted")
    result = submission(service, intent)
    assert not result["accepted"]
    assert result["checks"]["context"]["status"] == "failed"
    assert decision["id"] in result["checks"]["context"]["new_decisions"]
    service.rebase_task(task["id"], "alice", service.state()["version"])
    assert submission(service, intent)["accepted"]


def test_decision_after_submission_requires_explicit_merge_review(project):
    service, repo = project
    intent, task = assigned(service)
    feature(repo)
    assert submission(service, intent)["accepted"]
    decision = service.log_decision(
        {
            "author": "owner",
            "decision_type": "api",
            "summary": "New API guideline",
            "rationale": "Consistency",
        }
    )
    service.transition_decision(decision["id"], "accepted")
    git(repo, "merge", "--no-edit", "feature/greeting")
    with pytest.raises(ValueError, match="review rationale"):
        service.merge_task(task["id"], "owner", **approval_anchor(service, task["id"]))
    result = service.merge_task(
        task["id"],
        "owner",
        "Checked the new guideline against greeting.py",
        **approval_anchor(service, task["id"]),
    )
    assert result["task"]["status"] == "merged"
    assert decision["id"] in result["review_decision"]["rationale"]


def test_integrated_artifact_cannot_be_cancelled_or_rebased(project):
    service, repo = project
    intent, task = assigned(service)
    feature(repo)
    assert submission(service, intent)["accepted"]
    git(repo, "merge", "--no-edit", "feature/greeting")
    with pytest.raises(ValueError, match="Revert integrated"):
        service.cancel_task(task["id"], "owner", "Scope changed")
    with pytest.raises(ValueError, match="already integrated"):
        service.rebase_task(task["id"], "alice", service.state()["version"])
    assert service.state()["tasks"][task["id"]]["status"] == "submitted"
