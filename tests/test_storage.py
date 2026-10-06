from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backbone_conductor.api import create_app
from backbone_conductor.auth import create_token_file
from backbone_conductor.cli import main
from backbone_conductor.models import Decision, Intent, Task
from backbone_conductor.service import Conductor
from backbone_conductor.storage import GitStore, StorageError


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, text=True, capture_output=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Storage Test")
    git(root, "config", "user.email", "storage@example.test")
    return root


def snapshot(path: Path) -> dict[str, bytes]:
    return {
        item.relative_to(path).as_posix(): item.read_bytes()
        for item in path.rglob("*")
        if item.is_file()
    }


def configure_ssh_signing(repo: Path, key: Path, email: str) -> Path:
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
        timeout=20,
    )
    allowed = key.parent / f"{key.name}-allowed-signers"
    allowed.write_text(email + " " + key.with_suffix(".pub").read_text())
    git(repo, "config", "gpg.format", "ssh")
    git(repo, "config", "user.signingkey", str(key))
    git(repo, "config", "gpg.ssh.allowedSignersFile", str(allowed))
    git(repo, "config", "commit.gpgsign", "true")
    return allowed


def increment_counter(repo: str) -> None:
    store = GitStore(repo)

    def change(state):
        current = state.sessions.get("counter", {}).get("value", 0)
        state.sessions["counter"] = {"value": current + 1}

    store.mutate(change, "backbone: increment counter")


def test_init_on_unborn_branch_and_idempotence(repo: Path):
    store = GitStore(repo)
    state = store.init()
    assert state.version == git(repo, "rev-parse", "HEAD")
    assert state.parent_version is None
    assert json.loads((repo / ".backbone/state.json").read_text())["version"] is None
    assert (repo / ".backbone/BACKBONE.md").is_file()
    assert store.init().version == state.version
    assert len(store.log()) == 1
    assert git(repo, "status", "--porcelain") == ""


def test_read_requires_init_and_repo_must_exist(tmp_path: Path, repo: Path):
    with pytest.raises(StorageError, match="not initialized"):
        GitStore(repo).read()
    with pytest.raises(StorageError, match="does not exist"):
        GitStore(tmp_path / "absent")
    with pytest.raises(StorageError, match="not a git repository"):
        GitStore(tmp_path)


def test_read_version_requires_reachable_metadata_commit(repo: Path):
    store = GitStore(repo)
    initial = store.init()
    store.mutate(lambda state: state.sessions.update({"example": {"value": 1}}), "new state")
    assert store.read_version(initial.version).version == initial.version
    assert store.read_version(initial.version).sessions == {}
    assert store.read().sessions == {"example": {"value": 1}}

    (repo / "code.txt").write_text("code\n")
    git(repo, "add", "code.txt")
    git(repo, "commit", "-m", "Code only")
    with pytest.raises(StorageError, match="metadata version"):
        store.read_version(git(repo, "rev-parse", "HEAD"))
    with pytest.raises(StorageError, match="full Backbone metadata commit SHA"):
        store.read_version("HEAD")


def test_git_output_digest_streams_preview_and_stops_at_hard_limit(repo: Path):
    data = b"large-diff-line\n" * 150_000
    (repo / "large.txt").write_bytes(data)
    git(repo, "add", "large.txt")
    git(repo, "commit", "-m", "Large file")
    store = GitStore(repo)
    complete = store._git_output_digest("show", "HEAD:large.txt", preview_limit=257)
    assert complete.complete is True
    assert complete.preview == data[:257]
    assert complete.size == len(data)
    assert complete.sha256 == hashlib.sha256(data).hexdigest()

    limited = store._git_output_digest("show", "HEAD:large.txt", preview_limit=257, max_bytes=1024)
    assert limited.complete is False
    assert limited.preview == data[:257]
    assert limited.size > 1024
    assert limited.sha256 is None
    with pytest.raises(StorageError, match="failed"):
        store._git_output_digest("show", "HEAD:missing.txt", preview_limit=10)


def test_persistence_versions_and_generated_views(repo: Path):
    store = GitStore(repo)
    first = store.init()
    intent = Intent(author="alice", problem="重复逻辑", proposed_outcome="One implementation")
    decision = Decision(
        author="alice",
        decision_type="architecture",
        summary="Share the parser",
        rationale="Consistency",
    )
    task = Task(member_id="alice", intent_id=intent.id)

    def change(state):
        state.intents[intent.id] = intent
        state.decisions[decision.id] = decision
        state.tasks[task.id] = task
        state.sessions["alice@example.test"] = {"active": True}
        return intent.id

    assert store.mutate(change, "backbone: add coordination objects") == intent.id
    loaded = GitStore(repo).read()
    assert loaded.intents[intent.id] == intent
    assert loaded.tasks[task.id] == task
    assert loaded.parent_version == first.version
    assert loaded.version != first.version
    assert "重复逻辑" in (repo / f".backbone/intents/{intent.id}.md").read_text()
    assert "Share the parser" in (repo / f".backbone/decisions/{decision.id}.md").read_text()
    assert (
        json.loads((repo / f".backbone/tasks/{task.id}.json").read_text())["member_id"] == "alice"
    )
    assert len(list((repo / ".backbone/sessions").glob("*.md"))) == 1
    entry = store.log(limit=1)[0]
    assert entry["commit"] == loaded.version
    assert entry["author"] == "Storage Test"
    assert entry["message"] == "backbone: add coordination objects"
    assert entry["timestamp"]
    assert "http_principal" not in entry


