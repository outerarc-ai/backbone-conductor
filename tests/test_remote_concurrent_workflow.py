"""Two isolated code clones submit through one coordinator at the same time."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
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
from backbone_conductor.cli import main
from backbone_conductor.service import Conductor


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _member_worker(
    repo: Path, url: str, name: str, task_id: str, token: str, barrier, results
) -> None:
    try:
        branch = f"feature/{name}"
        git(repo, "switch", "-c", branch)
        (repo / f"{name}.py").write_text(f"def {name}():\n    return '{name}'\n")
        git(repo, "add", f"{name}.py")
        git(repo, "commit", "-m", f"Implement {name}")
        commit_sha = git(repo, "rev-parse", "HEAD")
        git(repo, "push", "origin", f"HEAD:refs/heads/{branch}")
        barrier.wait(timeout=30)

        async def submit() -> dict:
            async with httpx.AsyncClient(
                headers={"Authorization": f"Bearer {token}"}, trust_env=False, timeout=20
            ) as client:
                async with streamable_http_client(f"{url}/mcp", http_client=client) as (
                    read,
                    write,
                    _,
                ):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        context = await session.call_tool("get_my_task", {})
                        assert not context.isError, context
                        context_data = json.loads(context.content[0].text)
                        assert [task["id"] for task in context_data["tasks"]] == [task_id]
                        started = await session.call_tool("start_task", {"task_id": task_id})
                        assert not started.isError, started
                        fetched = await session.call_tool(
                            "fetch_artifact_branch",
                            {"task_id": task_id, "branch": branch, "expected_sha": commit_sha},
                        )
                        assert not fetched.isError, fetched
                        tracking = json.loads(fetched.content[0].text)["tracking_ref"]
                        checked = await session.call_tool(
                            "submit_artifact",
                            {
                                "artifact": {
                                    "intent_id": f"intent-{name}",
                                    "branch": tracking,
                                    "base_ref": "main",
                                    "summary": f"Implement {name}",
                                    "commit_sha": commit_sha,
                                }
                            },
                        )
                        assert not checked.isError, checked
                        body = json.loads(checked.content[0].text)
                        assert body["accepted"], body
                        return body

        result = asyncio.run(submit())
        results.put(
            {
                "name": name,
                "task_id": task_id,
                "commit_sha": commit_sha,
                "artifact_sha": result["artifact"]["commit_sha"],
                "changed_paths": result["artifact"]["changed_paths"],
            }
        )
    except Exception as exc:
        results.put({"name": name, "error": f"{type(exc).__name__}: {exc}"})


def test_two_remote_member_clones_submit_concurrently_and_merge_separately(tmp_path: Path, capsys):
    origin = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(origin)], check=True, capture_output=True
    )
    coordinator = tmp_path / "coordinator"
    subprocess.run(["git", "clone", str(origin), str(coordinator)], check=True, capture_output=True)
    git(coordinator, "config", "user.name", "Coordinator")
    git(coordinator, "config", "user.email", "coordinator@example.invalid")
    git(coordinator, "config", "commit.gpgsign", "false")
    (coordinator / "README.md").write_text("Shared project\n")
    git(coordinator, "add", "README.md")
    git(coordinator, "commit", "-m", "Initial project")
    git(coordinator, "push", "-u", "origin", "main")
    Conductor(coordinator).initialize()

    credentials = tmp_path / "credentials.json"
    issued = create_token_file(credentials, coordinator, "owner", ["alice", "bob"], ["carol"])
    tokens = {entry["name"]: entry["token"] for entry in issued}
    reviewer_token = tmp_path / "carol.token"
    reviewer_token.write_text(tokens["carol"] + "\n", encoding="ascii")
    reviewer_token.chmod(0o600)
    member_token = tmp_path / "alice.token"
    member_token.write_text(tokens["alice"] + "\n", encoding="ascii")
    member_token.chmod(0o600)
    with socket.socket() as listener:
        try:
            listener.bind(("127.0.0.1", 0))
        except PermissionError:
            if os.environ.get("BACKBONE_REQUIRE_LIVE_HTTP") == "1":
                raise
            pytest.skip("This sandbox does not permit loopback listening sockets")
        port = listener.getsockname()[1]
    url = f"http://127.0.0.1:{port}"

    def reviewer_command(*arguments: str) -> dict | list:
        assert (
            main(
                [
                    "reviewer",
                    "--url",
                    url,
                    "--token-file",
                    str(reviewer_token),
                    *arguments,
                ]
            )
            == 0
        )
        return json.loads(capsys.readouterr().out)

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

        names = ("alice", "bob")
        task_ids = {}
        with httpx.Client(base_url=url, trust_env=False, timeout=20) as client:
            owner = {"Authorization": f"Bearer {tokens['owner']}"}
            assert reviewer_command("whoami") == {"name": "carol", "role": "reviewer"}
            reviewer_workspace = tmp_path / "reviewer-workspace"
            reviewer_workspace.mkdir()
            remote_reviewer = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "backbone_conductor",
                    "reviewer",
                    "--url",
                    url,
                    "--token-file",
                    str(reviewer_token),
                    "whoami",
                ],
                cwd=reviewer_workspace,
                env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            assert json.loads(remote_reviewer.stdout) == {"name": "carol", "role": "reviewer"}
            assert not (reviewer_workspace / ".git").exists()
            assert (
                main(
                    [
                        "reviewer",
                        "--url",
                        url,
                        "--token-file",
                        str(member_token),
                        "state",
                    ]
                )
                == 1
            )
            denied = capsys.readouterr()
            assert "reviewer role" in denied.err
            assert tokens["alice"] not in denied.err
            for name in names:
                created = client.post(
                    "/intents",
                    headers=owner,
                    json={
                        "id": f"intent-{name}",
                        "problem": f"Add {name}'s function",
                        "proposed_outcome": f"The {name} function is available",
                        "affected_paths": [f"{name}.py"],
                    },
                )
                assert created.status_code == 201, created.text
                version = reviewer_command("state")["version"]
                reviewed = reviewer_command(
                    "review-intent",
                    f"intent-{name}",
                    "--outcome",
                    "accepted",
                    "--rationale",
                    "The scoped change is ready",
                    "--version",
                    version,
                )
                assert reviewed["status"] == "accepted"
                dispatched = client.post(
                    "/tasks",
                    headers=owner,
                    json={"intent_id": f"intent-{name}", "member_id": name},
                )
                assert dispatched.status_code == 201, dispatched.text
                task_ids[name] = dispatched.json()["id"]

            git(coordinator, "push", "origin", "main")
            clones = {}
            for name in names:
                clone = tmp_path / f"{name}-clone"
                subprocess.run(
                    ["git", "clone", str(origin), str(clone)], check=True, capture_output=True
                )
                git(clone, "config", "user.name", name.title())
                git(clone, "config", "user.email", f"{name}@example.invalid")
                clones[name] = clone

            context = multiprocessing.get_context("spawn")
            barrier = context.Barrier(len(names))
            results = context.Queue()
            workers = [
                context.Process(
                    target=_member_worker,
                    args=(clones[name], url, name, task_ids[name], tokens[name], barrier, results),
                )
                for name in names
            ]
            try:
                for worker in workers:
                    worker.start()
                reports = [results.get(timeout=60) for _ in workers]
                for worker in workers:
                    worker.join(timeout=5)
                assert all(worker.exitcode == 0 for worker in workers), reports
                assert not any("error" in report for report in reports), reports
            finally:
                for worker in workers:
                    if worker.is_alive():
                        worker.terminate()
                        worker.join(timeout=5)
                results.close()
                results.join_thread()

            by_name = {report["name"]: report for report in reports}
            assert set(by_name) == set(names)
            assert git(coordinator, "rev-parse", "HEAD") != git(origin, "rev-parse", "main")
            inspections = {}
            for name in names:
                report = by_name[name]
                assert report["task_id"] == task_ids[name]
                assert report["artifact_sha"] == report["commit_sha"]
                assert report["changed_paths"] == [f"{name}.py"]
                assert (
                    git(coordinator, "rev-parse", f"origin/feature/{name}") == report["commit_sha"]
                )
                inspection = reviewer_command("inspect", task_ids[name])
                inspections[name] = inspection
                assert f"+def {name}():" in inspection["diff"]["patch"]
                assert inspection["git"]["integrated_into_target"] is False
                complete = reviewer_command("inspect", task_ids[name], "--full")
                assert complete["diff"]["patch"] == inspection["diff"]["patch"]
                assert complete["diff"]["truncated"] is False
                if name == "alice":
                    assert (
                        main(
                            [
                                "reviewer",
                                "--url",
                                url,
                                "--token-file",
                                str(reviewer_token),
                                "inspect",
                                task_ids[name],
                                "--full",
                                "--format",
                                "text",
                            ]
                        )
                        == 0
                    )
                    rendered = capsys.readouterr().out
                    assert "Problem:\n  Add alice's function" in rendered
                    assert "Intent: intent-alice (in_progress)" in rendered
                    assert "\n+def alice():\n" in rendered
                    assert "Integrated target diff: pending Git merge." in rendered

            assert (
                main(
                    [
                        "reviewer",
                        "--url",
                        url,
                        "--token-file",
                        str(reviewer_token),
                        "approve",
                        task_ids["alice"],
                        "--rationale",
                        "Reviewed before Git integration",
                        "--version",
                        inspections["alice"]["version"],
                        "--target-sha",
                        inspections["alice"]["git"]["target_sha"],
                    ]
                )
                == 1
            )
            assert "Merge the reviewed artifact" in capsys.readouterr().err

            for name in names:
                git(coordinator, "merge", "--no-ff", "--no-edit", f"origin/feature/{name}")
                inspection = reviewer_command("inspect", task_ids[name])
                assert inspection["git"]["integrated_into_target"] is True
                complete = reviewer_command("inspect", task_ids[name], "--full")
                assert f"+def {name}():" in complete["target_diff"]["patch"]
                assert complete["target_diff"]["truncated"] is False
                if name == "alice":
                    changed = client.post(
                        "/intents",
                        headers=owner,
                        json={
                            "id": "intent-after-inspection",
                            "problem": "Check concurrent review freshness",
                            "proposed_outcome": "Require reinspection",
                        },
                    )
                    assert changed.status_code == 201, changed.text
                    assert (
                        main(
                            [
                                "reviewer",
                                "--url",
                                url,
                                "--token-file",
                                str(reviewer_token),
                                "approve",
                                task_ids[name],
                                "--rationale",
                                "Stale inspection",
                                "--version",
                                inspection["version"],
                                "--target-sha",
                                inspection["git"]["target_sha"],
                            ]
                        )
                        == 1
                    )
                    assert "Backbone changed since task inspection" in capsys.readouterr().err
                    inspection = reviewer_command("inspect", task_ids[name])
                approved = reviewer_command(
                    "approve",
                    task_ids[name],
                    "--rationale",
                    f"Reviewed {name}'s final code and current decisions",
                    "--version",
                    inspection["version"],
                    "--target-sha",
                    inspection["git"]["target_sha"],
                )
                assert approved["task"]["status"] == "merged"
            for name in names:
                archived = reviewer_command("inspect", task_ids[name], "--full")
                assert archived["inspection_kind"] == "approval"
                assert archived["current_task_status"] == "merged"
                assert archived["version"] == archived["approval"]["reviewed_version"]
                assert archived["git"]["target_sha"] == archived["approval"]["target_sha"]
                assert f"+def {name}():" in archived["target_diff"]["patch"]
                if name == "alice":
                    assert archived["git"]["current_target_sha"] != archived["git"]["target_sha"]
                    assert (
                        main(
                            [
                                "reviewer",
                                "--url",
                                url,
                                "--token-file",
                                str(reviewer_token),
                                "inspect",
                                task_ids[name],
                                "--full",
                                "--format",
                                "text",
                            ]
                        )
                        == 0
                    )
                    rendered = capsys.readouterr().out
                    assert "Inspection: approval" in rendered
                    assert "Task: " + task_ids[name] + " (now merged)" in rendered
                    assert "Approval decision:" in rendered
            state = client.get("/state", headers=owner).json()
            assert {state["tasks"][task_ids[name]]["status"] for name in names} == {"merged"}
            assert {state["intents"][f"intent-{name}"]["status"] for name in names} == {"completed"}
            for name in names:
                assert git(coordinator, "show", f"HEAD:{name}.py") == (
                    f"def {name}():\n    return '{name}'"
                )
            audit = git(coordinator, "log", "--format=%B", "--", ".backbone")
            for name in names:
                assert f"Backbone-HTTP-Principal: {name}" in audit
            assert "Backbone-HTTP-Principal: carol" in audit
            assert all(token not in audit for token in tokens.values())
            git(coordinator, "push", "origin", "main")
            assert git(origin, "rev-parse", "main") == git(coordinator, "rev-parse", "main")
    finally:
        server.terminate()
        try:
            server.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)
