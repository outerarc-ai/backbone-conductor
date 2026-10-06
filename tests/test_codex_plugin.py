"""Exercise the packaged Codex plugin launcher against real MCP stdio sessions."""

from __future__ import annotations

import asyncio
import json
import os
import runpy
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from backbone_conductor.auth import create_token_file
from backbone_conductor.mcp_server import COORDINATOR_TOOLS
from backbone_conductor.remote_member_bridge import create_remote_member_bridge
from backbone_conductor.service import Conductor

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "plugins" / "backbone-conductor" / "scripts" / "launch_mcp.py"
BACKBONE = ROOT / ".venv" / "bin" / "backbone"
SOURCE_ROOT = str(ROOT / "src")
MEMBER_TOOLS = {
    "get_my_task",
    "create_intent",
    "log_decision",
    "start_task",
    "fetch_artifact_branch",
    "rebase_task",
    "submit_artifact",
    "check_backbone_sync",
}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    for command in (
        ("init", "-b", "main"),
        ("config", "user.name", "Codex Plugin Test"),
        ("config", "user.email", "codex-plugin@example.invalid"),
    ):
        subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True)
    (repo / "README.md").write_text("Plugin test\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "Initial"], check=True, capture_output=True
    )
    Conductor(repo).initialize()
    return repo


def test_codex_launcher_fails_closed_without_role_scope(tmp_path: Path) -> None:
    command = runpy.run_path(str(LAUNCHER))["command_from_environment"]
    base = {"BACKBONE_REPO": str(tmp_path), "BACKBONE_EXECUTABLE": str(BACKBONE)}
    with pytest.raises(ValueError, match="BACKBONE_MEMBER"):
        command(base)
    with pytest.raises(ValueError, match="must be member or coordinator"):
        command({**base, "BACKBONE_CODEX_ROLE": "administrator"})
    with pytest.raises(ValueError, match="absolute path"):
        command({**base, "BACKBONE_REPO": "."})
    with pytest.raises(ValueError, match="Remote member MCP needs"):
        command(
            {
                "BACKBONE_EXECUTABLE": str(BACKBONE),
                "BACKBONE_MCP_URL": "https://coordinator.example/mcp",
            }
        )
    with pytest.raises(ValueError, match="member role only"):
        command(
            {
                "BACKBONE_EXECUTABLE": str(BACKBONE),
                "BACKBONE_CODEX_ROLE": "coordinator",
                "BACKBONE_MCP_URL": "https://coordinator.example/mcp",
            }
        )
    with pytest.raises(ValueError, match="does not use a local repository"):
        command(
            {
                **base,
                "BACKBONE_MCP_URL": "https://coordinator.example/mcp",
                "BACKBONE_MEMBER": "alice",
                "BACKBONE_TOKEN_FILE": "/private/alice.token",
            }
        )
    with pytest.raises(ValueError, match="does not use BACKBONE_MEMBER"):
        command({**base, "BACKBONE_CODEX_ROLE": "coordinator", "BACKBONE_MEMBER": "alice"})


def test_codex_launcher_selects_remote_member_without_coordinator_checkout(tmp_path: Path) -> None:
    command = runpy.run_path(str(LAUNCHER))["command_from_environment"]
    selected = command(
        {
            "BACKBONE_MCP_URL": "https://coordinator.example/mcp",
            "BACKBONE_MEMBER": "alice",
            "BACKBONE_TOKEN_FILE": str(tmp_path / "alice.token"),
            "BACKBONE_EXECUTABLE": str(BACKBONE),
        }
    )
    assert selected[:2] == [str(BACKBONE), "mcp-remote-member"]
    assert "--repo" not in selected
    assert "--member" in selected and "alice" in selected


def test_remote_bridge_preflights_exact_member_scope(monkeypatch) -> None:
    observed = []
    monkeypatch.setattr(
        "backbone_conductor.remote_member_bridge.preflight_remote_member",
        lambda *args: observed.append(args),
    )
    server = create_remote_member_bridge(
        "https://coordinator.example/mcp", "alice", "/private/alice.token"
    )
    assert observed == [("https://coordinator.example/mcp", "alice", "/private/alice.token", None)]

    async def check() -> None:
        assert {tool.name for tool in await server.list_tools()} == MEMBER_TOOLS

    asyncio.run(check())


@pytest.mark.parametrize("role", ["member", "coordinator"])
def test_codex_plugin_mcp_exposes_only_selected_scope(tmp_path: Path, role: str) -> None:
    repo = _repo(tmp_path)

    async def check() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(LAUNCHER)],
            env={
                "BACKBONE_REPO": str(repo),
                "BACKBONE_CODEX_ROLE": role,
                **({"BACKBONE_MEMBER": "alice"} if role == "member" else {}),
                "BACKBONE_EXECUTABLE": str(BACKBONE),
            },
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            assert tools == (MEMBER_TOOLS if role == "member" else COORDINATOR_TOOLS)
            assert "merge_task" not in tools and "review_intent" not in tools
            if role == "member":
                response = await session.call_tool("get_my_task", {})
                assert not response.isError
                assert json.loads(response.content[0].text)["member_id"] == "alice"

    asyncio.run(asyncio.wait_for(check(), timeout=20))


def test_codex_plugin_remote_member_uses_authenticated_mcp(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    credentials = tmp_path / "credentials.json"
    issued = create_token_file(credentials, repo, "owner", ["alice"])
    token = next(item["token"] for item in issued if item["name"] == "alice")
    token_file = tmp_path / "alice.token"
    token_file.write_text(token + "\n", encoding="ascii")
    token_file.chmod(0o600)
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 0))
        except PermissionError:
            pytest.skip("This sandbox does not permit loopback listening sockets")
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
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
        env={**os.environ, "PYTHONPATH": SOURCE_ROOT},
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
                    if probe.get(f"{origin}/health").status_code == 200:
                        break
            except httpx.RequestError:
                time.sleep(0.1)
        else:
            raise AssertionError("Coordinator did not become ready")

        async def check() -> None:
            params = StdioServerParameters(
                command=sys.executable,
                args=[str(LAUNCHER)],
                env={
                    "BACKBONE_MCP_URL": f"{origin}/mcp",
                    "BACKBONE_MEMBER": "alice",
                    "BACKBONE_TOKEN_FILE": str(token_file),
                    "BACKBONE_EXECUTABLE": str(BACKBONE),
                },
            )
            async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
                await session.initialize()
                assert {tool.name for tool in (await session.list_tools()).tools} == MEMBER_TOOLS
                response = await session.call_tool(
                    "create_intent",
                    {
                        "intent_data": {
                            "id": "intent-codex-remote",
                            "problem": "Track remote Codex work",
                            "proposed_outcome": "Record the authenticated member proposal",
                        }
                    },
                )
                assert not response.isError
                assert json.loads(response.content[0].text)["author"] == "alice"
                spoof = await session.call_tool(
                    "create_intent",
                    {
                        "intent_data": {
                            "id": "intent-codex-spoof",
                            "author": "bob",
                            "problem": "Impersonate",
                            "proposed_outcome": "Must fail",
                        }
                    },
                )
                assert spoof.isError

        asyncio.run(asyncio.wait_for(check(), timeout=25))
        assert Conductor(repo).state()["intents"]["intent-codex-remote"]["author"] == "alice"
        assert "intent-codex-spoof" not in Conductor(repo).state()["intents"]
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