def test_audit_reports_unsigned_history_and_strict_cli_fails(repo: Path, capsys):
    store = GitStore(repo)
    store.init()
    report = store.verify_audit_signatures()
    assert report["checked"] == 1
    assert report["unsigned"] == 1
    assert not report["all_inspected_signed_and_valid"]
    assert main(["--repo", str(repo), "audit", "verify"]) == 0
    assert json.loads(capsys.readouterr().out)["unsigned"] == 1
    assert main(["--repo", str(repo), "audit", "verify", "--require-signatures"]) == 1
    assert json.loads(capsys.readouterr().out)["all_inspected_signed_and_valid"] is False


def test_snapshot_verification_checks_views_and_git_lineage(repo: Path, capsys):
    store = GitStore(repo)
    first = store.init()
    assert store.verify_current_snapshot()["ok"]
    store.mutate(lambda state: state.sessions.update({"example": {"value": 1}}), "new state")
    valid = store.verify_current_snapshot()
    assert valid["ok"]
    assert valid["parent_version"] == valid["expected_parent_version"] == first.version
    (repo / "code.txt").write_text("ordinary source change\n")
    git(repo, "add", "code.txt")
    git(repo, "commit", "-m", "Change source without touching Backbone")
    assert store.verify_current_snapshot()["ok"]
    assert main(["--repo", str(repo), "audit", "verify-snapshot"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]

    view = repo / ".backbone/BACKBONE.md"
    view.write_text(view.read_text() + "Unreviewed external edit\n")
    git(repo, "add", ".backbone/BACKBONE.md")
    git(repo, "commit", "-m", "External metadata edit")
    invalid = store.verify_current_snapshot()
    assert not invalid["ok"]
    assert invalid["changed_views"] == ["BACKBONE.md"]
    assert not invalid["parent_links_ok"]
    assert main(["--repo", str(repo), "audit", "verify-snapshot"]) == 1
    assert json.loads(capsys.readouterr().out)["changed_views"] == ["BACKBONE.md"]


def test_snapshot_verification_detects_wrong_parent_with_matching_views(repo: Path):
    store = GitStore(repo)
    store.init()
    store.mutate(lambda state: state.sessions.update({"example": {"value": 1}}), "new state")
    state_path = repo / ".backbone/state.json"
    state = json.loads(state_path.read_text())
    state["parent_version"] = None
    state_path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    git(repo, "add", ".backbone/state.json")
    git(repo, "commit", "-m", "External lineage edit")
    report = store.verify_current_snapshot()
    assert report["changed_views"] == []
    assert not report["parent_links_ok"]
    assert not report["ok"]


def test_history_verification_finds_repaired_older_metadata_drift(repo: Path, capsys):
    store = GitStore(repo)
    store.init()
    store.mutate(lambda state: state.sessions.update({"first": {"value": 1}}), "first")
    (repo / "code.txt").write_text("ordinary source change\n")
    git(repo, "add", "code.txt")
    git(repo, "commit", "-m", "Code only")
    view = repo / ".backbone/BACKBONE.md"
    view.write_text(view.read_text() + "External drift\n")
    git(repo, "add", ".backbone/BACKBONE.md")
    git(repo, "commit", "-m", "External metadata drift")
    damaged = git(repo, "rev-parse", "HEAD")
    store.mutate(lambda state: state.sessions.update({"later": {"value": 2}}), "repair")

    assert store.verify_current_snapshot()["ok"]
    report = store.verify_audit_history(limit=10)
    assert not report["ok"]
    assert not report["truncated"]
    assert report["invalid"] == 1
    assert report["total_metadata_commits"] == 4
    bad = next(entry for entry in report["commits"] if entry["commit"] == damaged)
    assert bad["changed_views"] == [".backbone/BACKBONE.md"]
    assert not bad["parent_links_ok"]
    assert main(["--repo", str(repo), "audit", "verify-history", "--limit", "10"]) == 1
    assert json.loads(capsys.readouterr().out)["invalid"] == 1

    latest_only = store.verify_audit_history(limit=1)
    assert latest_only["invalid"] == 0
    assert latest_only["truncated"]
    assert not latest_only["ok"]


def test_history_verification_accepts_complete_normal_chain(repo: Path, capsys):
    store = GitStore(repo)
    store.init()
    store.mutate(lambda state: state.sessions.update({"example": {"value": 1}}), "new state")
    report = store.verify_audit_history(limit=2)
    assert report["ok"]
    assert report["checked"] == report["total_metadata_commits"] == 2
    assert all(entry["ok"] for entry in report["commits"])
    assert main(["--repo", str(repo), "audit", "verify-history", "--all", "--limit", "1"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["pages"] == 2
    assert summary["checked"] == 2
    assert summary["invalid_commits"] == []


def test_history_verification_ignores_code_merge_with_unchanged_first_parent_metadata(repo: Path):
    store = GitStore(repo)
    store.init()
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "feature/code", base)
    (repo / "feature.py").write_text("value = 1\n")
    git(repo, "add", "feature.py")
    git(repo, "commit", "-m", "Implement code feature")
    git(repo, "switch", "main")
    store.mutate(lambda state: state.sessions.update({"later": {"value": 2}}), "new metadata")
    git(repo, "merge", "--no-ff", "--no-edit", "feature/code")
    code_merge = git(repo, "rev-parse", "HEAD")

    report = store.verify_audit_history(limit=10)
    assert report["ok"]
    assert report["total_metadata_commits"] == 2
    assert code_merge not in {entry["commit"] for entry in report["commits"]}
    assert {entry["commit"] for entry in store.log()} == {
        entry["commit"] for entry in report["commits"]
    }
    assert store.verify_audit_signatures()["total_metadata_commits"] == 2


def test_history_verification_rejects_merge_that_discards_side_metadata(repo: Path, capsys):
    store = GitStore(repo)
    store.init()
    base = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "-c", "feature/metadata", base)
    store.mutate(lambda state: state.sessions.update({"side": {"value": 1}}), "side metadata")
    side_metadata = git(repo, "rev-parse", "HEAD")
    git(repo, "switch", "main")
    store.mutate(lambda state: state.sessions.update({"main": {"value": 2}}), "main metadata")
    git(repo, "merge", "--no-ff", "--no-edit", "-s", "ours", "feature/metadata")
    dropped_merge = git(repo, "rev-parse", "HEAD")

    report = store.verify_audit_history(limit=10)
    assert not report["ok"]
    assert report["total_metadata_commits"] == 4
    assert side_metadata in {entry["commit"] for entry in report["commits"]}
    dropped = next(entry for entry in report["commits"] if entry["commit"] == dropped_merge)
    assert not dropped["parent_links_ok"]
    assert {entry["commit"] for entry in store.log()} == {
        entry["commit"] for entry in report["commits"]
    }
    signatures = store.verify_audit_signatures(limit=10)
    assert signatures["total_metadata_commits"] == signatures["checked"] == 4
    assert {entry["commit"] for entry in signatures["commits"]} == {
        entry["commit"] for entry in report["commits"]
    }
    assert main(["--repo", str(repo), "audit", "verify-history", "--all", "--limit", "1"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert {entry["commit"] for entry in summary["invalid_commits"]} == {dropped_merge}


def test_history_verification_pages_and_aggregates_without_losing_older_failures(
    repo: Path, capsys
):
    store = GitStore(repo)
    store.init()
    store.mutate(lambda state: state.sessions.update({"first": {"value": 1}}), "first")
    view = repo / ".backbone/BACKBONE.md"
    view.write_text(view.read_text() + "External drift\n")
    git(repo, "add", ".backbone/BACKBONE.md")
    git(repo, "commit", "-m", "External drift")
    store.mutate(lambda state: state.sessions.update({"later": {"value": 2}}), "repair")
    first = store.verify_audit_history(limit=1)
    assert first["page_ok"]
    assert first["truncated"]
    assert not first["ok"]
    assert first["next_offset"] == 1
    second = store.verify_audit_history(limit=1, offset=1, expected_head=first["head"])
    assert second["offset"] == 1
    assert second["invalid"] == 1
    assert second["truncated"]
    assert not second["page_ok"]
    assert not second["ok"]
    assert main(["--repo", str(repo), "audit", "verify-history", "--all", "--limit", "1"]) == 1
    summary = json.loads(capsys.readouterr().out)
    assert summary["checked"] == 4
    assert summary["pages"] == 4
    assert summary["invalid"] == len(summary["invalid_commits"]) == 1


def test_history_verification_rejects_changed_head_and_bad_offset(repo: Path):
    store = GitStore(repo)
    store.init()
    page = store.verify_audit_history(limit=1)
    (repo / "code.txt").write_text("new code\n")
    git(repo, "add", "code.txt")
    git(repo, "commit", "-m", "Move code HEAD")
    with pytest.raises(StorageError, match="HEAD changed between history pages"):
        store.verify_audit_history(limit=1, expected_head=page["head"])
    with pytest.raises(StorageError, match="offset is beyond"):
        store.verify_audit_history(limit=1, offset=1)


def test_history_verification_reports_missing_historical_state(repo: Path):
    store = GitStore(repo)
    store.init()
    git(repo, "rm", ".backbone/state.json")
    git(repo, "commit", "-m", "External removal of state")
    report = store.verify_audit_history(limit=2)
    assert report["invalid"] == 1
    assert not report["ok"]
    assert report["commits"][0]["error"] == "Missing state.json at metadata commit"


def test_history_verification_reports_non_utf8_historical_state(repo: Path):
    store = GitStore(repo)
    store.init()
    (repo / ".backbone/state.json").write_bytes(b"\xff")
    git(repo, "add", ".backbone/state.json")
    git(repo, "commit", "-m", "External binary state")
    report = store.verify_audit_history(limit=2)
    assert report["invalid"] == 1
    assert report["commits"][0]["error"] == "state.json is not UTF-8"


def test_log_filters_author_type_and_time_before_limit(repo: Path, monkeypatch, capsys):
    store = GitStore(repo)
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-09-27T09:00:00+00:00")
    store.init()
    git(repo, "config", "user.name", "Alice")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-09-28T09:00:00+00:00")
    Conductor(repo).create_intent(
        {"id": "intent-one", "author": "alice", "problem": "One", "proposed_outcome": "One"}
    )
    git(repo, "config", "user.name", "Bob")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-09-29T09:00:00+00:00")
    Conductor(repo).log_decision(
        {
            "id": "decision-one",
            "author": "bob",
            "decision_type": "architecture",
            "summary": "Use one parser",
            "rationale": "Reuse",
        }
    )
    git(repo, "config", "user.name", "Alice")
    monkeypatch.setenv("GIT_AUTHOR_DATE", "2026-09-30T09:00:00+00:00")
    Conductor(repo).create_intent(
        {"id": "intent-two", "author": "alice", "problem": "Two", "proposed_outcome": "Two"}
    )

    assert [entry["event_type"] for entry in store.log()] == [
        "intent",
        "decision",
        "intent",
        "initialize",
    ]
    assert [entry["message"] for entry in store.log(1, author="Alice", event_type="intent")] == [
        "backbone: intent intent-two created by alice"
    ]
    assert [entry["author"] for entry in store.log(2, author="Alice")] == ["Alice", "Alice"]
    assert [entry["message"] for entry in store.log(since="2026-09-28T09:00:00Z")] == [
        "backbone: intent intent-two created by alice",
        "backbone: decision decision-one proposed by bob",
        "backbone: intent intent-one created by alice",
    ]
    assert [entry["event_type"] for entry in store.log(until="2026-09-28T09:00:00Z")] == [
        "intent",
        "initialize",
    ]
    assert store.log(http_principal="alice") == []
    assert main(["--repo", str(repo), "log", "--type", "decision", "--author", "Bob"]) == 0
    assert [entry["event_type"] for entry in json.loads(capsys.readouterr().out)] == ["decision"]
    with pytest.raises(StorageError, match="timezone"):
        store.log(since="2026-09-28T09:00:00")
    with pytest.raises(StorageError, match="since must not be after until"):
        store.log(since="2026-09-30T09:00:00Z", until="2026-09-28T09:00:00Z")
    with pytest.raises(StorageError, match="Unknown audit event type"):
        store.log(event_type="unknown")


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen unavailable")
def test_commit_tree_honors_git_signing_and_verifies_trust(repo: Path, tmp_path: Path, capsys):
    key = tmp_path / "audit-signing-key"
    allowed = configure_ssh_signing(repo, key, "storage@example.test")
    store = GitStore(repo)
    store.init()
    Conductor(repo).create_intent(
        {"author": "alice", "problem": "Need export", "proposed_outcome": "Export records"}
    )
    report = store.verify_audit_signatures()
    assert report["checked"] == report["valid"] == 2
    assert report["all_inspected_signed_and_valid"]
    assert all(item["signature"] == "valid" for item in report["commits"])
    latest = store.verify_audit_signatures(limit=1)
    assert latest["checked"] == 1
    assert latest["total_metadata_commits"] == 2
    assert latest["truncated"]
    assert main(["--repo", str(repo), "audit", "verify", "--require-signatures"]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] == 2
    allowed.write_text("")
    untrusted = store.verify_audit_signatures()
    assert untrusted["invalid"] == 2
    assert not untrusted["all_inspected_signed_and_valid"]
    assert main(["--repo", str(repo), "audit", "verify"]) == 1
    assert json.loads(capsys.readouterr().out)["invalid"] == 2


def test_missing_signing_key_rolls_back_metadata_transaction(repo: Path, tmp_path: Path):
    store = GitStore(repo)
    head = store.init().version
    before = snapshot(repo / ".backbone")
    git(repo, "config", "gpg.format", "ssh")
    git(repo, "config", "user.signingkey", str(tmp_path / "missing-key"))
    git(repo, "config", "commit.gpgsign", "true")
    with pytest.raises(StorageError, match="commit-tree failed"):
        store.mutate(
            lambda state: state.sessions.update({"alice": {"active": True}}),
            "backbone: signed change",
        )
    assert store.read().version == head
    assert snapshot(repo / ".backbone") == before
    git(repo, "config", "commit.gpgsign", "maybe")
    with pytest.raises(StorageError, match="Invalid Git commit.gpgsign"):
        store.mutate(lambda state: state.sessions.update({"bob": {"active": True}}), "again")
    assert store.read().version == head


def test_unrelated_staged_and_unstaged_changes_preserved(repo: Path):
    file = repo / "application.txt"
    file.write_text("original\n")
    git(repo, "add", "application.txt")
    git(repo, "commit", "-m", "application baseline")
    file.write_text("staged application work\n")
    git(repo, "add", "application.txt")
    file.write_text("unstaged application work\n")
    before = git(repo, "diff", "--cached")
    store = GitStore(repo)
    store.init()
    increment_counter(str(repo))
    assert git(repo, "show", "HEAD:application.txt") == "original"
    assert git(repo, "diff", "--cached") == before
    assert file.read_text() == "unstaged application work\n"
    assert git(repo, "diff", "--name-only", "HEAD~2", "HEAD").splitlines() == [
        ".backbone/BACKBONE.md",
        ".backbone/sessions/" + next((repo / ".backbone/sessions").iterdir()).name,
        ".backbone/state.json",
    ]


def test_initial_commit_does_not_include_existing_staged_files(repo: Path):
    (repo / "uncommitted.txt").write_text("keep staged")
    git(repo, "add", "uncommitted.txt")
    GitStore(repo).init()
    assert "uncommitted.txt" not in git(repo, "ls-tree", "--name-only", "HEAD").splitlines()
    assert git(repo, "diff", "--cached", "--name-only") == "uncommitted.txt"


@pytest.mark.parametrize("failure_command", ["commit-tree", "update-ref"])
def test_commit_failure_rolls_back_files_head_and_index(
    repo: Path, monkeypatch, failure_command: str
):
    store = GitStore(repo)
    original_version = store.init().version
    original_files = snapshot(repo / ".backbone")
    (repo / "staged.txt").write_text("owner work")
    git(repo, "add", "staged.txt")
    original_index = (repo / ".git/index").read_bytes()
    original_git = store._git

    def fail(*args, **kwargs):
        if args[0] == failure_command:
            raise StorageError("simulated Git failure")
        return original_git(*args, **kwargs)

    monkeypatch.setattr(store, "_git", fail)
    with pytest.raises(StorageError, match="simulated"):
        store.mutate(lambda state: state.sessions.update({"new": {"value": 1}}), "backbone: fail")
    assert snapshot(repo / ".backbone") == original_files
    assert (repo / ".git/index").read_bytes() == original_index
    assert git(repo, "rev-parse", "HEAD") == original_version
    assert not (repo / ".git/index.lock").exists()


def test_callback_exception_writes_nothing(repo: Path):
    store = GitStore(repo)
    version = store.init().version

    def fail(state):
        state.sessions["partial"] = {"value": 2}
        raise ValueError("callback failed")

    with pytest.raises(ValueError, match="callback failed"):
        store.mutate(fail, "backbone: fail")
    assert store.read().version == version
    assert not store.read().sessions


def test_external_metadata_commit_cannot_be_overwritten_by_stale_callback(repo: Path):
    store = GitStore(repo)
    store.init()

    def change(state):
        state.sessions["ours"] = {"value": 1}
        # External Git users do not acquire the application's file lock.
        path = repo / ".backbone/state.json"
        external = json.loads(path.read_text())
        external["sessions"]["external"] = {"value": 2}
        path.write_text(json.dumps(external))
        git(repo, "add", ".backbone/state.json")
        git(repo, "commit", "-m", "External metadata update")

    with pytest.raises(StorageError, match="changed during"):
        store.mutate(change, "backbone: stale mutation")
    assert store.read().sessions == {"external": {"value": 2}}
    assert git(repo, "log", "-1", "--format=%s") == "External metadata update"


def test_external_application_commit_can_coexist_with_metadata_mutation(repo: Path):
    store = GitStore(repo)
    store.init()

    def change(state):
        state.sessions["ours"] = {"value": 1}
        (repo / "source.txt").write_text("External source change\n")
        git(repo, "add", "source.txt")
        git(repo, "commit", "-m", "External application update")

    store.mutate(change, "backbone: compatible mutation")
    assert store.read().sessions == {"ours": {"value": 1}}
    assert git(repo, "show", "HEAD:source.txt") == "External source change"
    assert git(repo, "log", "-1", "--format=%s", "HEAD~") == "External application update"


def test_cross_process_lock_prevents_lost_updates(repo: Path):
    store = GitStore(repo)
    store.init()
    with ProcessPoolExecutor(max_workers=3) as pool:
        list(pool.map(increment_counter, [str(repo)] * 6))
    assert store.read().sessions["counter"]["value"] == 6
    assert len(store.log()) == 7
    assert git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("dirty_kind", ["modified", "staged", "untracked", "ignored", "deleted"])
def test_dirty_backbone_is_rejected(repo: Path, dirty_kind: str):
    store = GitStore(repo)
    store.init()
    target = repo / ".backbone/state.json"
    if dirty_kind in {"modified", "staged"}:
        target.write_text(target.read_text() + "\n")
        if dirty_kind == "staged":
            git(repo, "add", ".backbone")
    elif dirty_kind == "deleted":
        target.unlink()
    else:
        if dirty_kind == "ignored":
            (repo / ".gitignore").write_text(".backbone/unknown.txt\n")
        (repo / ".backbone/unknown.txt").write_text("uncommitted")
    with pytest.raises(StorageError, match="uncommitted changes"):
        store.mutate(lambda state: None, "backbone: no-op")
    with pytest.raises(StorageError, match="uncommitted changes"):
        store.read()


@pytest.mark.parametrize("nested", [False, True])
def test_symlink_escape_is_rejected(repo: Path, tmp_path: Path, nested: bool):
    outside = tmp_path / "outside"
    outside.mkdir()
    store = GitStore(repo)
    if nested:
        store.init()
        (repo / ".backbone/escape").symlink_to(outside, target_is_directory=True)
    else:
        (repo / ".backbone").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageError, match="[Ss]ymbolic link"):
        store.init()
    assert list(outside.iterdir()) == []


def test_arbitrary_session_keys_cannot_escape_repository(repo: Path, tmp_path: Path):
    store = GitStore(repo)
    store.init()
    store.mutate(
        lambda state: state.sessions.update({"../../escaped": {"x": 1}}), "backbone: session"
    )
    assert store.read().sessions["../../escaped"] == {"x": 1}
    assert not (tmp_path / "escaped.md").exists()
    assert len(list((repo / ".backbone/sessions").glob("session-*.md"))) == 1


def test_index_lock_is_respected(repo: Path):
    store = GitStore(repo)
    first = store.init()
    lock = repo / ".git/index.lock"
    lock.write_text("another Git command")
    with pytest.raises(StorageError, match="index is locked"):
        store.mutate(lambda state: state.sessions.update({"x": {}}), "backbone: locked")
    assert lock.read_text() == "another Git command"
    assert store.read().version == first.version


def test_versions_follow_backbone_not_application_commits(repo: Path):
    store = GitStore(repo)
    version = store.init().version
    (repo / "application.py").write_text("print('hello')\n")
    git(repo, "add", "application.py")
    git(repo, "commit", "-m", "application work")
    assert git(repo, "rev-parse", "HEAD") != version
    assert store.read().version == version
    assert len(store.log()) == 1


def test_stale_generated_views_are_removed(repo: Path):
    store = GitStore(repo)
    store.init()
    intent = Intent(author="alice", problem="x", proposed_outcome="y")
    store.mutate(lambda state: state.intents.update({intent.id: intent}), "backbone: create")
    store.mutate(lambda state: state.intents.pop(intent.id), "backbone: discard")
    assert not (repo / f".backbone/intents/{intent.id}.md").exists()
    assert not store.read().intents


def test_diff_reports_whitespace_and_validates_revisions(repo: Path):
    store = GitStore(repo)
    store.init()
    git(repo, "checkout", "-b", "feature")
    (repo / "bad.txt").write_text("trailing space  \n")
    git(repo, "add", "bad.txt")
    git(repo, "commit", "-m", "whitespace issue")
    result = store.check_diff("main", "feature")
    assert result["ok"] is False
    assert result["changed_paths"] == ["bad.txt"]
    assert "trailing whitespace" in result["detail"]
    assert store.check_diff("main", "main")["ok"] is True
    for revision in ("--output=/tmp/unwanted", "does-not-exist"):
        with pytest.raises(StorageError, match="Git revision"):
            store.check_diff("main", revision)


def test_explicit_sync_pushes_to_local_remote(repo: Path, tmp_path: Path):
    store = GitStore(repo)
    state = store.init()
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(repo, "remote", "add", "origin", str(remote))
    assert not (remote / "refs/heads/main").exists()
    result = store.sync()
    assert result["ok"]
    assert result["branch"] == "main"
    assert git(remote, "rev-parse", "main") == state.version
    with pytest.raises(StorageError, match="Unknown Git remote"):
        store.sync("--force")
    with pytest.raises(StorageError, match="Invalid destination branch"):
        store.sync(branch="main:other")


def clone_pair(repo: Path, tmp_path: Path) -> tuple[GitStore, GitStore, Path]:
    first = GitStore(repo)
    first.init()
    remote = tmp_path / "shared.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(repo, "remote", "add", "origin", str(remote))
    first.sync()
    second_path = tmp_path / "second"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(second_path)],
        check=True,
        capture_output=True,
    )
    git(second_path, "config", "user.name", "Second Clone")
    git(second_path, "config", "user.email", "second@example.test")
    return first, GitStore(second_path), remote


