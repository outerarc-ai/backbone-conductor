"""Exercise authenticated Streamable HTTP MCP against a real Git ledger."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from backbone_conductor.api import create_app
from backbone_conductor.auth import (
    create_token_file,
    rotate_principal_token,
    rotate_token_file,
)
from backbone_conductor.cli import main
from backbone_conductor.dsh_agent import DSHRemoteMemberRunner
from backbone_conductor.ledger import create_ledger
from backbone_conductor.service import Conductor


@pytest.fixture
def remote_repo(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    repo = tmp_path / "project"
    repo.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.name", "Remote MCP Tests"),
        ("config", "user.email", "remote-mcp@example.invalid"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    Conductor(repo).initialize()
    credentials = tmp_path / "credentials.json"
    issued = create_token_file(credentials, repo, "owner", ["alice", "bob"])
    return repo, credentials, {item["name"]: item["token"] for item in issued}


def _request(client: TestClient, method: str, token: str | None, params: dict | None = None):
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Mcp-Protocol-Version": "2025-06-18",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp",
        headers=headers,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )


def test_http_mcp_requires_member_token_and_binds_each_request(remote_repo) -> None:
    repo, credentials, tokens = remote_repo
    app = create_app(repo, auth_file=credentials, mcp_http=True, mcp_allowed_hosts=("testserver",))
    with TestClient(app) as client:
        initialize = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "backbone-tests", "version": "1"},
        }
        assert _request(client, "initialize", None, initialize).status_code == 401
        assert _request(client, "initialize", tokens["owner"], initialize).status_code == 403
        assert _request(client, "initialize", tokens["alice"], initialize).status_code == 200

        listing = _request(client, "tools/list", tokens["alice"])
        assert listing.status_code == 200, listing.text
        names = {tool["name"] for tool in listing.json()["result"]["tools"]}
        assert names == {
            "get_my_task",
            "fetch_artifact_branch",
            "submit_artifact",
            "check_backbone_sync",
            "create_intent",
            "log_decision",
            "start_task",
            "rebase_task",
        }

        spoof = _request(
            client,
            "tools/call",
            tokens["alice"],
            {
                "name": "create_intent",
                "arguments": {
                    "intent_data": {
                        "author": "bob",
                        "problem": "Spoof",
                        "proposed_outcome": "No",
                    }
                },
            },
        )
        assert spoof.json()["result"]["isError"]
        created = _request(
            client,
            "tools/call",
            tokens["alice"],
            {
                "name": "create_intent",
                "arguments": {
                    "intent_data": {
                        "id": "intent-alice-http",
                        "problem": "Remote collaboration",
                        "proposed_outcome": "Record a member intent",
                    }
                },
            },
        )
        assert created.status_code == 200, created.text
        assert not created.json()["result"]["isError"]
        assert Conductor(repo).state()["intents"]["intent-alice-http"]["author"] == "alice"
        commit = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%B"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "Backbone-HTTP-Principal: alice" in commit
        assert "Backbone-HTTP-Role: member" in commit

        mismatch = _request(
            client,
            "tools/call",
            tokens["bob"],
            {"name": "get_my_task", "arguments": {"member_id": "alice"}},
        )
        assert mismatch.json()["result"]["isError"]
        own = _request(
            client,
            "tools/call",
            tokens["bob"],
            {"name": "get_my_task", "arguments": {}},
        )
        assert not own.json()["result"]["isError"], own.text
        assert json.loads(own.json()["result"]["content"][0]["text"])["member_id"] == "bob"

        host = {"Host": "unlisted.example", "Authorization": f"Bearer {tokens['alice']}"}
        assert client.post("/mcp", headers=host, json={}).status_code == 421
        one = rotate_principal_token(credentials, repo, "alice")
        assert _request(client, "tools/list", tokens["alice"]).status_code == 401
        assert _request(client, "tools/list", one["token"]).status_code == 200
        assert _request(client, "tools/list", tokens["bob"]).status_code == 200
        assert (
            client.get(
                "/whoami", headers={"Authorization": f"Bearer {tokens['owner']}"}
            ).status_code
            == 200
        )
        rotated = rotate_token_file(credentials, repo, "owner", ["alice", "bob"])
        new_alice = next(item["token"] for item in rotated if item["name"] == "alice")
        assert _request(client, "tools/list", one["token"]).status_code == 401
        assert _request(client, "tools/list", new_alice).status_code == 200
        credentials.chmod(0o644)
        try:
            assert _request(client, "tools/list", new_alice).status_code == 503
        finally:
            credentials.chmod(0o600)


def test_http_mcp_is_explicit_and_requires_credentials(remote_repo) -> None:
    repo, credentials, tokens = remote_repo
    with pytest.raises(ValueError, match="requires --auth-file"):
        create_app(repo, mcp_http=True)
    with TestClient(create_app(repo, auth_file=credentials)) as client:
        assert _request(client, "tools/list", tokens["alice"]).status_code == 403
    assert main(["--repo", str(repo), "serve", "--mcp-http"]) == 1
    assert main(["--repo", str(repo), "serve", "--mcp-allowed-host", "example.org"]) == 1


def test_cli_can_enable_member_mcp_with_compose_environment(remote_repo, monkeypatch) -> None:
    repo, credentials, tokens = remote_repo
    captured = {}
    monkeypatch.setenv("BACKBONE_MCP_HTTP", "1")
    monkeypatch.setenv("BACKBONE_MCP_ALLOWED_HOSTS", "testserver, coordinator.example.org:8443")
    monkeypatch.setattr("uvicorn.run", lambda app, **_options: captured.setdefault("app", app))
    assert main(["--repo", str(repo), "serve", "--auth-file", str(credentials)]) == 0
    with TestClient(captured["app"]) as client:
        assert _request(client, "tools/list", tokens["alice"]).status_code == 200
        assert _request(client, "tools/list", tokens["owner"]).status_code == 403

    monkeypatch.setenv("BACKBONE_MCP_HTTP", "true")
    assert main(["--repo", str(repo), "serve", "--auth-file", str(credentials)]) == 1
    monkeypatch.setenv("BACKBONE_MCP_HTTP", "1")
    monkeypatch.setenv("BACKBONE_MCP_ALLOWED_HOSTS", "testserver,,example.org")
    assert main(["--repo", str(repo), "serve", "--auth-file", str(credentials)]) == 1


def test_http_mcp_writes_only_separate_ledger_branch(tmp_path: Path) -> None:
    repo = tmp_path / "source"
    repo.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.name", "Remote Ledger Tests"),
        ("config", "user.email", "remote-ledger@example.invalid"),
        ("commit", "--allow-empty", "-m", "Source baseline"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    source_head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    create_ledger(repo)
    credentials = tmp_path / "credentials.json"
    issued = create_token_file(credentials, repo, "owner", ["alice"])
    token = next(item["token"] for item in issued if item["name"] == "alice")
    app = create_app(
        repo,
        auth_file=credentials,
        ledger_branch="backbone",
        mcp_http=True,
        mcp_allowed_hosts=("testserver",),
    )
    with TestClient(app) as client:
        response = _request(
            client,
            "tools/call",
            token,
            {
                "name": "create_intent",
                "arguments": {
                    "intent_data": {
                        "id": "separate-mcp-intent",
                        "problem": "Keep code unchanged",
                        "proposed_outcome": "Write ledger only",
                    }
                },
            },
        )
        assert not response.json()["result"]["isError"], response.text
    assert "separate-mcp-intent" in Conductor(repo, ledger_branch="backbone").state()["intents"]
    assert (
        subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        == source_head
    )


def test_live_streamable_http_mcp_client_and_rotation(remote_repo) -> None:
    repo, credentials, tokens = remote_repo
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 0))
        except PermissionError:
            if os.environ.get("BACKBONE_REQUIRE_LIVE_HTTP") == "1":
                raise
            pytest.skip("This sandbox does not permit loopback listening sockets")
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backbone_conductor",
            "--repo",
            str(repo),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--auth-file",
            str(credentials),
            "--mcp-http",
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise AssertionError(f"MCP server exited: {server.stderr.read()}")
            try:
                with httpx.Client(trust_env=False, timeout=0.5) as probe:
                    if probe.get(f"{url}/health").status_code == 200:
                        break
            except httpx.RequestError:
                time.sleep(0.1)
        else:
            raise AssertionError("MCP server did not become ready")

        async def member_call(token: str, tool: str, arguments: dict) -> dict:
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, trust_env=False
            ) as http_client:
                async with streamable_http_client(f"{url}/mcp", http_client=http_client) as (
                    read,
                    write,
                    _session_id,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        listed = {item.name for item in (await session.list_tools()).tools}
                        assert "dispatch_task" not in listed
                        result = await session.call_tool(tool, arguments)
                        assert not result.isError
                        return json.loads(result.content[0].text)

        created = asyncio.run(
            member_call(
                tokens["alice"],
                "create_intent",
                {
                    "intent_data": {
                        "id": "intent-live-mcp",
                        "problem": "Use remote MCP",
                        "proposed_outcome": "Persist an authenticated intent",
                    }
                },
            )
        )
        assert created["author"] == "alice"
        assert Conductor(repo).state()["intents"]["intent-live-mcp"]["author"] == "alice"

        async def concurrent_members() -> list[dict]:
            return await asyncio.gather(
                member_call(
                    tokens["alice"],
                    "create_intent",
                    {
                        "intent_data": {
                            "id": "intent-concurrent-alice",
                            "problem": "Alice's remote task",
                            "proposed_outcome": "Record Alice's intent",
                        }
                    },
                ),
                member_call(
                    tokens["bob"],
                    "create_intent",
                    {
                        "intent_data": {
                            "id": "intent-concurrent-bob",
                            "problem": "Bob's remote task",
                            "proposed_outcome": "Record Bob's intent",
                        }
                    },
                ),
            )

        simultaneous = asyncio.run(concurrent_members())
        assert {item["author"] for item in simultaneous} == {"alice", "bob"}
        state = Conductor(repo).state()
        assert state["intents"]["intent-concurrent-alice"]["author"] == "alice"
        assert state["intents"]["intent-concurrent-bob"]["author"] == "bob"

        rotated = rotate_token_file(credentials, repo, "owner", ["alice", "bob"])
        new_alice = next(item["token"] for item in rotated if item["name"] == "alice")
        with httpx.Client(trust_env=False) as client:
            response = client.post(
                f"{url}/mcp",
                headers={"Authorization": f"Bearer {tokens['alice']}"},
                json={},
            )
        assert response.status_code == 401
        context = asyncio.run(member_call(new_alice, "get_my_task", {}))
        assert context["member_id"] == "alice"
    finally:
        server.terminate()
        try:
            server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)


def test_remote_dsh_member_preflight_sdk_and_mock_tool_call(
    remote_repo, tmp_path: Path, monkeypatch, mock_dsh_tool_provider, capsys
) -> None:
    repo, credentials, tokens = remote_repo
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 0))
        except PermissionError:
            if os.environ.get("BACKBONE_REQUIRE_LIVE_HTTP") == "1":
                raise
            pytest.skip("This sandbox does not permit loopback listening sockets")
        port = listener.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backbone_conductor",
            "--repo",
            str(repo),
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--auth-file",
            str(credentials),
            "--mcp-http",
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
                raise AssertionError(f"MCP server exited: {server.stderr.read()}")
            try:
                with httpx.Client(trust_env=False, timeout=0.5) as probe:
                    if probe.get(f"{base_url}/health").status_code == 200:
                        break
            except httpx.RequestError:
                time.sleep(0.1)
        else:
            raise AssertionError("MCP server did not become ready")

        workspace = tmp_path / "member-workspace"
        workspace.mkdir()
        token_file = tmp_path / "alice.token"
        token_file.write_text(tokens["alice"] + "\n", encoding="ascii")
        token_file.chmod(0o600)
        command = [
            "member-check",
            "--mcp-url",
            f"{base_url}/mcp",
            "--member",
            "alice",
            "--token-file",
            str(token_file),
        ]
        assert main(command) == 0
        report = json.loads(capsys.readouterr().out)
        assert report == {
            "member_id": "alice",
            "mcp_url": f"{base_url}/mcp",
            "tool_count": 8,
            "verified": True,
        }
        assert tokens["alice"] not in json.dumps(report)
        assert main(command[:4] + ["bob"] + command[5:]) == 1
        assert "preflight failed" in capsys.readouterr().err
        token_file.chmod(0o644)
        assert main(command) == 1
        assert "0600" in capsys.readouterr().err
        token_file.chmod(0o600)
        assert main(command[:2] + ["http://coordinator.example/mcp"] + command[3:]) == 1
        assert "HTTPS" in capsys.readouterr().err
        runner = DSHRemoteMemberRunner(
            workspace, tmp_path / "dsh-home", "alice", "placeholder", f"{base_url}/mcp", token_file
        )
        runner._preflight_mcp()
        token_file.write_text(tokens["bob"] + "\n", encoding="ascii")
        with pytest.raises(ValueError, match="preflight failed"):
            runner._preflight_mcp()
        token_file.write_text(tokens["alice"] + "\n", encoding="ascii")

        if os.environ.get("BACKBONE_REQUIRE_DSH_MCP") == "1":
            from deepseek_harness import DeepSeekHarness

            patch = tmp_path / "remote.patch.yml"
            patch.write_text(json.dumps(runner.member_patch()), encoding="utf-8")
            patch.chmod(0o600)
            harness = DeepSeekHarness(
                dsh_home=str(runner.home),
                cwd=str(workspace),
                profile="sdk-minimal",
                patches=(str(patch),),
                provider="deepseek-official",
                model="placeholder",
                initialize_timeout_seconds=30,
            )
            try:
                harness.start()
                assert harness._initialized
                assert harness.client._proc.poll() is None
            finally:
                harness.close()

            with mock_dsh_tool_provider(
                "mcp__backbone__create_intent",
                {
                    "intent_data": {
                        "id": "intent-dsh-remote",
                        "problem": "Exercise the remote DSH tool path",
                        "proposed_outcome": "Audit the member-bound write",
                    }
                },
                "Remote intent recorded",
            ) as (provider_url, requests):
                monkeypatch.setenv("DEEPSEEK_BASE_URL", provider_url)
                monkeypatch.setenv("DEEPSEEK_API_KEY", "remote-local-mock-key")
                result = runner.run("Create the assigned remote intent")
            assert result["final_response"] == "Remote intent recorded"
            assert len(requests) == 2
            assert "mcp__backbone__create_intent" in json.dumps(requests[0].get("tools", []))
            tool_messages = [
                message for message in requests[1]["messages"] if message.get("role") == "tool"
            ]
            assert "intent-dsh-remote" in json.dumps(tool_messages)
            assert tokens["alice"] not in json.dumps(requests)
            assert Conductor(repo).state()["intents"]["intent-dsh-remote"]["author"] == "alice"
            commit = subprocess.run(
                ["git", "-C", str(repo), "log", "-1", "--format=%B"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            assert "Backbone-HTTP-Principal: alice" in commit
            assert "Backbone-HTTP-Role: member" in commit
    finally:
        server.terminate()
        try:
            server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)
