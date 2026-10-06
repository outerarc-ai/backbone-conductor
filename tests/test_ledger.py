"""Separate Backbone branch exercises source-code and metadata Git boundaries."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backbone_conductor.api import create_app
from backbone_conductor.cli import main
from backbone_conductor.ledger import attach_ledger, create_ledger, ledger_path, migrate_ledger
from backbone_conductor.service import Conductor
from backbone_conductor.storage import GitStore, StorageError


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def source_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Ledger Tests")
    git(repo, "config", "user.email", "ledger@example.invalid")
    (repo / "README.md").write_text("Source project\n")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "source baseline")
    return repo


def test_create_orphan_ledger_keeps_source_clean_and_uses_own_branch(source_repo: Path):
    source_head = git(source_repo, "rev-parse", "HEAD")
    result = create_ledger(source_repo)
    ledger = GitStore(result["worktree"])
    assert result["branch"] == "backbone"
    assert result["version"] == ledger.read().version
    assert git(source_repo, "rev-parse", "HEAD") == source_head
    assert git(source_repo, "status", "--porcelain") == ""
    assert not (source_repo / ".backbone").exists()
    assert git(ledger.root, "branch", "--show-current") == "backbone"
    assert git(ledger.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [
        ".backbone/BACKBONE.md",
        ".backbone/state.json",
    ]
    assert git(ledger.root, "rev-list", "--parents", "-n", "1", "HEAD").split() == [
        ledger.read().version
    ]
    with pytest.raises(StorageError, match="already exists"):
        create_ledger(source_repo)
    with pytest.raises(StorageError, match="Only"):
        Conductor(source_repo, ledger_branch="other")


def test_separate_ledger_uses_source_branch_for_artifacts_and_merge(source_repo: Path):
    create_ledger(source_repo)
    conductor = Conductor(source_repo, ledger_branch="backbone")
    source_head = git(source_repo, "rev-parse", "HEAD")
    intent = conductor.create_intent(
        {
            "id": "intent-export",
            "author": "owner",
            "problem": "Need export",
            "proposed_outcome": "Export records",
            "affected_paths": ["export.py"],
        }
    )
    conductor.transition_intent(intent["id"], "accepted")
    task = conductor.dispatch_task(intent["id"], "alice")
    assert task["base_ref"] == "main"
    assert task["base_sha"] == source_head
    assert git(source_repo, "rev-parse", "HEAD") == source_head
    conductor.start_task(task["id"], "alice")
    git(source_repo, "switch", "-c", "feature/export")
    (source_repo / "export.py").write_text("def export():\n    return []\n")
    git(source_repo, "add", "export.py")
    git(source_repo, "commit", "-m", "add export")
    git(source_repo, "switch", "main")
    submitted = conductor.submit_artifact(
        "alice",
        {
            "intent_id": intent["id"],
            "branch": "feature/export",
            "base_ref": "main",
            "summary": "Add export",
        },
    )
    assert submitted["accepted"] is True
    assert submitted["checks"]["code"]["status"] == "passed"
    git(source_repo, "merge", "--no-edit", "feature/export")
    packet = conductor.inspect_task(task["id"])
    merged = conductor.merge_task(
        task["id"],
        "owner",
        expected_version=packet["version"],
        expected_target_sha=packet["git"]["target_sha"],
    )
    assert merged["task"]["status"] == "merged"
    archived = conductor.inspect_task(task["id"], full_patch=True)
    assert archived["inspection_kind"] == "approval"
    assert archived["version"] == packet["version"]
    assert archived["git"]["target_sha"] == packet["git"]["target_sha"]
    assert archived["approval"]["decision"]["id"] == merged["review_decision"]["id"]
    assert conductor.state()["intents"][intent["id"]]["status"] == "completed"
    assert not (source_repo / ".backbone").exists()
    assert git(source_repo, "status", "--porcelain") == ""


def test_attach_remote_ledger_and_sync_across_clones(source_repo: Path, tmp_path: Path):
    create_ledger(source_repo)
    first = Conductor(source_repo, ledger_branch="backbone")
    first.create_intent(
        {"id": "intent-first", "author": "owner", "problem": "First", "proposed_outcome": "Work"}
    )
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(source_repo, "remote", "add", "origin", str(remote))
    git(source_repo, "push", "origin", "main")
    first.sync()
    second_repo = tmp_path / "second"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(second_repo)],
        capture_output=True,
        check=True,
    )
    git(second_repo, "config", "user.name", "Second Ledger")
    git(second_repo, "config", "user.email", "second@example.invalid")
    with pytest.raises(StorageError, match="use ledger attach"):
        create_ledger(second_repo)
    attached = attach_ledger(second_repo)
    assert Path(attached["worktree"]) == ledger_path(GitStore(second_repo))
    second = Conductor(second_repo, ledger_branch="backbone")
    assert "intent-first" in second.state()["intents"]
    second.create_intent(
        {"id": "intent-second", "author": "bob", "problem": "Second", "proposed_outcome": "Work"}
    )
    second.sync()
    assert first.refresh()["status"] == "fast_forwarded"
    assert set(first.state()["intents"]) == {"intent-first", "intent-second"}
    assert git(source_repo, "rev-parse", "main") == git(second_repo, "rev-parse", "main")
    assert not (second_repo / ".backbone").exists()


def test_reviewed_collision_reconciles_separate_ledgers(source_repo: Path, tmp_path: Path):
    create_ledger(source_repo)
    first = Conductor(source_repo, ledger_branch="backbone")
    intent = first.create_intent(
        {"id": "intent-shared", "author": "owner", "problem": "Initial", "proposed_outcome": "Work"}
    )
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare")
    git(source_repo, "remote", "add", "origin", str(remote))
    git(source_repo, "push", "origin", "main")
    first.sync()
    second_repo = tmp_path / "second"
    subprocess.run(
        ["git", "clone", "--branch", "main", str(remote), str(second_repo)],
        capture_output=True,
        check=True,
    )
    git(second_repo, "config", "user.name", "Second Ledger")
    git(second_repo, "config", "user.email", "second@example.invalid")
    attach_ledger(second_repo)
    second = Conductor(second_repo, ledger_branch="backbone")
    first.revise_intent(intent["id"], {"problem": "Remote"}, "alice", first.state()["version"])
    second.revise_intent(intent["id"], {"problem": "Local"}, "bob", second.state()["version"])
    first.sync()
    inspection = second.refresh()
    assert inspection["status"] == "diverged"
    result = second.reconcile(
        inspection["local_head"],
        inspection["remote_head"],
        "owner",
        "Reviewed both edits and chose remote wording",
        resolutions={"intents": {intent["id"]: {"source": "remote"}}},
    )
    assert result["status"] == "reconciled"
    assert second.state()["intents"][intent["id"]]["problem"] == "Remote"
    assert git(second_repo, "status", "--porcelain") == ""
    second.sync()
    assert first.refresh()["status"] == "fast_forwarded"
    assert first.state()["intents"][intent["id"]]["problem"] == "Remote"


def test_cli_and_http_accept_separate_ledger(source_repo: Path, capsys):
    assert main(["--repo", str(source_repo), "ledger", "create"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["branch"] == "backbone"
    assert main(["--repo", str(source_repo), "--ledger-branch", "backbone", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["version"] == created["version"]
    with TestClient(create_app(source_repo, ledger_branch="backbone")) as client:
        assert client.get("/state").json()["version"] == created["version"]
    inside = source_repo / "credentials.json"
    inside.write_text("{}")
    inside.chmod(0o600)
    with pytest.raises(ValueError, match="outside the repository"):
        create_app(source_repo, ledger_branch="backbone", auth_file=inside)


def test_create_requires_explicit_migration_of_inline_state(source_repo: Path):
    Conductor(source_repo).initialize()
    with pytest.raises(StorageError, match="explicit migration"):
        create_ledger(source_repo)
    assert git(source_repo, "branch", "--list", "backbone") == ""


def test_migrate_inline_ledger_preserves_audit_ancestry(source_repo: Path, capsys):
    inline = Conductor(source_repo)
    inline.initialize()
    inline.create_intent(
        {"id": "intent-before", "author": "owner", "problem": "Before", "proposed_outcome": "Keep"}
    )
    old_version = inline.state()["version"]
    old_head = git(source_repo, "rev-parse", "HEAD")
    old_audit = git(source_repo, "log", "--format=%H", "--", ".backbone").splitlines()
    assert main(["--repo", str(source_repo), "ledger", "migrate"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["previous_version"] == old_version
    assert result["source_before"] == old_head
    assert result["source_after"] == git(source_repo, "rev-parse", "main")
    assert not (source_repo / ".backbone").exists()
    ledger = GitStore(result["worktree"])
    assert git(ledger.root, "show", "-s", "--format=%P", "HEAD") == old_head
    assert set(git(ledger.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines()) == {
        ".backbone/BACKBONE.md",
        ".backbone/intents/intent-before.md",
        ".backbone/state.json",
    }
    assert ledger.read().version == result["version"]
    assert ledger.read().parent_version == old_version
    assert git(ledger.root, "log", "--format=%H", "--", ".backbone").splitlines()[1:] == old_audit
    assert git(source_repo, "status", "--porcelain") == ""
    separate = Conductor(source_repo, ledger_branch="backbone")
    assert "intent-before" in separate.state()["intents"]
    code_head = git(source_repo, "rev-parse", "HEAD")
    separate.create_intent(
        {"id": "intent-after", "author": "owner", "problem": "After", "proposed_outcome": "Keep"}
    )
    assert git(source_repo, "rev-parse", "HEAD") == code_head
    assert set(separate.state()["intents"]) == {"intent-before", "intent-after"}


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="ssh-keygen unavailable")
def test_migration_signs_both_ledger_and_source_commits(source_repo: Path, tmp_path: Path):
    Conductor(source_repo).initialize()
    key = tmp_path / "migration-key"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
        check=True,
        capture_output=True,
        timeout=20,
    )
    allowed = tmp_path / "allowed-signers"
    allowed.write_text("ledger@example.invalid " + key.with_suffix(".pub").read_text())
    git(source_repo, "config", "gpg.format", "ssh")
    git(source_repo, "config", "user.signingkey", str(key))
    git(source_repo, "config", "gpg.ssh.allowedSignersFile", str(allowed))
    git(source_repo, "config", "commit.gpgsign", "true")

    result = migrate_ledger(source_repo)
    ledger = GitStore(result["worktree"])
    assert ledger.verify_audit_signatures(limit=1)["valid"] == 1
    assert ledger.verify_current_snapshot()["ok"]
    assert ledger.verify_audit_history(limit=20)["ok"]
    git(source_repo, "verify-commit", "HEAD")


def test_migrate_rejects_active_task_and_dirty_source(source_repo: Path):
    inline = Conductor(source_repo)
    inline.initialize()
    intent = inline.create_intent(
        {"id": "intent-work", "author": "owner", "problem": "Work", "proposed_outcome": "Done"}
    )
    inline.transition_intent(intent["id"], "accepted")
    task = inline.dispatch_task(intent["id"], "alice")
    with pytest.raises(StorageError, match="Finish or cancel active tasks"):
        migrate_ledger(source_repo)
    inline.cancel_task(task["id"], "owner", "migrate")
    (source_repo / "untracked.txt").write_text("local work")
    with pytest.raises(StorageError, match="clean source worktree"):
        migrate_ledger(source_repo)
    assert git(source_repo, "branch", "--list", "backbone") == ""