def test_refresh_fast_forwards_peer_metadata_and_preserves_history(repo: Path, tmp_path: Path):
    first, second, remote = clone_pair(repo, tmp_path)
    baseline = second.read().version
    first.mutate(lambda state: state.sessions.update({"alice": {"value": 1}}), "alice update")
    first.sync()
    result = Conductor(second.root).refresh()
    assert result["status"] == "fast_forwarded"
    assert result["local_version"] == baseline
    assert result["remote_version"] == first.read().version
    assert result["version"] == first.read().version
    assert second.read().sessions == {"alice": {"value": 1}}
    assert git(second.root, "rev-parse", "HEAD") == git(remote, "rev-parse", "main")
    assert git(second.root, "status", "--porcelain") == ""
    assert git(second.root, "for-each-ref", "--format=%(refname)", "refs/backbone/fetch") == ""
    assert second.refresh()["status"] == "up_to_date"


def test_refresh_reports_local_ahead_and_divergent_objects_without_rewriting(
    repo: Path, tmp_path: Path
):
    first, second, remote = clone_pair(repo, tmp_path)
    second.mutate(lambda state: state.sessions.update({"bob": {"value": 2}}), "bob update")
    local_head = git(second.root, "rev-parse", "HEAD")
    ahead = second.refresh()
    assert ahead["status"] == "local_ahead"
    assert ahead["updated"] is False
    assert git(second.root, "rev-parse", "HEAD") == local_head

    first.mutate(lambda state: state.sessions.update({"alice": {"value": 1}}), "alice update")
    first.sync()
    remote_head = git(remote, "rev-parse", "main")
    divergent = second.refresh()
    assert divergent["status"] == "diverged"
    assert divergent["updated"] is False
    assert divergent["local_objects"]["sessions"] == ["bob"]
    assert divergent["remote_objects"]["sessions"] == ["alice"]
    assert divergent["overlapping_objects"]["sessions"] == []
    assert ".backbone/state.json" in divergent["overlapping_paths"]
    assert git(second.root, "rev-parse", "HEAD") == local_head
    assert git(remote, "rev-parse", "main") == remote_head
    with pytest.raises(StorageError, match="failed"):
        second.sync()


