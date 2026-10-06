"""Exercise real HTTP, Git, and bearer identities across separate OS processes."""

from __future__ import annotations

import multiprocessing
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from backbone_conductor.auth import (
    add_principal_token,
    create_token_file,
    revoke_principal_token,
    rotate_principal_token,
    rotate_token_file,
)
from backbone_conductor.service import Conductor


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout.strip()


def _member_create(url: str, name: str, token: str, barrier, results) -> None:
    try:
        barrier.wait(timeout=20)
        with httpx.Client(base_url=url, timeout=15, trust_env=False) as client:
            response = client.post(
                "/intents",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "id": f"intent-{name}",
                    "problem": f"Independent request from {name}",
                    "proposed_outcome": f"Complete {name}'s work",
                    "affected_paths": [f"{name}.py"],
                },
            )
            results.put({"member": name, "status": response.status_code, "body": response.json()})
    except Exception as exc:
        results.put({"member": name, "error": f"{type(exc).__name__}: {exc}"})


def _member_start(
    url: str, name: str, token: str, own_task: str, other_task: str, barrier, results
) -> None:
    try:
        barrier.wait(timeout=20)
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(base_url=url, timeout=15, trust_env=False) as client:
            listing = client.get("/tasks", headers=headers)
            forbidden = client.get(f"/tasks/{other_task}", headers=headers)
            started = client.post(
                f"/tasks/{own_task}/start", headers=headers, json={"member_id": name}
            )
            results.put(
                {
                    "member": name,
                    "list_status": listing.status_code,
                    "visible_ids": [task["id"] for task in listing.json()["tasks"]],
                    "other_status": forbidden.status_code,
                    "start_status": started.status_code,
                    "task_status": started.json().get("status"),
                }
            )
    except Exception as exc:
        results.put({"member": name, "error": f"{type(exc).__name__}: {exc}"})


def _review_intent(url: str, token: str, intent_id: str, version: str, barrier, results) -> None:
    try:
        barrier.wait(timeout=20)
        with httpx.Client(base_url=url, timeout=15, trust_env=False) as client:
            response = client.post(
                f"/intents/{intent_id}/review",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "author": "carol",
                    "outcome": "accepted",
                    "rationale": "Scope and constraints are clear",
                    "expected_version": version,
                },
            )
            results.put({"status": response.status_code, "body": response.json()})
    except Exception as exc:
        results.put({"error": f"{type(exc).__name__}: {exc}"})


def _member_submit(url: str, token: str, task_id: str, barrier, results) -> None:
    try:
        barrier.wait(timeout=20)
        with httpx.Client(base_url=url, timeout=15, trust_env=False) as client:
            response = client.post(
                f"/tasks/{task_id}/submit",
                headers={"Authorization": f"Bearer {token}"},
                json={
                    "member_id": "alice",
                    "artifact": {
                        "branch": "feature/alice",
                        "summary": "Implement Alice's request",
                    },
                },
            )
            results.put({"status": response.status_code, "body": response.json()})
    except Exception as exc:
        results.put({"error": f"{type(exc).__name__}: {exc}"})


def _review_merge(url: str, token: str, task_id: str, barrier, results) -> None:
    try:
        barrier.wait(timeout=20)
        with httpx.Client(base_url=url, timeout=15, trust_env=False) as client:
            headers = {"Authorization": f"Bearer {token}"}
            packet = client.get(f"/tasks/{task_id}/inspection", headers=headers).json()
            response = client.post(
                f"/tasks/{task_id}/merge",
                headers=headers,
                json={
                    "author": "carol",
                    "rationale": "Reviewed the merged implementation",
                    "expected_version": packet["version"],
                    "expected_target_sha": packet["git"]["target_sha"],
                },
            )
            results.put({"status": response.status_code, "body": response.json()})
    except Exception as exc:
        results.put({"error": f"{type(exc).__name__}: {exc}"})


def _run_members(context, worker, assignments: list[tuple]) -> list[dict]:
    barrier = context.Barrier(len(assignments))
    results = context.Queue()
    processes = [
        context.Process(target=worker, args=(*assignment, barrier, results))
        for assignment in assignments
    ]
    try:
        for process in processes:
            process.start()
        reports = [results.get(timeout=35) for _ in processes]
        for process in processes:
            process.join(timeout=5)
        assert all(process.exitcode == 0 for process in processes), reports
        assert not any("error" in report for report in reports), reports
        return reports
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        results.close()
        results.join_thread()


