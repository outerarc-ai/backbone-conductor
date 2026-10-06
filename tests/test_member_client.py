"""A remote member completes a code handoff without DSH or a coordinator clone."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from backbone_conductor.auth import create_token_file
from backbone_conductor.cli import main
from backbone_conductor.service import Conductor


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


def test_member_cli_requires_https_and_private_token(tmp_path: Path, capsys) -> None:
    token = tmp_path / "member.token"
    token.write_text("a" * 48)
    token.chmod(0o600)
    command = [
        "member",
        "--url",
        "http://coordinator.example",
        "--token-file",
        str(token),
        "whoami",
    ]
    assert main(command) == 1
    assert "HTTPS" in json.loads(capsys.readouterr().err)["error"]
    token.chmod(0o644)
    command[2] = "https://coordinator.example"
    assert main(command) == 1
    assert "0600" in json.loads(capsys.readouterr().err)["error"]


def test_member_cli_hands_off_real_git_branch_from_separate_clone(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    monkeypatch.chdir(tmp_path)
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    coordinator = tmp_path / "coordinator"
    subprocess.run(["git", "clone", str(origin), str(coordinator)], check=True, capture_output=True)
    git(coordinator, "config", "user.name", "Coordinator")
    git(coordinator, "config", "user.email", "coordinator@example.invalid")
    git(coordinator, "config", "commit.gpgsign", "false")
    (coordinator / "app.py").write_text("def value():\n    return 1\n")
    git(coordinator, "add", "app.py")
    git(coordinator, "commit", "-m", "Initial app")
    git(coordinator, "push", "-u", "origin", "main")
    Conductor(coordinator).initialize()

    credentials = tmp_path / "credentials.json"
    issued = create_token_file(credentials, coordinator, "owner", ["alice", "bob"])
    tokens = {item["name"]: item["token"] for item in issued}
    member_tokens = {}
    for name in ("alice", "bob", "owner"):
        path = tmp_path / f"{name}.token"
        path.write_text(tokens[name] + "\n", encoding="ascii")
        path.chmod(0o600)
        member_tokens[name] = path

    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 0))
        except PermissionError:
            if os.environ.get("BACKBONE_REQUIRE_LIVE_HTTP") == "1":
                raise
            pytest.skip("This sandbox does not permit loopback listening sockets")
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backbone_conductor",
            "--repo",
            str(coordinator),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--auth-file",
            str(credentials),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise AssertionError(f"Coordinator exited: {server.stderr.read()}")
            try:
                with httpx.Client(trust_env=False, timeout=0.5) as probe:
                    if probe.get(f"{url}/health").status_code == 200:
                        break
            except httpx.RequestError:
                time.sleep(0.1)
        else:
            raise AssertionError("Coordinator did not become ready")

        def command(name: str, *actions: str) -> tuple[int, dict]:
            status = main(
                ["member", "--url", url, "--token-file", str(member_tokens[name]), *actions]
            )
            output = capsys.readouterr()
            return status, json.loads(output.out if status == 0 else output.err)

        assert command("alice", "whoami") == (0, {"name": "alice", "role": "member"})
        assert command("owner", "whoami")[0] == 1
        assert command("bob", "tasks")[1]["tasks"] == []
        with httpx.Client(base_url=url, trust_env=False) as admin:
            headers = {"Authorization": f"Bearer {tokens['owner']}"}
            created = admin.post(
                "/intents",
                headers=headers,
                json={
                    "id": "intent-alice",
                    "problem": "Change app value",
                    "proposed_outcome": "Return 2",
                    "affected_paths": ["app.py"],
                },
            )
            assert created.status_code == 201
            assert (
                admin.post(
                    "/intents/intent-alice/transition", headers=headers, json={"status": "accepted"}
                ).status_code
                == 200
            )
            dispatch = admin.post(
                "/tasks", headers=headers, json={"intent_id": "intent-alice", "member_id": "alice"}
            )
            assert dispatch.status_code == 201, dispatch.text
            task_id = dispatch.json()["id"]

        status, context = command("alice", "tasks")
        assert status == 0 and context["member_id"] == "alice"
        assert [task["id"] for task in context["tasks"]] == [task_id]
        assert command("bob", "tasks")[1]["tasks"] == []
        assert command("bob", "start", task_id)[0] == 1
        assert command("alice", "start", task_id)[1]["status"] == "in_progress"
        assert command("alice", "updates", "--since-version", context["version"])[1]["changed"]
        assert command("alice", "rebase", task_id, "--version", context["version"])[0] == 1
        fresh = command("alice", "tasks")[1]
        assert command("alice", "rebase", task_id, "--version", fresh["version"])[0] == 0

        member_repo = tmp_path / "alice-clone"
        subprocess.run(
            ["git", "clone", str(origin), str(member_repo)], check=True, capture_output=True
        )
        git(member_repo, "config", "user.name", "Alice")
        git(member_repo, "config", "user.email", "alice@example.invalid")
        git(member_repo, "switch", "-c", "feature/alice")
        (member_repo / "app.py").write_text("def value():\n    return 2\n")
        git(member_repo, "add", "app.py")
        git(member_repo, "commit", "-m", "Change app value")
        sha = git(member_repo, "rev-parse", "HEAD")
        git(member_repo, "push", "origin", "feature/alice")
        assert (
            command("alice", "fetch", task_id, "--branch", "feature/alice", "--sha", "0" * 40)[0]
            == 1
        )
        assert (
            command("alice", "fetch", task_id, "--branch", "feature/alice", "--sha", sha)[1][
                "tracking_ref"
            ]
            == "origin/feature/alice"
        )
        assert git(coordinator, "rev-parse", "origin/feature/alice") == sha

        artifact = tmp_path / "artifact.json"
        artifact.write_text(
            json.dumps(
                {
                    "intent_id": "intent-alice",
                    "branch": "origin/feature/alice",
                    "base_ref": "main",
                    "summary": "Return 2",
                    "commit_sha": sha,
                }
            )
        )
        status, submission = command("alice", "submit", task_id, "--file", str(artifact))
        assert status == 0 and submission["accepted"]
        assert submission["artifact"]["commit_sha"] == sha
        assert Conductor(coordinator).state()["tasks"][task_id]["status"] == "submitted"
        assert git(coordinator, "rev-parse", "main") != sha

        proposal = tmp_path / "intent.json"
        proposal.write_text(
            json.dumps(
                {
                    "id": "intent-next",
                    "problem": "Next change",
                    "proposed_outcome": "Documented plan",
                }
            )
        )
        status, created = command("alice", "create-intent", "--file", str(proposal))
        assert status == 0 and created["author"] == "alice" and created["status"] == "draft"
        decision = tmp_path / "decision.json"
        decision.write_text(
            json.dumps(
                {"decision_type": "process", "summary": "Review first", "rationale": "Human review"}
            )
        )
        status, proposed = command("alice", "propose-decision", "--file", str(decision))
        assert status == 0 and proposed["author"] == "alice" and proposed["status"] == "proposed"
        proposal.write_text(
            json.dumps(
                {"id": "intent-spoof", "author": "bob", "problem": "x", "proposed_outcome": "y"}
            )
        )
        assert command("alice", "create-intent", "--file", str(proposal))[0] == 1
        assert "intent-spoof" not in Conductor(coordinator).state()["intents"]
        history = git(coordinator, "log", "--format=%B", "--", ".backbone")
        assert "Backbone-HTTP-Principal: alice" in history
        assert all(token not in history for token in tokens.values())
    finally:
        server.terminate()
        try:
            server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)