def test_refresh_rejects_dirty_worktree_before_fast_forward(repo: Path, tmp_path: Path):
    first, second, _ = clone_pair(repo, tmp_path)
    first.mutate(lambda state: state.sessions.update({"alice": {}}), "alice update")
    first.sync()
    before = git(second.root, "rev-parse", "HEAD")
    (second.root / "notes.txt").write_text("keep this local file\n")
    with pytest.raises(StorageError, match="clean worktree"):
        second.refresh()
    assert git(second.root, "rev-parse", "HEAD") == before
    assert (second.root / "notes.txt").read_text() == "keep this local file\n"


def test_refresh_rejects_invalid_remote_state_before_checkout(repo: Path, tmp_path: Path):
    first, second, _ = clone_pair(repo, tmp_path)
    before = git(second.root, "rev-parse", "HEAD")
    (first.root / ".backbone/state.json").write_text('{"invalid": true}\n')
    git(first.root, "add", ".backbone/state.json")
    git(first.root, "commit", "-m", "invalid external metadata")
    git(first.root, "push", "origin", "main")
    with pytest.raises(StorageError, match="Invalid Backbone state"):
        second.refresh()
    assert git(second.root, "rev-parse", "HEAD") == before
    assert second.read().version == before


def test_refresh_requires_current_branch(repo: Path, tmp_path: Path):
    _, second, _ = clone_pair(repo, tmp_path)
    with pytest.raises(StorageError, match="checked-out branch"):
        second.refresh(branch="other")


