"""Exercise real Git ledgers through CLI, HTTP, and the MCP wire protocol."""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ToolError

from backbone_conductor.api import create_app
from backbone_conductor.cli import main
from backbone_conductor.mcp_server import (
    COORDINATOR_TOOLS,
    create_coordinator_server,
    create_server,
)
from backbone_conductor.service import Conductor

SOURCE_ROOT = str(Path(__file__).resolve().parents[1] / "src")


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def interface_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Interface Tests")
    git(repo, "config", "user.email", "interfaces@example.invalid")
    (repo / "README.md").write_text("Interface test project\n", encoding="utf-8")
    git(repo, "add", "README.md")
    git(repo, "commit", "-m", "Initial project")
    return repo


def intent_data() -> dict:
    return {
        "author": "alice",
        "problem": "A project needs an export function",
        "proposed_outcome": "Add a deterministic export function",
        "affected_symbols": ["export"],
        "affected_paths": ["export.py"],
    }


def test_cli_initialization_creation_and_readable_failures(interface_repo: Path, capsys) -> None:
    prefix = ["--repo", str(interface_repo)]
    assert main([*prefix, "init"]) == 0
    assert json.loads(capsys.readouterr().out)["schema_version"] == 1
    # Inputs stay outside the repository so they do not alter the user's worktree.
    payload = interface_repo.parent / "intent.json"
    payload.write_text(json.dumps(intent_data()), encoding="utf-8")
    assert main([*prefix, "intent", "create", "--file", str(payload)]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["author"] == "alice"
    assert main([*prefix, "intent", "transition", created["id"], "completed"]) == 1
    error = capsys.readouterr()
    assert error.out == ""
    assert "error" in json.loads(error.err)
    assert main([*prefix, "intent", "transition", "missing", "accepted"]) == 1
    assert "missing" in json.loads(capsys.readouterr().err)["error"]
    assert main([*prefix, "log", "--limit", "0"]) == 1
    assert "limit" in json.loads(capsys.readouterr().err)["error"]


def test_cli_replaces_accepted_intent_with_audited_draft(interface_repo: Path, capsys) -> None:
    prefix = ["--repo", str(interface_repo)]
    assert main([*prefix, "init"]) == 0
    capsys.readouterr()
    original = Conductor(interface_repo).create_intent(intent_data())
    Conductor(interface_repo).transition_intent(original["id"], "accepted")
    version = Conductor(interface_repo).state()["version"]
    patch_file = interface_repo.parent / "replacement.json"
    patch_file.write_text(json.dumps({"problem": "Export needs a new shape"}), encoding="utf-8")
    assert (
        main(
            [
                *prefix,
                "intent",
                "replace",
                original["id"],
                "--file",
                str(patch_file),
                "--author",
                "owner",
                "--reason",
                "Requirements changed",
                "--version",
                version,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["previous"]["status"] == "superseded"
    assert result["replacement"]["supersedes"] == original["id"]


def test_cli_reverts_decision_with_reason_and_version(interface_repo: Path, capsys) -> None:
    service = Conductor(interface_repo)
    service.initialize()
    decision = service.log_decision(
        {
            "author": "alice",
            "decision_type": "api",
            "summary": "Remove the old endpoint",
            "rationale": "Simpler interface",
        }
    )
    service.transition_decision(decision["id"], "accepted")
    version = service.state()["version"]
    prefix = ["--repo", str(interface_repo)]
    assert main([*prefix, "decision", "transition", decision["id"], "reverted"]) == 1
    assert "audited author" in json.loads(capsys.readouterr().err)["error"]
    assert (
        main(
            [
                *prefix,
                "revert",
                decision["id"],
                "--author",
                "bob",
                "--rationale",
                "Existing clients still need it",
                "--version",
                version,
            ]
        )
        == 0
    )
    reverted = json.loads(capsys.readouterr().out)
    assert reverted["status"] == "reverted"
    assert reverted["reversion"]["reviewed_version"] == version
    assert reverted["reversion"]["rationale"] == "Existing clients still need it"


def test_cli_resolves_conflict_against_observed_version(interface_repo: Path, capsys) -> None:
    service = Conductor(interface_repo)
    service.initialize()
    service.create_intent({**intent_data(), "id": "intent-left"})
    service.create_intent(
        {
            **intent_data(),
            "id": "intent-right",
            "author": "bob",
            "problem": "Competing export change",
        }
    )
    conflict = next(item for item in service.state()["conflicts"].values() if not item["resolved"])
    version = service.state()["version"]
    assert (
        main(
            [
                "--repo",
                str(interface_repo),
                "conflict",
                "resolve",
                conflict["id"],
                "--author",
                "owner",
                "--action",
                "coordinate",
                "--rationale",
                "Agree compatible scopes",
                "--version",
                version,
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["conflict"]["resolution"]["reviewed_version"] == version


def test_cli_returns_read_only_semantic_conflict_advice(
    interface_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    from backbone_conductor.runtime import DSHReviewer

    service = Conductor(interface_repo)
    service.initialize()
    first = service.create_intent(intent_data())
    second = service.create_intent(
        {
            "author": "bob",
            "problem": "Change the export contract",
            "proposed_outcome": "Replace the return type",
        }
    )
    monkeypatch.setattr(
        DSHReviewer,
        "advise_conflict",
        lambda _self, _context: {
            "verdict": "uncertain",
            "rationale": "The plans need human comparison",
            "evidence": [],
            "coordination": [],
        },
    )
    head = git(interface_repo, "rev-parse", "HEAD")
    assert (
        main(
            [
                "--repo",
                str(interface_repo),
                "conflict",
                "advise",
                first["id"],
                second["id"],
                "--dsh-home",
                str(tmp_path / "private-advice-home"),
                "--model",
                "test-model",
            ]
        )
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["advisory"] and result["human_review_required"]
    assert result["model_advice"]["verdict"] == "uncertain"
    assert result["pair"] == [first["id"], second["id"]]
    assert git(interface_repo, "rev-parse", "HEAD") == head


def test_cli_reviews_draft_with_rationale(interface_repo: Path, capsys) -> None:
    prefix = ["--repo", str(interface_repo)]
    assert main([*prefix, "init"]) == 0
    capsys.readouterr()
    original = Conductor(interface_repo).create_intent(intent_data())
    version = Conductor(interface_repo).state()["version"]
    assert (
        main(
            [
                *prefix,
                "intent",
                "review",
                original["id"],
                "--outcome",
                "accepted",
                "--author",
                "carol",
                "--rationale",
                "Clear scope",
                "--version",
                version,
            ]
        )
        == 0
    )
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["status"] == "accepted"
    assert reviewed["reviews"][-1]["reviewed_version"] == version


def test_cli_rejects_non_object_json_and_exports_schema(interface_repo: Path, capsys) -> None:
    payload = interface_repo.parent / "invalid.json"
    payload.write_text("[]", encoding="utf-8")
    assert main(["--repo", str(interface_repo), "intent", "create", "--file", str(payload)]) == 1
    assert "object" in json.loads(capsys.readouterr().err)["error"]
    assert main(["schema"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert "Intent" in schema["$defs"]
    assert "tasks" in schema["properties"]


def test_cli_runs_member_scoped_dsh_entry_with_prompt_file(
    interface_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    from backbone_conductor.dsh_agent import DSHMemberRunner

    Conductor(interface_repo).initialize()
    workspace = tmp_path / "coding-worktree"
    workspace.mkdir()
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Read my task before coding", encoding="utf-8")
    observed = {}

    def run(self, prompt, *, session_id=None):
        observed.update(member=self.member, workspace=self.workspace, prompt=prompt)
        return {"member": self.member, "final_response": "Ready", "session_id": "session-1"}

    monkeypatch.setattr(DSHMemberRunner, "run", run)
    assert (
        main(
            [
                "--repo",
                str(interface_repo),
                "dsh",
                "--member",
                "alice",
                "--workspace",
                str(workspace),
                "--dsh-home",
                str(tmp_path / "dsh-home"),
                "--model",
                "test-model",
                "--prompt-file",
                str(prompt_file),
            ]
        )
        == 0
    )
    assert observed == {
        "member": "alice",
        "workspace": workspace,
        "prompt": "Read my task before coding",
    }
    assert json.loads(capsys.readouterr().out)["session_id"] == "session-1"


def test_cli_runs_member_prompts_in_one_session(
    interface_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    from backbone_conductor.dsh_agent import DSHMemberRunner

    Conductor(interface_repo).initialize()
    workspace = tmp_path / "coding-worktree"
    workspace.mkdir()
    prompts_file = tmp_path / "turns.json"
    prompts_file.write_text(json.dumps(["Read the task", "Recheck the decisions"]))
    observed = {}

    def run_turns(self, prompts, *, session_id=None):
        observed.update(member=self.member, prompts=prompts, session_id=session_id)
        return {"member": self.member, "session_id": "session-1", "turns": []}

    monkeypatch.setattr(DSHMemberRunner, "run_turns", run_turns)
    args = [
        "--repo",
        str(interface_repo),
        "dsh",
        "--member",
        "alice",
        "--workspace",
        str(workspace),
        "--dsh-home",
        str(tmp_path / "dsh-home"),
        "--model",
        "test-model",
        "--prompts-file",
        str(prompts_file),
    ]
    assert main(args) == 0
    assert observed == {
        "member": "alice",
        "prompts": ["Read the task", "Recheck the decisions"],
        "session_id": None,
    }
    assert json.loads(capsys.readouterr().out)["turns"] == []

    prompts_file.write_text(json.dumps(["Read the task", " "]))
    assert main(args) == 1
    assert "nonempty JSON array" in json.loads(capsys.readouterr().err)["error"]


def test_cli_streams_member_turns_until_quit(
    interface_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    from backbone_conductor.dsh_agent import DSHMemberRunner

    Conductor(interface_repo).initialize()
    workspace = tmp_path / "coding-worktree"
    workspace.mkdir()
    observed = []

    def run_turns(self, prompts, *, session_id=None, on_turn=None):
        assert session_id is None
        for prompt in prompts:
            observed.append(prompt)
            on_turn({"session_id": "session-1", "final_response": f"Reply {len(observed)}"})
        return {"session_id": "session-1", "turns": []}

    monkeypatch.setattr(DSHMemberRunner, "run_turns", run_turns)
    command = [
        "--repo",
        str(interface_repo),
        "dsh",
        "--member",
        "alice",
        "--workspace",
        str(workspace),
        "--dsh-home",
        str(tmp_path / "dsh-home"),
        "--model",
        "test-model",
        "--interactive",
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO("\nRead my task\nFollow up\n:quit\nIgnored"))
    assert main(command) == 0
    assert observed == ["Read my task", "Follow up"]
    assert [
        json.loads(line)["final_response"] for line in capsys.readouterr().out.splitlines()
    ] == [
        "Reply 1",
        "Reply 2",
    ]
    monkeypatch.setattr(sys, "stdin", io.StringIO(":quit\n"))
    assert main(command) == 0
    assert capsys.readouterr().out == ""
    assert observed == ["Read my task", "Follow up"]


def test_cli_runs_limited_coordinator_entry(
    interface_repo: Path, tmp_path: Path, monkeypatch, capsys
):
    from backbone_conductor.dsh_agent import DSHCoordinatorRunner

    Conductor(interface_repo).initialize()
    observed = {}

    def run(self, prompt, *, session_id=None):
        observed.update(repo=self.repo, prompt=prompt, session_id=session_id)
        return {"final_response": "Draft prepared", "session_id": "session-1"}

    monkeypatch.setattr(DSHCoordinatorRunner, "run", run)
    assert (
        main(
            [
                "--repo",
                str(interface_repo),
                "conductor",
                "--dsh-home",
                str(tmp_path / "private-dsh-home"),
                "--model",
                "test-model",
                "--prompt",
                "Summarize the current intents",
            ]
        )
        == 0
    )
    assert observed == {
        "repo": interface_repo,
        "prompt": "Summarize the current intents",
        "session_id": None,
    }
    assert json.loads(capsys.readouterr().out)["final_response"] == "Draft prepared"


def test_cli_runs_coordinator_prompts_in_one_session(
    interface_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    from backbone_conductor.dsh_agent import DSHCoordinatorRunner

    Conductor(interface_repo).initialize()
    prompts_file = tmp_path / "coordinator-turns.json"
    prompts_file.write_text(json.dumps(["Read status", "Propose a draft"]))
    observed = {}

    def run_turns(self, prompts, *, session_id=None):
        observed.update(repo=self.repo, prompts=prompts, session_id=session_id)
        return {"session_id": "session-1", "turns": []}

    monkeypatch.setattr(DSHCoordinatorRunner, "run_turns", run_turns)
    assert (
        main(
            [
                "--repo",
                str(interface_repo),
                "conductor",
                "--dsh-home",
                str(tmp_path / "private-dsh-home"),
                "--model",
                "test-model",
                "--prompts-file",
                str(prompts_file),
            ]
        )
        == 0
    )
    assert observed == {
        "repo": interface_repo,
        "prompts": ["Read status", "Propose a draft"],
        "session_id": None,
    }
    assert json.loads(capsys.readouterr().out)["turns"] == []


def test_cli_streams_coordinator_turns_until_eof(
    interface_repo: Path, tmp_path: Path, monkeypatch, capsys
) -> None:
    from backbone_conductor.dsh_agent import DSHCoordinatorRunner

    Conductor(interface_repo).initialize()
    observed = []

    def run_turns(self, prompts, *, session_id=None, on_turn=None):
        for prompt in prompts:
            observed.append(prompt)
            on_turn({"session_id": "coordinator-session", "final_response": "Draft prepared"})
        return {"session_id": "coordinator-session", "turns": []}

    monkeypatch.setattr(DSHCoordinatorRunner, "run_turns", run_turns)
    monkeypatch.setattr(sys, "stdin", io.StringIO("Read status\nPropose draft\n"))
    assert (
        main(
            [
                "--repo",
                str(interface_repo),
                "conductor",
                "--dsh-home",
                str(tmp_path / "private-dsh-home"),
                "--model",
                "test-model",
                "--interactive",
            ]
        )
        == 0
    )
    assert observed == ["Read status", "Propose draft"]
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_cli_routes_remote_dsh_without_a_local_ledger(tmp_path: Path, monkeypatch, capsys) -> None:
    from backbone_conductor.dsh_agent import DSHRemoteMemberRunner

    workspace = tmp_path / "member-workspace"
    workspace.mkdir()
    token_file = tmp_path / "alice.token"
    token_file.write_text("a" * 48)
    token_file.chmod(0o600)
    observed = {}

    def run(self, prompt, *, session_id=None):
        observed.update(member=self.member, url=self.mcp_url, prompt=prompt)
        return {"member": self.member, "final_response": "Ready", "session_id": "session-1"}

    monkeypatch.setattr(DSHRemoteMemberRunner, "run", run)
    command = [
        "dsh",
        "--member",
        "alice",
        "--workspace",
        str(workspace),
        "--dsh-home",
        str(tmp_path / "dsh-home"),
        "--model",
        "test-model",
        "--mcp-url",
        "https://coordinator.example/mcp",
        "--mcp-token-file",
        str(token_file),
        "--prompt",
        "Read my remote task",
    ]
    assert main(command) == 0
    assert observed == {
        "member": "alice",
        "url": "https://coordinator.example/mcp",
        "prompt": "Read my remote task",
    }
    assert json.loads(capsys.readouterr().out)["session_id"] == "session-1"
    assert main(command[:-4] + ["--prompt", "Read my remote task"]) == 1
    assert "mcp-token-file" in json.loads(capsys.readouterr().err)["error"]


def test_cli_streams_remote_member_turns_without_local_ledger(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from backbone_conductor.dsh_agent import DSHRemoteMemberRunner

    workspace = tmp_path / "member-workspace"
    workspace.mkdir()
    token_file = tmp_path / "alice.token"
    token_file.write_text("a" * 48)
    token_file.chmod(0o600)
    observed = []

    def run_turns(self, prompts, *, session_id=None, on_turn=None):
        assert self.mcp_url == "https://coordinator.example/mcp"
        for prompt in prompts:
            observed.append(prompt)
            on_turn({"session_id": "remote-session", "final_response": "Ready"})
        return {"session_id": "remote-session", "turns": []}

    monkeypatch.setattr(DSHRemoteMemberRunner, "run_turns", run_turns)
    monkeypatch.setattr(sys, "stdin", io.StringIO("Read my remote task\nWhat changed?\n:exit\n"))
    assert (
        main(
            [
                "dsh",
                "--member",
                "alice",
                "--workspace",
                str(workspace),
                "--dsh-home",
                str(tmp_path / "dsh-home"),
                "--model",
                "test-model",
                "--mcp-url",
                "https://coordinator.example/mcp",
                "--mcp-token-file",
                str(token_file),
                "--interactive",
            ]
        )
        == 0
    )
    assert observed == ["Read my remote task", "What changed?"]
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_python_module_propagates_failure_exit_code(interface_repo: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "backbone_conductor",
            "--repo",
            str(interface_repo),
            "log",
            "--limit",
            "0",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
        env={**os.environ, "PYTHONPATH": SOURCE_ROOT},
    )
    assert result.returncode == 1
    assert "limit" in json.loads(result.stderr)["error"]
    assert result.stdout == ""


def test_http_lifecycle_and_error_mapping(interface_repo: Path) -> None:
    with TestClient(create_app(interface_repo)) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert client.post("/initialize").status_code == 200
        invalid = client.post("/intents", json={"author": "alice"})
        assert invalid.status_code == 422
        response = client.post("/intents", json=intent_data())
        assert response.status_code == 201
        intent_id = response.json()["id"]
        assert client.get("/intents/missing").status_code == 404
        assert client.get(f"/intents/{intent_id}").json()["status"] == "draft"
        response = client.post(f"/intents/{intent_id}/transition", json={"status": "completed"})
        assert response.status_code == 422
        assert (
            client.post(f"/intents/{intent_id}/transition", json={"status": "accepted"}).status_code
            == 200
        )
        task = client.post("/tasks", json={"intent_id": intent_id, "member_id": "alice"})
        assert task.status_code == 201
        task_id = task.json()["id"]
        assert client.get("/tasks", params={"member_id": "bob"}).json()["tasks"] == []
        assert client.post(f"/tasks/{task_id}/start", json={"member_id": "bob"}).status_code == 403
        assert (
            client.post(f"/tasks/{task_id}/start", json={"member_id": "alice"}).status_code == 200
        )
        assert client.get("/tasks").json()["tasks"][0]["status"] == "in_progress"
        assert client.get("/timeline", params={"limit": 0}).status_code == 422
        assert client.get("/timeline", params={"event_type": "invalid"}).status_code == 422
        assert client.get("/timeline", params={"since": "2026-09-29"}).status_code == 422
        timeline = client.get("/timeline").json()
        assert len(timeline) >= 4
        exact_time = client.get(
            "/timeline",
            params={"since": timeline[0]["timestamp"], "until": timeline[0]["timestamp"]},
        )
        assert exact_time.status_code == 200
        assert exact_time.json()[0]["commit"] == timeline[0]["commit"]
        assert client.get("/schema").status_code == 200
        assert client.get("/openapi.json").status_code == 200


def test_http_replacement_requires_current_version_and_returns_linked_draft(
    interface_repo: Path,
) -> None:
    conductor = Conductor(interface_repo)
    conductor.initialize()
    with TestClient(create_app(interface_repo)) as client:
        original = client.post("/intents", json=intent_data()).json()
        client.post(f"/intents/{original['id']}/transition", json={"status": "accepted"})
        version = conductor.state()["version"]
        payload = {
            "author": "owner",
            "patch": {"problem": "Export needs a new shape"},
            "reason": "Requirements changed",
            "expected_version": version,
        }
        response = client.post(f"/intents/{original['id']}/replace", json=payload)
        assert response.status_code == 201, response.text
        assert response.json()["replacement"]["supersedes"] == original["id"]
        assert client.get(f"/intents/{original['id']}").json()["status"] == "superseded"
        assert client.post(f"/intents/{original['id']}/replace", json=payload).status_code == 422


def test_http_intent_review_records_decision(interface_repo: Path) -> None:
    conductor = Conductor(interface_repo)
    conductor.initialize()
    with TestClient(create_app(interface_repo)) as client:
        original = client.post("/intents", json=intent_data()).json()
        version = conductor.state()["version"]
        response = client.post(
            f"/intents/{original['id']}/review",
            json={
                "author": "carol",
                "outcome": "rejected",
                "rationale": "Scope is unclear",
                "expected_version": version,
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "rejected"
        assert response.json()["reviews"][-1]["reviewed_version"] == version


def test_http_task_submission_binds_path_and_requires_real_merge(interface_repo: Path) -> None:
    conductor = Conductor(interface_repo)
    conductor.initialize()
    intent = conductor.create_intent(intent_data())
    conductor.transition_intent(intent["id"], "accepted")
    task = conductor.dispatch_task(intent["id"], "alice")
    conductor.start_task(task["id"], "alice")
    git(interface_repo, "switch", "-c", "member-export")
    (interface_repo / "export.py").write_text("def export():\n    return []\n", encoding="utf-8")
    git(interface_repo, "add", "export.py")
    git(interface_repo, "commit", "-m", "Add export")
    git(interface_repo, "switch", "main")
    payload = {
        "member_id": "alice",
        "artifact": {"branch": "member-export", "base_ref": "main", "summary": "Added export"},
    }
    with TestClient(create_app(interface_repo)) as client:
        mismatch = {**payload, "artifact": {**payload["artifact"], "intent_id": "wrong"}}
        assert client.post(f"/tasks/{task['id']}/submit", json=mismatch).status_code == 422
        spoof = {**payload, "member_id": "bob"}
        assert client.post(f"/tasks/{task['id']}/submit", json=spoof).status_code == 403
        result = client.post(f"/tasks/{task['id']}/submit", json=payload)
        assert result.status_code == 200, result.text
        packet = client.get(f"/tasks/{task['id']}/inspection").json()
        assert (
            client.post(
                f"/tasks/{task['id']}/merge",
                json={
                    "author": "reviewer",
                    "expected_version": packet["version"],
                    "expected_target_sha": packet["git"]["target_sha"],
                },
            ).status_code
            == 422
        )
        git(interface_repo, "merge", "--no-edit", "member-export")
        packet = client.get(f"/tasks/{task['id']}/inspection").json()
        approved = client.post(
            f"/tasks/{task['id']}/merge",
            json={
                "author": "reviewer",
                "expected_version": packet["version"],
                "expected_target_sha": packet["git"]["target_sha"],
            },
        )
        assert approved.status_code == 200, approved.text
        assert client.get(f"/intents/{intent['id']}").json()["status"] == "completed"


def test_mcp_member_binding_hides_admin_and_rejects_spoofing(interface_repo: Path) -> None:
    Conductor(interface_repo).initialize()

    async def check() -> None:
        server = create_server(interface_repo, "alice")
        names = {tool.name for tool in await server.list_tools()}
        assert {
            "get_my_task",
            "submit_artifact",
            "check_backbone_sync",
            "create_intent",
            "log_decision",
        } <= names
        assert "rebase_task" in names
        assert (
            not {
                "dispatch_task",
                "transition_intent",
                "resolve_conflict",
                "merge_task",
                "inspect_task",
                "revise_intent",
                "replace_intent",
                "review_intent",
                "revert_decision",
                "verify_audit_signatures",
                "cancel_task",
                "refresh_backbone",
                "reconcile_backbone",
            }
            & names
        )
        with pytest.raises(ToolError, match="bound member"):
            await server.call_tool("get_my_task", {"member_id": "bob"})
        with pytest.raises(ToolError, match="bound member"):
            await server.call_tool(
                "create_intent", {"intent_data": {**intent_data(), "author": "bob"}}
            )
        with pytest.raises(ToolError, match="draft"):
            await server.call_tool(
                "create_intent", {"intent_data": {**intent_data(), "status": "accepted"}}
            )
        data = intent_data()
        del data["author"]
        await server.call_tool("create_intent", {"intent_data": data})
        state = Conductor(interface_repo).state()
        assert next(iter(state["intents"].values()))["author"] == "alice"
        admin_tools = await create_server(interface_repo).list_tools()
        admin_names = {tool.name for tool in admin_tools}
        assert {
            "dispatch_task",
            "transition_intent",
            "resolve_conflict",
            "merge_task",
            "inspect_task",
            "revise_intent",
            "replace_intent",
            "review_intent",
            "revert_decision",
            "verify_audit_signatures",
            "cancel_task",
            "refresh_backbone",
            "reconcile_backbone",
        } <= admin_names
        resolve_tool = next(tool for tool in admin_tools if tool.name == "resolve_conflict")
        assert "expected_version" in resolve_tool.inputSchema["required"]
        await create_server(interface_repo).call_tool("verify_audit_signatures", {"limit": 1})
        intent_id = next(iter(state["intents"]))
        Conductor(interface_repo).transition_intent(intent_id, "accepted")
        await create_server(interface_repo).call_tool(
            "replace_intent",
            {
                "intent_id": intent_id,
                "patch": {"problem": "Revised scope"},
                "author": "owner",
                "reason": "Requirements changed",
                "expected_version": Conductor(interface_repo).state()["version"],
            },
        )
        successors = [
            item
            for item in Conductor(interface_repo).state()["intents"].values()
            if item["supersedes"] == intent_id
        ]
        assert len(successors) == 1
        await create_server(interface_repo).call_tool(
            "review_intent",
            {
                "intent_id": successors[0]["id"],
                "outcome": "accepted",
                "reviewer": "carol",
                "rationale": "Replacement scope is clear",
                "expected_version": Conductor(interface_repo).state()["version"],
            },
        )
        assert (
            Conductor(interface_repo).state()["intents"][successors[0]["id"]]["status"]
            == "accepted"
        )
        decision = Conductor(interface_repo).log_decision(
            {
                "author": "owner",
                "decision_type": "api",
                "summary": "Remove the old endpoint",
                "rationale": "Simpler interface",
            }
        )
        Conductor(interface_repo).transition_decision(decision["id"], "accepted")
        version = Conductor(interface_repo).state()["version"]
        await create_server(interface_repo).call_tool(
            "revert_decision",
            {
                "decision_id": decision["id"],
                "author": "carol",
                "rationale": "Compatibility is still needed",
                "expected_version": version,
            },
        )
        reverted = Conductor(interface_repo).state()["decisions"][decision["id"]]
        assert reverted["status"] == "reverted"
        assert reverted["reversion"]["reviewed_version"] == version

    asyncio.run(check())


def test_coordinator_mcp_requires_human_acceptance_before_dispatch(interface_repo: Path) -> None:
    conductor = Conductor(interface_repo)
    conductor.initialize()

    async def check() -> None:
        server = create_coordinator_server(interface_repo)
        names = {tool.name for tool in await server.list_tools()}
        assert names == COORDINATOR_TOOLS
        assert (
            not {
                "review_intent",
                "revert_decision",
                "resolve_conflict",
                "merge_task",
                "transition_intent",
            }
            & names
        )
        with pytest.raises(ToolError, match="human author"):
            await server.call_tool("create_intent", {"intent_data": intent_data()})
        data = intent_data()
        data.pop("author")
        response = await server.call_tool("create_intent", {"intent_data": data})
        created = json.loads(response[0].text)
        assert created["author"] == "conductor-agent"
        assert created["status"] == "draft"
        with pytest.raises(ToolError):
            await server.call_tool(
                "dispatch_task", {"intent_id": created["id"], "member_id": "alice"}
            )
        conductor.review_intent(
            created["id"], "accepted", "owner", "Scope reviewed", conductor.state()["version"]
        )
        response = await server.call_tool(
            "dispatch_task", {"intent_id": created["id"], "member_id": "alice"}
        )
        dispatched = json.loads(response[0].text)
        assert dispatched["member_id"] == "alice"

    asyncio.run(check())


def test_mcp_stdio_protocol_roundtrip(interface_repo: Path) -> None:
    Conductor(interface_repo).initialize()

    async def check() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[
                "-m",
                "backbone_conductor",
                "--repo",
                str(interface_repo),
                "mcp",
                "--member",
                "alice",
            ],
            env={"PYTHONPATH": SOURCE_ROOT},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            assert "get_my_task" in {tool.name for tool in listed.tools}
            result = await session.call_tool("get_my_task", {})
            assert not result.isError
            parsed = json.loads(result.content[0].text)
            assert parsed["member_id"] == "alice"
            assert parsed["tasks"] == []
            denied = await session.call_tool("get_my_task", {"member_id": "bob"})
            assert denied.isError

    asyncio.run(asyncio.wait_for(check(), timeout=20))


def test_cli_revision_and_task_context_rebase(interface_repo: Path, capsys) -> None:
    conductor = Conductor(interface_repo)
    conductor.initialize()
    created = conductor.create_intent(intent_data())
    patch_file = interface_repo.parent / "intent-patch.json"
    patch_file.write_text(json.dumps({"problem": "Export is missing in two modules"}))
    version = conductor.state()["version"]
    prefix = ["--repo", str(interface_repo)]
    assert (
        main(
            [
                *prefix,
                "intent",
                "revise",
                created["id"],
                "--file",
                str(patch_file),
                "--author",
                "owner",
                "--version",
                version,
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["problem"] == "Export is missing in two modules"
    conductor.transition_intent(created["id"], "accepted")
    task = conductor.dispatch_task(created["id"], "alice")
    version = conductor.state()["version"]
    assert (
        main([*prefix, "task", "rebase", task["id"], "--member", "alice", "--version", version])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["task"]["id"] == task["id"]
    assert (
        main(
            [
                *prefix,
                "task",
                "cancel",
                task["id"],
                "--author",
                "owner",
                "--reason",
                "Requirements changed",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["intent"]["status"] == "accepted"


def test_http_revision_rebase_and_cancellation(interface_repo: Path) -> None:
    conductor = Conductor(interface_repo)
    conductor.initialize()
    intent = conductor.create_intent(intent_data())
    with TestClient(create_app(interface_repo)) as client:
        version = client.get("/state").json()["version"]
        revised = client.post(
            f"/intents/{intent['id']}/revise",
            json={
                "patch": {"proposed_outcome": "Add a CSV export"},
                "author": "owner",
                "expected_version": version,
            },
        )
        assert revised.status_code == 200
        assert revised.json()["proposed_outcome"] == "Add a CSV export"
        assert (
            client.post(
                f"/intents/{intent['id']}/revise",
                json={
                    "patch": {"problem": "Stale"},
                    "author": "owner",
                    "expected_version": version,
                },
            ).status_code
            == 422
        )
        assert (
            client.post(
                f"/intents/{intent['id']}/transition", json={"status": "accepted"}
            ).status_code
            == 200
        )
        task = client.post("/tasks", json={"intent_id": intent["id"], "member_id": "alice"}).json()
        version = client.get("/state").json()["version"]
        assert (
            client.post(
                f"/tasks/{task['id']}/rebase",
                json={
                    "member_id": "mallory",
                    "expected_version": version,
                },
            ).status_code
            == 403
        )
        assert (
            client.post(
                f"/tasks/{task['id']}/rebase",
                json={
                    "member_id": "alice",
                    "expected_version": version,
                },
            ).status_code
            == 200
        )
        cancelled = client.post(
            f"/tasks/{task['id']}/cancel",
            json={
                "author": "owner",
                "reason": "Split the task",
            },
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["task"]["status"] == "cancelled"
        assert client.get("/tasks", params={"member_id": "alice"}).json()["tasks"] == []