def test_authenticated_live_server_coordinates_two_member_processes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Live HTTP Test")
    _git(repo, "config", "user.email", "http@example.invalid")
    _git(repo, "config", "commit.gpgsign", "false")
    Conductor(repo).initialize()
    credentials = tmp_path / "credentials.json"
    issued = create_token_file(credentials, repo, "owner", ["alice", "bob"], ["carol"])
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
    environment = os.environ.copy()
    source_path = str(Path(__file__).resolve().parents[1] / "src")
    environment["PYTHONPATH"] = source_path
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
        ],
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        last_response = None
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise AssertionError(f"HTTP server exited: {server.stderr.read()}")
            try:
                with httpx.Client(trust_env=False, timeout=0.5) as probe:
                    last_response = probe.get(f"{url}/health")
                if last_response.status_code == 200:
                    break
            except httpx.RequestError:
                time.sleep(0.1)
        else:
            server.terminate()
            _, diagnostic = server.communicate(timeout=5)
            raise AssertionError(
                f"HTTP server did not become ready: response={last_response}; {diagnostic}"
            )

        context = multiprocessing.get_context("spawn")
        names = ("alice", "bob")
        created = _run_members(
            context,
            _member_create,
            [(url, name, tokens[name]) for name in names],
        )
        assert {report["member"] for report in created} == set(names)
        assert all(report["status"] == 201 for report in created)
        assert all(report["body"]["author"] == report["member"] for report in created)

        headers = {"Authorization": f"Bearer {tokens['owner']}"}
        task_ids = {}
        with httpx.Client(base_url=url, timeout=15, trust_env=False) as admin:
            state = admin.get("/state", headers=headers)
            assert state.status_code == 200
            assert set(state.json()["intents"]) == {f"intent-{name}" for name in names}
            observed_version = state.json()["version"]
            for name in names:
                intent_id = f"intent-{name}"
                reviewed = _run_members(
                    context,
                    _review_intent,
                    [(url, tokens["carol"], intent_id, observed_version)],
                )[0]
                if name == "bob":
                    assert reviewed["status"] == 422, reviewed
                    assert "changed" in reviewed["body"]["detail"]
                    observed_version = admin.get("/state", headers=headers).json()["version"]
                    reviewed = _run_members(
                        context,
                        _review_intent,
                        [(url, tokens["carol"], intent_id, observed_version)],
                    )[0]
                assert reviewed["status"] == 200, reviewed
                assert reviewed["body"]["status"] == "accepted"
                assert reviewed["body"]["reviews"][-1]["reviewed_version"] == observed_version
                dispatched = admin.post(
                    "/tasks",
                    headers=headers,
                    json={"intent_id": intent_id, "member_id": name},
                )
                assert dispatched.status_code == 201, dispatched.text
                task_ids[name] = dispatched.json()["id"]

        started = _run_members(
            context,
            _member_start,
            [
                (
                    url,
                    name,
                    tokens[name],
                    task_ids[name],
                    task_ids["bob" if name == "alice" else "alice"],
                )
                for name in names
            ],
        )
        assert all(report["list_status"] == 200 for report in started)
        assert all(report["visible_ids"] == [task_ids[report["member"]]] for report in started)
        assert all(report["other_status"] == 403 for report in started)
        assert all(report["start_status"] == 200 for report in started)
        assert all(report["task_status"] == "in_progress" for report in started)
        assert {task.member_id for task in Conductor(repo).store.read().tasks.values()} == set(
            names
        )
        assert _git(repo, "rev-list", "--count", "HEAD") == "9"
        assert _git(repo, "status", "--porcelain") == ""
        history = _git(repo, "log", "--format=%B", "--", ".backbone")
        assert "Backbone-HTTP-Principal: alice" in history
        assert "Backbone-HTTP-Principal: bob" in history
        assert "Backbone-HTTP-Principal: owner" in history
        assert "Backbone-HTTP-Principal: carol" in history
        assert "Backbone-HTTP-Role: reviewer" in history
        assert all(token not in history for token in tokens.values())
        for name in names:
            creation = _git(
                repo,
                "log",
                "-1",
                "--format=%B",
                "--grep",
                f"backbone: intent intent-{name} created",
            )
            assert f"Backbone-HTTP-Principal: {name}" in creation
            assert "Backbone-HTTP-Role: member" in creation
        dispatched = _git(repo, "log", "-1", "--format=%B", "--grep", "dispatched to bob")
        assert "Backbone-HTTP-Principal: owner" in dispatched
        assert "Backbone-HTTP-Role: admin" in dispatched
        initial = _git(repo, "rev-list", "--max-parents=0", "HEAD")
        assert "Backbone-HTTP-" not in _git(repo, "show", "-s", "--format=%B", initial)
        _git(repo, "switch", "-c", "feature/alice")
        (repo / "alice.py").write_text("def alice():\n    return 'ready'\n", encoding="utf-8")
        _git(repo, "add", "alice.py")
        _git(repo, "commit", "-m", "Implement Alice request")
        artifact_commit = _git(repo, "rev-parse", "HEAD")
        _git(repo, "switch", "main")
        submitted = _run_members(
            context, _member_submit, [(url, tokens["alice"], task_ids["alice"])]
        )[0]
        assert submitted["status"] == 200 and submitted["body"]["accepted"], submitted
        assert submitted["body"]["artifact"]["commit_sha"] == artifact_commit
        premature = _run_members(
            context, _review_merge, [(url, tokens["carol"], task_ids["alice"])]
        )[0]
        assert premature["status"] == 422, premature
        assert "Merge the reviewed" in premature["body"]["detail"]
        _git(repo, "merge", "--no-ff", "--no-edit", "feature/alice")
        approved = _run_members(
            context, _review_merge, [(url, tokens["carol"], task_ids["alice"])]
        )[0]
        assert approved["status"] == 200, approved
        assert approved["body"]["task"]["status"] == "merged"
        assert Conductor(repo).state()["intents"]["intent-alice"]["status"] == "completed"
        review_commit = _git(repo, "log", "-1", "--format=%B")
        assert "Backbone-HTTP-Principal: carol" in review_commit
        assert "Backbone-HTTP-Role: reviewer" in review_commit
        one = rotate_principal_token(credentials, repo, "alice")
        dave = add_principal_token(credentials, repo, "dave", "member")
        with httpx.Client(base_url=url, timeout=15, trust_env=False) as client:
            assert (
                client.get(
                    "/whoami", headers={"Authorization": f"Bearer {tokens['alice']}"}
                ).status_code
                == 401
            )
            assert client.get(
                "/whoami", headers={"Authorization": f"Bearer {one['token']}"}
            ).json() == {"name": "alice", "role": "member"}
            for name in ("owner", "bob", "carol"):
                assert (
                    client.get(
                        "/whoami", headers={"Authorization": f"Bearer {tokens[name]}"}
                    ).status_code
                    == 200
                )
            assert client.get(
                "/whoami", headers={"Authorization": f"Bearer {dave['token']}"}
            ).json() == {"name": "dave", "role": "member"}
            assert revoke_principal_token(credentials, repo, "dave") == {
                "name": "dave",
                "role": "member",
            }
            assert (
                client.get(
                    "/whoami", headers={"Authorization": f"Bearer {dave['token']}"}
                ).status_code
                == 401
            )
            assert (
                client.get(
                    "/whoami", headers={"Authorization": f"Bearer {tokens['carol']}"}
                ).status_code
                == 200
            )
        rotated = rotate_token_file(credentials, repo, "owner", ["alice", "bob"], ["carol"])
        new_owner_token = next(item["token"] for item in rotated if item["name"] == "owner")
        with httpx.Client(base_url=url, timeout=15, trust_env=False) as client:
            assert client.get("/state", headers=headers).status_code == 401
            assert (
                client.get(
                    "/state", headers={"Authorization": f"Bearer {new_owner_token}"}
                ).status_code
                == 200
            )
    finally:
        if server.poll() is None:
            server.terminate()
        try:
            server.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.communicate(timeout=5)