def test_reconcile_disjoint_metadata_keeps_both_parents_and_regenerates_conflicts(
    repo: Path, tmp_path: Path
):
    first, second, remote = clone_pair(repo, tmp_path)
    left = Conductor(first.root).create_intent(
        {
            "id": "intent-left",
            "author": "alice",
            "problem": "Left plan",
            "proposed_outcome": "Left work",
            "affected_paths": ["shared.py"],
        }
    )
    right = Conductor(second.root).create_intent(
        {
            "id": "intent-right",
            "author": "bob",
            "problem": "Right plan",
            "proposed_outcome": "Right work",
            "affected_paths": ["shared.py"],
        }
    )
    first.sync()
    inspection = second.refresh()
    assert inspection["status"] == "diverged"
    result = Conductor(second.root).reconcile(
        inspection["local_head"],
        inspection["remote_head"],
        "owner",
        "Reviewed independent intents; coordinate the shared path",
    )
    assert result["status"] == "reconciled"
    assert result["parents"] == [inspection["local_head"], inspection["remote_head"]]
    assert git(second.root, "show", "-s", "--format=%P", "HEAD") == " ".join(result["parents"])
    merged = second.read()
    assert set(merged.intents) == {left["id"], right["id"]}
    assert merged.parent_version == inspection["local_version"]
    assert merged.merged_parent_version == inspection["remote_version"]
    assert second.verify_current_snapshot()["ok"]
    history = second.verify_audit_history(limit=20)
    assert history["ok"]
    assert {result["commit"], *result["parents"]} <= {item["commit"] for item in history["commits"]}
    assert result["version"] == result["commit"] == merged.version
    assert any(not conflict.resolved for conflict in merged.conflicts.values())
    assert git(second.root, "status", "--porcelain") == ""
    assert git(second.root, "for-each-ref", "--format=%(refname)", "refs/backbone/fetch") == ""
    second.sync()
    assert git(remote, "rev-parse", "main") == result["commit"]
    assert first.refresh()["status"] == "fast_forwarded"
    assert set(first.read().intents) == {left["id"], right["id"]}
    Conductor(first.root).create_intent(
        {"id": "intent-later", "author": "owner", "problem": "Later", "proposed_outcome": "Later"}
    )
    assert first.read().merged_parent_version is None


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen unavailable")
def test_reconcile_signs_merge_commit_when_enabled(repo: Path, tmp_path: Path):
    first, second, _ = clone_pair(repo, tmp_path)
    first.mutate(lambda state: state.sessions.update({"alice": {"value": 1}}), "alice update")
    second.mutate(lambda state: state.sessions.update({"bob": {"value": 2}}), "bob update")
    first.sync()
    inspection = second.refresh()
    assert inspection["status"] == "diverged"
    configure_ssh_signing(second.root, tmp_path / "reconcile-key", "second@example.test")
    result = Conductor(second.root).reconcile(
        inspection["local_head"], inspection["remote_head"], "owner", "Reviewed both updates"
    )
    assert result["status"] == "reconciled"
    assert second.verify_audit_signatures(limit=1)["valid"] == 1


