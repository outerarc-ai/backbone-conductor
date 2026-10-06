"""Verify that direct TLS serves authenticated HTTP with certificate validation."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from backbone_conductor.auth import create_token_file
from backbone_conductor.cli import main
from backbone_conductor.dsh_agent import DSHRemoteMemberRunner
from backbone_conductor.service import Conductor


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def test_direct_https_requires_trusted_certificate_and_bearer_token(
    tmp_path: Path, capsys, monkeypatch, mock_dsh_tool_provider
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "TLS Test")
    git(repo, "config", "user.email", "tls@example.invalid")
    Conductor(repo).initialize()
    credentials = tmp_path / "credentials.json"
    issued = create_token_file(credentials, repo, "owner", ["alice"], ["carol"])
    token = next(item["token"] for item in issued if item["name"] == "owner")
    member_token = next(item["token"] for item in issued if item["name"] == "alice")
    reviewer_token = next(item["token"] for item in issued if item["name"] == "carol")
    reviewer_file = tmp_path / "carol.token"
    reviewer_file.write_text(reviewer_token + "\n", encoding="ascii")
    reviewer_file.chmod(0o600)
    certificate = tmp_path / "server.crt"
    key = tmp_path / "server.key"
    subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-nodes",
            "-keyout",
            str(key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 0))
        except PermissionError:
            if os.environ.get("BACKBONE_REQUIRE_LIVE_HTTP") == "1":
                raise
            pytest.skip("This sandbox does not permit loopback listening sockets")
        port = listener.getsockname()[1]
    source_path = str(Path(__file__).resolve().parents[1] / "src")
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "backbone_conductor",
            "--repo",
            str(repo),
            "serve",
            "--port",
            str(port),
            "--auth-file",
            str(credentials),
            "--mcp-http",
            "--tls-certfile",
            str(certificate),
            "--tls-keyfile",
            str(key),
        ],
        env={**os.environ, "PYTHONPATH": source_path},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    url = f"https://127.0.0.1:{port}"
    reviewer_command = [
        "reviewer",
        "--url",
        url,
        "--token-file",
        str(reviewer_file),
        "--ca-file",
        str(certificate),
    ]
    try:
        context = ssl.create_default_context(cafile=str(certificate))
        with httpx.Client(verify=context, trust_env=False, timeout=2) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise AssertionError(f"TLS server exited: {server.stderr.read()}")
                try:
                    if client.get(f"{url}/health").status_code == 200:
                        break
                except httpx.RequestError:
                    time.sleep(0.1)
            else:
                raise AssertionError("TLS server did not become healthy")

            assert client.get(f"{url}/state").status_code == 401
            assert main([*reviewer_command, "whoami"]) == 0
            assert json.loads(capsys.readouterr().out) == {"name": "carol", "role": "reviewer"}
            service = Conductor(repo)
            decision = service.log_decision(
                {
                    "author": "owner",
                    "decision_type": "api",
                    "summary": "Drop the old endpoint",
                    "rationale": "Simpler interface",
                }
            )
            service.transition_decision(decision["id"], "accepted")
            assert main([*reviewer_command, "decisions"]) == 0
            assert decision["id"] in {item["id"] for item in json.loads(capsys.readouterr().out)}
            version = service.state()["version"]
            assert (
                main(
                    [
                        *reviewer_command,
                        "revert-decision",
                        decision["id"],
                        "--rationale",
                        "Clients still need the endpoint",
                        "--version",
                        version,
                    ]
                )
                == 0
            )
            reverted = json.loads(capsys.readouterr().out)
            assert reverted["reversion"]["author"] == "carol"
            assert reverted["reversion"]["reviewed_version"] == version
            assert "Backbone-HTTP-Principal: carol" in git(repo, "log", "-1", "--format=%B")
            assert (
                main(
                    [
                        *reviewer_command,
                        "revert-decision",
                        decision["id"],
                        "--rationale",
                        "Stale retry",
                        "--version",
                        version,
                    ]
                )
                == 1
            )
            assert "HTTP 422" in json.loads(capsys.readouterr().err)["error"]

            first = service.create_intent(
                {
                    "author": "owner",
                    "problem": "Replace the export API",
                    "proposed_outcome": "New export format",
                    "affected_paths": ["src/export.py"],
                }
            )
            second = service.create_intent(
                {
                    "author": "alice",
                    "problem": "Extend the export API",
                    "proposed_outcome": "Old callers keep working",
                    "affected_paths": ["src/export.py"],
                }
            )
            assert main([*reviewer_command, "conflicts"]) == 0
            conflicts = json.loads(capsys.readouterr().out)
            overlap = next(
                item
                for item in conflicts
                if set(item["parties"]) == {first["id"], second["id"]} and not item["resolved"]
            )
            stale_version = service.state()["version"]
            service.log_decision(
                {
                    "author": "owner",
                    "decision_type": "process",
                    "summary": "Schedule review",
                    "rationale": "Plan",
                }
            )
            stale_command = [
                *reviewer_command,
                "resolve-conflict",
                overlap["id"],
                "--action",
                "coordinate",
                "--rationale",
                "Agree the API change order",
                "--version",
                stale_version,
            ]
            assert main(stale_command) == 1
            assert "HTTP 422" in json.loads(capsys.readouterr().err)["error"]
            assert not service.state()["conflicts"][overlap["id"]]["resolved"]
            version = service.state()["version"]
            assert (
                main(
                    [
                        *reviewer_command,
                        "resolve-conflict",
                        overlap["id"],
                        "--action",
                        "coordinate",
                        "--rationale",
                        "Agree the API change order",
                        "--version",
                        version,
                    ]
                )
                == 0
            )
            resolution = json.loads(capsys.readouterr().out)
            assert resolution["decision"]["author"] == "carol"
            assert resolution["conflict"]["resolution"]["reviewed_version"] == version
            assert service.state()["conflicts"][overlap["id"]]["resolved"]
            assert (
                main(
                    [
                        *reviewer_command,
                        "timeline",
                        "--http-principal",
                        "carol",
                        "--type",
                        "conflict",
                        "--limit",
                        "1",
                    ]
                )
                == 0
            )
            audit_events = json.loads(capsys.readouterr().out)
            assert len(audit_events) == 1
            assert audit_events[0]["http_principal"] == "carol"
            assert audit_events[0]["event_type"] == "conflict"
            assert main([*reviewer_command, "audit-verify", "--limit", "1"]) == 0
            audit_report = json.loads(capsys.readouterr().out)
            assert audit_report["checked"] == 1
            assert audit_report["unsigned"] == 1
            assert main([*reviewer_command, "audit-snapshot"]) == 0
            snapshot_report = json.loads(capsys.readouterr().out)
            assert snapshot_report["ok"] is True
            assert snapshot_report["version"] == service.state()["version"]
            assert main([*reviewer_command, "audit-history", "--limit", "100"]) == 0
            history_report = json.loads(capsys.readouterr().out)
            assert history_report["ok"] is True
            assert history_report["checked"] == history_report["total_metadata_commits"]
            assert main([*reviewer_command, "audit-history", "--all", "--limit", "2"]) == 0
            complete_history = json.loads(capsys.readouterr().out)
            assert complete_history["ok"] is True
            assert complete_history["pages"] > 1
            assert complete_history["checked"] == history_report["checked"]
            assert (
                main([*reviewer_command, "audit-verify", "--limit", "1", "--require-signatures"])
                == 1
            )
            assert json.loads(capsys.readouterr().out)["unsigned"] == 1
            headers = {"Authorization": f"Bearer {token}"}
            assert client.get(f"{url}/state", headers=headers).status_code == 200
            response = client.post(
                f"{url}/intents",
                headers=headers,
                json={"id": "tls-intent", "problem": "Verify HTTPS", "proposed_outcome": "Pass"},
            )
            assert response.status_code == 201, response.text

        async def member_mcp() -> dict:
            async with httpx.AsyncClient(
                verify=context,
                trust_env=False,
                headers={"Authorization": f"Bearer {member_token}"},
            ) as http_client:
                async with streamable_http_client(f"{url}/mcp", http_client=http_client) as (
                    read,
                    write,
                    _session_id,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        result = await session.call_tool(
                            "create_intent",
                            {
                                "intent_data": {
                                    "id": "tls-mcp-intent",
                                    "problem": "Verify remote MCP with HTTPS",
                                    "proposed_outcome": "Persist through trusted TLS",
                                }
                            },
                        )
                        assert not result.isError
                        return json.loads(result.content[0].text)

        assert asyncio.run(member_mcp())["author"] == "alice"
        workspace = tmp_path / "member-workspace"
        workspace.mkdir()
        token_file = tmp_path / "alice.token"
        token_file.write_text(member_token + "\n", encoding="ascii")
        token_file.chmod(0o600)
        member_command = [
            "member",
            "--url",
            url,
            "--token-file",
            str(token_file),
            "--ca-file",
            str(certificate),
        ]
        assert main([*member_command, "whoami"]) == 0
        assert json.loads(capsys.readouterr().out) == {"name": "alice", "role": "member"}
        assert main([*member_command, "tasks"]) == 0
        assert json.loads(capsys.readouterr().out)["member_id"] == "alice"
        assert main(["member", "--url", url, "--token-file", str(token_file), "whoami"]) == 1
        assert "connection failed" in json.loads(capsys.readouterr().err)["error"]
        runner = DSHRemoteMemberRunner(
            workspace,
            tmp_path / "private-dsh-home",
            "alice",
            "placeholder",
            f"{url}/mcp",
            token_file,
            ca_file=certificate,
        )
        runner._preflight_mcp()
        assert runner._harness_env() == {"NODE_EXTRA_CA_CERTS": str(certificate)}
        if os.environ.get("BACKBONE_REQUIRE_DSH_MCP") == "1":
            from deepseek_harness import DeepSeekHarness

            patch = tmp_path / "trusted-remote.patch.yml"
            patch.write_text(json.dumps(runner.member_patch()), encoding="utf-8")
            patch.chmod(0o600)
            harness = DeepSeekHarness(
                dsh_home=str(runner.home),
                cwd=str(workspace),
                profile="sdk-minimal",
                patches=(str(patch),),
                provider="deepseek-official",
                model="placeholder",
                env=runner._harness_env(),
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
                        "id": "tls-dsh-intent",
                        "problem": "Verify trusted HTTPS from DSH",
                        "proposed_outcome": "Record a member-authenticated intent",
                    }
                },
                "Trusted intent recorded",
            ) as (provider_url, requests):
                monkeypatch.setenv("DEEPSEEK_BASE_URL", provider_url)
                monkeypatch.setenv("DEEPSEEK_API_KEY", "https-local-mock-key")
                result = runner.run("Create an intent over trusted HTTPS")
            assert result["final_response"] == "Trusted intent recorded"
            assert len(requests) == 2
            tool_messages = [
                message for message in requests[1]["messages"] if message.get("role") == "tool"
            ]
            assert "tls-dsh-intent" in json.dumps(tool_messages)
            assert member_token not in json.dumps(requests)
            assert Conductor(repo).state()["intents"]["tls-dsh-intent"]["author"] == "alice"
            assert "Backbone-HTTP-Principal: alice" in git(repo, "log", "-1", "--format=%B")
        with httpx.Client(trust_env=False, timeout=2) as untrusted:
            with pytest.raises(httpx.RequestError):
                untrusted.get(f"{url}/health")
            with pytest.raises(httpx.RequestError):
                untrusted.get(f"http://127.0.0.1:{port}/health")
        history = git(repo, "log", "--format=%B", "--", ".backbone")
        assert "Backbone-HTTP-Principal: owner" in history
        assert "Backbone-HTTP-Principal: alice" in history
    finally:
        if server.poll() is None:
            server.terminate()
        try:
            server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)
