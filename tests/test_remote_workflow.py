"""A member in another Git clone completes work through the remote coordinator."""

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
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from backbone_conductor.auth import create_token_file
from backbone_conductor.service import Conductor


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def test_remote_member_clone_can_fetch_and_requires_real_human_merge(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)],
        check=True,
        capture_output=True,
    )
    coordinator = tmp_path / "coordinator"
    subprocess.run(["git", "clone", str(origin), str(coordinator)], check=True, capture_output=True)
    for setting, value in (
        ("user.name", "Coordinator"),
        ("user.email", "coordinator@example.invalid"),
        ("commit.gpgsign", "false"),
    ):
        git(coordinator, "config", setting, value)
    (coordinator / "app.py").write_text("def value():\n    return 1\n")
    git(coordinator, "add", "app.py")
    git(coordinator, "commit", "-m", "Initial app")
    git(coordinator, "push", "-u", "origin", "main")
    Conductor(coordinator).initialize()

    credentials = tmp_path / "credentials.json"
    issued = create_token_file(credentials, coordinator, "owner", ["alice"], ["reviewer"])
    tokens = {entry["name"]: entry["token"] for entry in issued}
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
                raise AssertionError(f"Coordinator exited: {server.stderr.read()}")
            try:
                with httpx.Client(trust_env=False, timeout=0.5) as probe:
                    if probe.get(f"{url}/health").status_code == 200:
                        break
            except httpx.RequestError:
                time.sleep(0.1)
        else:
            raise AssertionError("Coordinator did not become ready")

        with httpx.Client(base_url=url, trust_env=False) as admin:
            owner = {"Authorization": f"Bearer {tokens['owner']}"}
            reviewer = {"Authorization": f"Bearer {tokens['reviewer']}"}
            created = admin.post(
                "/intents",
                headers=owner,
                json={
                    "id": "intent-remote-clone",
                    "problem": "Change the application value",
                    "proposed_outcome": "The new value is available",
                    "affected_paths": ["app.py"],
                },
            )
            assert created.status_code == 201, created.text
            version = admin.get("/state", headers=owner).json()["version"]
            reviewed = admin.post(
                "/intents/intent-remote-clone/review",
                headers=reviewer,
                json={
                    "outcome": "accepted",
                    "author": "reviewer",
                    "rationale": "The scoped change is ready to assign",
                    "expected_version": version,
                },
            )
            assert reviewed.status_code == 200, reviewed.text
            dispatched = admin.post(
                "/tasks",
                headers=owner,
                json={"intent_id": "intent-remote-clone", "member_id": "alice"},
            )
            assert dispatched.status_code == 201, dispatched.text
            task_id = dispatched.json()["id"]

            git(coordinator, "push", "origin", "main")
            member_repo = tmp_path / "alice-clone"
            subprocess.run(
                ["git", "clone", str(origin), str(member_repo)], check=True, capture_output=True
            )
            git(member_repo, "config", "user.name", "Alice")
            git(member_repo, "config", "user.email", "alice@example.invalid")

            async def member_call(tool: str, arguments: dict) -> tuple[bool, dict | str]:
                async with httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {tokens['alice']}"}, trust_env=False
                ) as http_client:
                    async with streamable_http_client(f"{url}/mcp", http_client=http_client) as (
                        read,
                        write,
                        _,
                    ):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            response = await session.call_tool(tool, arguments)
                            body = response.content[0].text
                            return response.isError, json.loads(
                                body
                            ) if not response.isError else body

            failed, context = asyncio.run(member_call("get_my_task", {}))
            assert not failed
            assert context["member_id"] == "alice"
            assert context["tasks"][0]["id"] == task_id
            failed, started = asyncio.run(member_call("start_task", {"task_id": task_id}))
            assert not failed and started["status"] == "in_progress"

            git(member_repo, "switch", "-c", "feature/alice")
            (member_repo / "app.py").write_text("def value():\n    return 2  # first pass\n")
            git(member_repo, "add", "app.py")
            git(member_repo, "commit", "-m", "Change app value")
            feature_sha = git(member_repo, "rev-parse", "HEAD")
            git(member_repo, "push", "-u", "origin", "feature/alice")
            artifact = {
                "artifact": {
                    "intent_id": "intent-remote-clone",
                    "branch": "origin/feature/alice",
                    "base_ref": "main",
                    "summary": "Change app value to 2",
                    "commit_sha": feature_sha,
                }
            }
            before_failed_submission = git(coordinator, "rev-parse", "HEAD")
            failed, _ = asyncio.run(member_call("submit_artifact", artifact))
            assert failed, "The coordinator must not invent an unfetched member commit"
            assert git(coordinator, "rev-parse", "HEAD") == before_failed_submission

            failed, _ = asyncio.run(
                member_call(
                    "fetch_artifact_branch",
                    {
                        "task_id": task_id,
                        "branch": "feature/alice",
                        "expected_sha": "0" * 40,
                    },
                )
            )
            assert failed
            assert (
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(coordinator),
                        "rev-parse",
                        "--verify",
                        "origin/feature/alice",
                    ],
                    capture_output=True,
                ).returncode
                != 0
            )
            failed, fetched = asyncio.run(
                member_call(
                    "fetch_artifact_branch",
                    {
                        "task_id": task_id,
                        "branch": "feature/alice",
                        "expected_sha": feature_sha,
                    },
                )
            )
            assert not failed and fetched["tracking_ref"] == "origin/feature/alice"
            assert git(coordinator, "rev-parse", "origin/feature/alice") == feature_sha
            wrong_artifact = {"artifact": {**artifact["artifact"], "commit_sha": "0" * 40}}
            failed, _ = asyncio.run(member_call("submit_artifact", wrong_artifact))
            assert failed

            (member_repo / "app.py").write_text("def value():\n    return 2\n")
            git(member_repo, "add", "app.py")
            git(member_repo, "commit", "-m", "Polish app value")
            advanced_sha = git(member_repo, "rev-parse", "HEAD")
            git(member_repo, "push", "origin", "feature/alice")
            failed, advanced = asyncio.run(
                member_call(
                    "fetch_artifact_branch",
                    {
                        "task_id": task_id,
                        "branch": "feature/alice",
                        "expected_sha": advanced_sha,
                    },
                )
            )
            assert not failed and advanced["updated"] is True
            artifact["artifact"]["commit_sha"] = advanced_sha
            feature_sha = advanced_sha
            failed, submitted = asyncio.run(member_call("submit_artifact", artifact))
            assert not failed
            assert submitted["accepted"] is True, submitted
            assert submitted["artifact"]["commit_sha"] == feature_sha
            assert submitted["artifact"]["changed_paths"] == ["app.py"]

            impostor = admin.post(
                f"/tasks/{task_id}/fetch",
                headers={"Authorization": f"Bearer {tokens['alice']}"},
                json={
                    "member_id": "another-member",
                    "branch": "feature/alice",
                    "expected_sha": feature_sha,
                },
            )
            assert impostor.status_code == 403

            git(member_repo, "switch", "--orphan", "rewritten")
            (member_repo / "app.py").write_text("def value():\n    return 3\n")
            git(member_repo, "add", "app.py")
            git(member_repo, "commit", "-m", "Rewrite published branch")
            rewritten_sha = git(member_repo, "rev-parse", "HEAD")
            git(member_repo, "push", "--force", "origin", "HEAD:refs/heads/feature/alice")
            failed, _ = asyncio.run(
                member_call(
                    "fetch_artifact_branch",
                    {
                        "task_id": task_id,
                        "branch": "feature/alice",
                        "expected_sha": rewritten_sha,
                    },
                )
            )
            assert failed, "A rewritten remote branch must not replace the reviewed ref"
            assert git(coordinator, "rev-parse", "origin/feature/alice") == feature_sha

            packet = admin.get(f"/tasks/{task_id}/inspection", headers=reviewer).json()
            premature = admin.post(
                f"/tasks/{task_id}/merge",
                headers=reviewer,
                json={
                    "author": "reviewer",
                    "rationale": "Reviewed implementation",
                    "expected_version": packet["version"],
                    "expected_target_sha": packet["git"]["target_sha"],
                },
            )
            assert premature.status_code == 422, premature.text
            git(coordinator, "merge", "--no-ff", "--no-edit", "origin/feature/alice")
            packet = admin.get(f"/tasks/{task_id}/inspection", headers=reviewer).json()
            integrated = admin.post(
                f"/tasks/{task_id}/merge",
                headers=reviewer,
                json={
                    "author": "reviewer",
                    "rationale": "Reviewed implementation and tests",
                    "expected_version": packet["version"],
                    "expected_target_sha": packet["git"]["target_sha"],
                },
            )
            assert integrated.status_code == 200, integrated.text
            state = admin.get("/state", headers=owner).json()
            assert state["tasks"][task_id]["status"] == "merged"
            assert state["intents"]["intent-remote-clone"]["status"] == "completed"
            assert git(coordinator, "show", "HEAD:app.py") == "def value():\n    return 2"
            audit = git(coordinator, "log", "--format=%B", "--", ".backbone")
            assert "Backbone-HTTP-Principal: alice" in audit
            assert "Backbone-HTTP-Principal: reviewer" in audit
            git(coordinator, "push", "origin", "main")
            assert git(origin, "rev-parse", "main") == git(coordinator, "rev-parse", "main")
    finally:
        server.terminate()
        try:
            server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)