def test_reconcile_same_object_changes_require_review(repo: Path, tmp_path: Path):
    first, second, _ = clone_pair(repo, tmp_path)
    intent = Conductor(first.root).create_intent(
        {"id": "intent-shared", "author": "owner", "problem": "Initial", "proposed_outcome": "Work"}
    )
    first.sync()
    second.refresh()
    first_version = first.read().version
    second_version = second.read().version
    Conductor(first.root).revise_intent(
        intent["id"], {"problem": "First edit"}, "alice", first_version
    )
    Conductor(second.root).revise_intent(
        intent["id"], {"problem": "Second edit"}, "bob", second_version
    )
    first.sync()
    inspection = second.refresh()
    result = second.reconcile(
        "origin", None, inspection["local_head"], inspection["remote_head"], "owner", "Review"
    )
    assert result["status"] == "requires_review"
    assert result["reason"] == "object_conflicts"
    assert result["objects"]["intents"] == [intent["id"]]
    assert git(second.root, "rev-parse", "HEAD") == inspection["local_head"]
    resolved = second.reconcile(
        "origin",
        None,
        inspection["local_head"],
        inspection["remote_head"],
        "owner",
        "Reviewed both revisions and selected the remote wording",
        {"intents": {intent["id"]: {"source": "remote"}}},
    )
    assert resolved["status"] == "reconciled"
    assert second.read().intents[intent["id"]].problem == "First edit"
    assert resolved["parents"] == [inspection["local_head"], inspection["remote_head"]]
    assert '"intents/intent-shared": "remote"' in git(second.root, "show", "-s", "--format=%B")


def test_authenticated_http_reconcile_attributes_merge_commit(repo: Path, tmp_path: Path):
    first, second, _ = clone_pair(repo, tmp_path)
    first.mutate(lambda state: state.sessions.update({"alice": {"value": 1}}), "alice update")
    second.mutate(lambda state: state.sessions.update({"bob": {"value": 2}}), "bob update")
    first.sync()
    inspection = second.refresh()
    credentials = tmp_path / "credentials.json"
    token = create_token_file(credentials, second.root, "owner", [])[0]["token"]
    with TestClient(create_app(second.root, auth_file=credentials)) as client:
        response = client.post(
            "/reconcile",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "local_head": inspection["local_head"],
                "remote_head": inspection["remote_head"],
                "author": "owner",
                "rationale": "Reviewed independent session updates",
            },
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "reconciled"
    message = git(second.root, "show", "-s", "--format=%B", "HEAD")
    assert "Backbone-HTTP-Principal: owner" in message
    assert "Backbone-HTTP-Role: admin" in message
    assert token not in message
    assert second.log(limit=1)[0]["http_principal"] == "owner"


def test_reconcile_reviewed_object_value_merges_both_edits_via_cli(
    repo: Path, tmp_path: Path, capsys
):
    first, second, _ = clone_pair(repo, tmp_path)
    intent = Conductor(first.root).create_intent(
        {"id": "intent-shared", "author": "owner", "problem": "Initial", "proposed_outcome": "Work"}
    )
    first.sync()
    second.refresh()
    Conductor(first.root).revise_intent(
        intent["id"], {"problem": "Remote problem"}, "alice", first.read().version
    )
    Conductor(second.root).revise_intent(
        intent["id"], {"proposed_outcome": "Local outcome"}, "bob", second.read().version
    )
    first.sync()
    inspection = second.refresh()
    resolved_value = second.read().intents[intent["id"]].model_dump(mode="json")
    resolved_value["problem"] = "Remote problem"
    bad_value = {**resolved_value, "id": "other-intent"}
    with pytest.raises(ValueError, match="must match object id"):
        second.reconcile(
            "origin",
            None,
            inspection["local_head"],
            inspection["remote_head"],
            "owner",
            "Reviewed",
            {"intents": {intent["id"]: {"value": bad_value}}},
        )
    with pytest.raises(StorageError, match="must match competing objects"):
        second.reconcile(
            "origin",
            None,
            inspection["local_head"],
            inspection["remote_head"],
            "owner",
            "Reviewed",
            {"intents": {"wrong-id": {"source": "local"}}},
        )
    assert git(second.root, "rev-parse", "HEAD") == inspection["local_head"]
    resolution_file = tmp_path / "resolutions.json"
    resolution_file.write_text(
        json.dumps({"intents": {intent["id"]: {"value": resolved_value}}}), encoding="utf-8"
    )
    assert (
        main(
            [
                "--repo",
                str(second.root),
                "reconcile",
                "--local-head",
                inspection["local_head"],
                "--remote-head",
                inspection["remote_head"],
                "--author",
                "owner",
                "--rationale",
                "Reviewed both fields",
                "--resolutions-file",
                str(resolution_file),
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "reconciled"
    merged = second.read().intents[intent["id"]]
    assert merged.problem == "Remote problem"
    assert merged.proposed_outcome == "Local outcome"
    assert result["parents"] == [inspection["local_head"], inspection["remote_head"]]
    assert git(second.root, "status", "--porcelain") == ""


def test_reconcile_rejects_code_changes_and_stale_heads(repo: Path, tmp_path: Path):
    first, second, _ = clone_pair(repo, tmp_path)
    (first.root / "app.py").write_text("print('from first')\n")
    git(first.root, "add", "app.py")
    git(first.root, "commit", "-m", "first code")
    second.mutate(lambda state: state.sessions.update({"bob": {}}), "second metadata")
    first.sync()
    inspection = second.refresh()
    with pytest.raises(StorageError, match="Remote HEAD changed"):
        second.reconcile("origin", None, inspection["local_head"], "0" * 40, "owner", "Review")
    result = second.reconcile(
        "origin", None, inspection["local_head"], inspection["remote_head"], "owner", "Review"
    )
    assert result["status"] == "requires_review"
    assert result["reason"] == "code_changes"
    assert result["code_paths"] == ["app.py"]
    assert git(second.root, "rev-parse", "HEAD") == inspection["local_head"]


def test_reconcile_detects_code_source_of_rename_into_backbone(repo: Path, tmp_path: Path):
    first, second, _ = clone_pair(repo, tmp_path)
    (first.root / "app.py").write_text("print('baseline')\n")
    git(first.root, "add", "app.py")
    git(first.root, "commit", "-m", "baseline code")
    first.sync()
    second.refresh()
    git(first.root, "mv", "app.py", ".backbone/extra.md")
    git(first.root, "commit", "-m", "move code into metadata directory")
    second.mutate(lambda state: state.sessions.update({"bob": {}}), "second metadata")
    first.sync()
    inspection = second.refresh()
    result = second.reconcile(
        "origin", None, inspection["local_head"], inspection["remote_head"], "owner", "Review"
    )
    assert result["status"] == "requires_review"
    assert result["reason"] == "code_changes"
    assert result["code_paths"] == ["app.py"]


def test_reconcile_rejects_two_active_tasks_for_one_intent(repo: Path, tmp_path: Path):
    first, second, _ = clone_pair(repo, tmp_path)
    intent = Conductor(first.root).create_intent(
        {"id": "intent-work", "author": "owner", "problem": "Work", "proposed_outcome": "Done"}
    )
    Conductor(first.root).transition_intent(intent["id"], "accepted")
    first.sync()
    second.refresh()
    Conductor(first.root).dispatch_task(intent["id"], "alice")
    Conductor(second.root).dispatch_task(intent["id"], "bob")
    first.sync()
    inspection = second.refresh()
    with pytest.raises(StorageError, match="multiple active tasks"):
        second.reconcile(
            "origin", None, inspection["local_head"], inspection["remote_head"], "owner", "Review"
        )
    assert git(second.root, "rev-parse", "HEAD") == inspection["local_head"]


def test_reconcile_rejects_parent_cycle_across_independent_edits(repo: Path, tmp_path: Path):
    first, second, _ = clone_pair(repo, tmp_path)
    conductor = Conductor(first.root)
    for intent_id in ("intent-a", "intent-b"):
        conductor.create_intent(
            {"id": intent_id, "author": "owner", "problem": intent_id, "proposed_outcome": "Work"}
        )
    first.sync()
    second.refresh()
    Conductor(first.root).revise_intent(
        "intent-a", {"parent_intent": "intent-b"}, "alice", first.read().version
    )
    Conductor(second.root).revise_intent(
        "intent-b", {"parent_intent": "intent-a"}, "bob", second.read().version
    )
    first.sync()
    inspection = second.refresh()
    with pytest.raises(StorageError, match="parent cycle"):
        second.reconcile(
            "origin", None, inspection["local_head"], inspection["remote_head"], "owner", "Review"
        )
    assert git(second.root, "rev-parse", "HEAD") == inspection["local_head"]


def test_diff_includes_rename_source_and_destination_for_scope_checks(repo: Path):
    store = GitStore(repo)
    store.init()
    (repo / "protected.py").write_text("def greeting():\n    return 'hello'\n")
    git(repo, "add", "protected.py")
    git(repo, "commit", "-m", "protected source")
    git(repo, "checkout", "-b", "feature")
    git(repo, "mv", "protected.py", "allowed.py")
    git(repo, "commit", "-m", "move protected source")
    result = store.check_diff("main", "feature")
    assert result["changed_paths"] == ["allowed.py", "protected.py"]


def test_store_resolves_subdirectories_and_worktrees(repo: Path, tmp_path: Path):
    store = GitStore(repo)
    store.init()
    nested = repo / "src"
    nested.mkdir()
    assert GitStore(nested).root == repo
    worktree = tmp_path / "worktree"
    git(repo, "worktree", "add", "-b", "parallel", str(worktree))
    other = GitStore(worktree)
    other.mutate(lambda state: state.sessions.update({"parallel": {}}), "backbone: worktree change")
    assert "parallel" in other.read().sessions
    assert "parallel" not in store.read().sessions
    assert git(worktree, "status", "--porcelain") == ""
