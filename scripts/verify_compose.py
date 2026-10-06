"""Exercise Compose HTTP, HTTPS, ledger, and isolated member MCP against real Docker."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import ssl
import subprocess
import sys
import tempfile
import time
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from backbone_conductor.auth import create_token_file, rotate_token_file

PROJECT_ROOT = Path(__file__).resolve().parents[1]
HTTP = build_opener(ProxyHandler({}))


def command(*args: str, env: dict[str, str] | None = None) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=PROJECT_ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.CalledProcessError as error:
        raise RuntimeError(
            f"Command failed: {' '.join(args)}\n{error.stdout}\n{error.stderr}"
        ) from error
    return result.stdout.strip()


def git(repo: Path, *args: str) -> str:
    return command("git", "-C", str(repo), *args)


def compose(
    project: str,
    env: dict[str, str],
    *args: str,
    separate: bool = False,
    tls: bool = False,
    mcp: bool = False,
) -> str:
    files = ["-f", str(PROJECT_ROOT / "compose.yaml")]
    if tls:
        files += ["-f", str(PROJECT_ROOT / "compose.tls.yaml")]
        if separate:
            files += ["-f", str(PROJECT_ROOT / "compose.ledger-tls.yaml")]
    elif separate:
        files += ["-f", str(PROJECT_ROOT / "compose.ledger.yaml")]
    if mcp:
        files += ["-f", str(PROJECT_ROOT / "compose.mcp.yaml")]
    return command("docker", "compose", "-p", project, *files, *args, env=env)


def port_available() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def request(
    port: int,
    path: str,
    token: str | None = None,
    data: dict | None = None,
    certificate: Path | None = None,
) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body = None
    if data is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(data).encode("utf-8")
    scheme = "https" if certificate else "http"
    target = Request(f"{scheme}://127.0.0.1:{port}{path}", data=body, headers=headers)
    opener = HTTP
    if certificate:
        context = ssl.create_default_context(cafile=str(certificate))
        opener = build_opener(ProxyHandler({}), HTTPSHandler(context=context))
    try:
        with opener.open(target, timeout=2) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        with error:
            return error.code, json.load(error)


def wait_healthy(port: int, certificate: Path | None = None) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if request(port, "/health", certificate=certificate)[0] == 200:
                return
        except (OSError, HTTPException):
            pass
        time.sleep(0.2)
    raise AssertionError(f"Compose service on port {port} did not become healthy")


def setup_repo(repo: Path) -> None:
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Compose Verification")
    git(repo, "config", "user.email", "compose@example.invalid")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "commit", "--allow-empty", "-m", "Seed code branch")


def review_intent(
    port: int,
    intent_id: str,
    owner_token: str,
    reviewer_token: str,
    certificate: Path | None = None,
) -> None:
    status, state = request(port, "/state", owner_token, certificate=certificate)
    assert status == 200, (status, state)
    status, reviewed = request(
        port,
        f"/intents/{intent_id}/review",
        reviewer_token,
        {
            "author": "carol",
            "outcome": "accepted",
            "rationale": "Reviewed the deployment intent",
            "expected_version": state["version"],
        },
        certificate,
    )
    assert status == 200 and reviewed["status"] == "accepted", (status, reviewed)
    assert reviewed["reviews"][-1]["reviewed_version"] == state["version"]
    assert reviewed["reviews"][-1]["reviewer"] == "carol"


def verify_member_mcp(
    port: int, token: str, intent_id: str, certificate: Path | None = None
) -> None:
    async def call() -> dict:
        context = ssl.create_default_context(cafile=str(certificate)) if certificate else True
        scheme = "https" if certificate else "http"
        async with httpx.AsyncClient(
            verify=context,
            trust_env=False,
            headers={"Authorization": f"Bearer {token}"},
        ) as http_client:
            async with streamable_http_client(
                f"{scheme}://127.0.0.1:{port}/mcp", http_client=http_client
            ) as (read, write, _session_id):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = {item.name for item in (await session.list_tools()).tools}
                    assert len(tools) == 8 and "fetch_artifact_branch" in tools
                    assert "dispatch_task" not in tools
                    result = await session.call_tool(
                        "create_intent",
                        {
                            "intent_data": {
                                "id": intent_id,
                                "problem": "Verify Compose member MCP",
                                "proposed_outcome": "Persist authenticated member work",
                            }
                        },
                    )
                    assert not result.isError, result
                    return json.loads(result.content[0].text)

    created = asyncio.run(call())
    assert created["id"] == intent_id and created["author"] == "alice", created


def mcp_status(port: int, token: str | None, certificate: Path | None = None) -> int:
    context = ssl.create_default_context(cafile=str(certificate)) if certificate else True
    scheme = "https" if certificate else "http"
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "Mcp-Protocol-Version": "2025-06-18",
    }
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    with httpx.Client(verify=context, trust_env=False, timeout=5) as client:
        response = client.post(
            f"{scheme}://127.0.0.1:{port}/mcp",
            headers=headers,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        return response.status_code


def verify_inline(
    project: str,
    repo: Path,
    auth_dir: Path,
    old_token: str,
    reviewer_token: str,
    member_token: str,
    port: int,
) -> tuple[str, str, str, str]:
    env = {**os.environ, "BACKBONE_REPO": str(repo), "BACKBONE_AUTH_DIR": str(auth_dir)}
    env["BACKBONE_PORT"] = str(port)
    env["BACKBONE_UID"] = str(os.getuid())
    env["BACKBONE_GID"] = str(os.getgid())
    compose(project, env, "build")
    try:
        compose(project, env, "run", "--rm", "conductor", "--repo", "/workspace", "init")
        compose(project, env, "up", "-d", mcp=True)
        wait_healthy(port)
        assert mcp_status(port, None) == 401
        assert mcp_status(port, old_token) == 403
        assert request(port, "/state")[0] == 401
        assert request(port, "/state", old_token)[0] == 200
        status, intent = request(
            port,
            "/intents",
            old_token,
            {"id": "inline-intent", "problem": "Verify inline Compose", "proposed_outcome": "Pass"},
        )
        assert status == 201 and intent["id"] == "inline-intent", (status, intent)
        review_intent(port, intent["id"], old_token, reviewer_token)
        verify_member_mcp(port, member_token, "inline-mcp-intent")
        assert git(repo, "status", "--porcelain") == ""
        assert "Backbone-HTTP-Principal: alice" in git(repo, "log", "-1", "--format=%B")
        rotated = rotate_token_file(
            auth_dir / "backbone-http-tokens.json", repo, "owner", ["alice", "bob"], ["carol"]
        )
        new_tokens = {entry["name"]: entry["token"] for entry in rotated}
        assert request(port, "/state", old_token)[0] == 401
        assert request(port, "/state", reviewer_token)[0] == 401
        assert mcp_status(port, member_token) == 401
        assert mcp_status(port, new_tokens["alice"]) == 200
        assert request(port, "/state", new_tokens["owner"])[0] == 200
        assert request(port, "/state", new_tokens["carol"])[0] == 200
        verify_member_mcp(port, new_tokens["alice"], "inline-rotated-mcp-intent")
        return (
            new_tokens["owner"],
            new_tokens["carol"],
            new_tokens["alice"],
            new_tokens["bob"],
        )
    finally:
        compose(project, env, "down", mcp=True)


def verify_separate(
    project: str,
    repo: Path,
    auth_dir: Path,
    token: str,
    reviewer_token: str,
    member_token: str,
    port: int,
) -> None:
    env = {**os.environ, "BACKBONE_REPO": str(repo), "BACKBONE_AUTH_DIR": str(auth_dir)}
    env["BACKBONE_PORT"] = str(port)
    env["BACKBONE_UID"] = str(os.getuid())
    env["BACKBONE_GID"] = str(os.getgid())
    initial_head = git(repo, "rev-parse", "HEAD")
    try:
        compose(
            project,
            env,
            "run",
            "--rm",
            "conductor",
            "--repo",
            "/workspace",
            "ledger",
            "create",
            separate=True,
        )
        old_ledger_head = git(repo, "rev-parse", "backbone")
        compose(project, env, "up", "-d", separate=True, mcp=True)
        wait_healthy(port)
        assert mcp_status(port, token) == 403
        assert mcp_status(port, member_token) == 200
        status, intent = request(
            port,
            "/intents",
            token,
            {
                "id": "separate-intent",
                "problem": "Verify separate Compose",
                "proposed_outcome": "Pass",
            },
        )
        assert status == 201 and intent["id"] == "separate-intent", (status, intent)
        review_intent(port, intent["id"], token, reviewer_token)
        verify_member_mcp(port, member_token, "separate-mcp-intent")
        assert git(repo, "rev-parse", "HEAD") == initial_head
        assert git(repo, "rev-parse", "backbone") != old_ledger_head
        assert git(repo, "status", "--porcelain") == ""
        assert "Backbone-HTTP-Principal: alice" in git(repo, "log", "-1", "backbone", "--format=%B")
        compose(project, env, "restart", separate=True, mcp=True)
        wait_healthy(port)
        status, state = request(port, "/state", token)
        assert status == 200 and state["intents"]["separate-intent"]["status"] == "accepted"
        assert state["intents"]["separate-mcp-intent"]["author"] == "alice"
    finally:
        compose(project, env, "down", separate=True, mcp=True)


def create_test_certificate(directory: Path) -> Path:
    directory.mkdir(mode=0o700)
    certificate = directory / "server.crt"
    key = directory / "server.key"
    command(
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
        "subjectAltName=DNS:localhost,DNS:conductor,IP:127.0.0.1",
    )
    key.chmod(0o600)
    return certificate


def verify_network_member(
    project: str,
    env: dict[str, str],
    tls_dir: Path,
    member_token: str,
    intent_id: str,
    member: str,
    other_member: str,
    client_uid: int,
    *,
    separate: bool,
) -> None:
    image = compose(
        project, env, "images", "-q", "conductor", separate=separate, tls=True, mcp=True
    )
    assert image, "Compose service image is missing"
    client_env = {
        **os.environ,
        "BACKBONE_TEST_TOKEN": member_token,
        "BACKBONE_TEST_INTENT_ID": intent_id,
        "BACKBONE_TEST_MEMBER": member,
        "BACKBONE_TEST_OTHER_MEMBER": other_member,
        "BACKBONE_TEST_CLIENT_UID": str(client_uid),
        "BACKBONE_TEST_SERVER_UID": env["BACKBONE_UID"],
    }
    command(
        "docker",
        "run",
        "--rm",
        "--network",
        f"{project}_default",
        "--user",
        f"{client_uid}:{client_uid}",
        "--env",
        "BACKBONE_TEST_TOKEN",
        "--env",
        "BACKBONE_TEST_INTENT_ID",
        "--env",
        "BACKBONE_TEST_MEMBER",
        "--env",
        "BACKBONE_TEST_OTHER_MEMBER",
        "--env",
        "BACKBONE_TEST_CLIENT_UID",
        "--env",
        "BACKBONE_TEST_SERVER_UID",
        "--volume",
        f"{tls_dir / 'server.crt'}:/certs/server.crt:ro",
        "--volume",
        f"{PROJECT_ROOT / 'scripts' / 'verify_network_mcp_client.py'}:/probe.py:ro",
        "--entrypoint",
        "python",
        image,
        "/probe.py",
        env=client_env,
    )


def verify_tls(
    project: str,
    repo: Path,
    auth_dir: Path,
    tls_dir: Path,
    token: str,
    reviewer_token: str,
    member_token: str,
    second_member_token: str,
    *,
    separate: bool,
) -> None:
    port = port_available()
    base_port = port_available()
    while base_port == port:
        base_port = port_available()
    env = {
        **os.environ,
        "BACKBONE_REPO": str(repo),
        "BACKBONE_AUTH_DIR": str(auth_dir),
        "BACKBONE_TLS_DIR": str(tls_dir),
        "BACKBONE_PORT": str(base_port),
        "BACKBONE_TLS_PORT": str(port),
        "BACKBONE_UID": str(os.getuid()),
        "BACKBONE_GID": str(os.getgid()),
        "BACKBONE_MCP_ALLOWED_HOSTS": "conductor:8000",
    }
    certificate = tls_dir / "server.crt"
    branch = "backbone" if separate else "HEAD"
    code_head = git(repo, "rev-parse", "HEAD")
    ledger_head = git(repo, "rev-parse", branch)
    try:
        compose(project, env, "config", "--quiet", separate=separate, tls=True, mcp=True)
        compose(project, env, "up", "-d", separate=separate, tls=True, mcp=True)
        wait_healthy(port, certificate)
        assert mcp_status(port, token, certificate) == 403
        assert mcp_status(port, member_token, certificate) == 200
        assert mcp_status(port, second_member_token, certificate) == 200
        assert request(port, "/state", certificate=certificate)[0] == 401
        assert request(port, "/state", token, certificate=certificate)[0] == 200
        intent_id = "tls-separate-intent" if separate else "tls-inline-intent"
        status, intent = request(
            port,
            "/intents",
            token,
            {"id": intent_id, "problem": "Verify Compose HTTPS", "proposed_outcome": "Pass"},
            certificate,
        )
        assert status == 201 and intent["id"] == intent_id, (status, intent)
        review_intent(port, intent_id, token, reviewer_token, certificate)
        mcp_intent_id = "tls-separate-mcp-intent" if separate else "tls-inline-mcp-intent"
        verify_member_mcp(port, member_token, mcp_intent_id, certificate)
        network_intent_id = (
            "tls-separate-network-intent" if separate else "tls-inline-network-intent"
        )
        client_uids = [uid for uid in (10001, 10002, 10003) if uid != int(env["BACKBONE_UID"])]
        verify_network_member(
            project,
            env,
            tls_dir,
            member_token,
            network_intent_id,
            "alice",
            "bob",
            client_uids[0],
            separate=separate,
        )
        assert "Backbone-HTTP-Principal: alice" in git(repo, "log", "-1", branch, "--format=%B")
        second_network_intent_id = f"{network_intent_id}-bob"
        verify_network_member(
            project,
            env,
            tls_dir,
            second_member_token,
            second_network_intent_id,
            "bob",
            "alice",
            client_uids[1],
            separate=separate,
        )
        assert git(repo, "rev-parse", branch) != ledger_head
        assert "Backbone-HTTP-Principal: bob" in git(repo, "log", "-1", branch, "--format=%B")
        assert git(repo, "status", "--porcelain") == ""
        if separate:
            assert git(repo, "rev-parse", "HEAD") == code_head
        try:
            with HTTP.open(f"https://127.0.0.1:{port}/health", timeout=2):
                pass
        except URLError:
            pass
        else:
            raise AssertionError("Untrusted TLS certificate was accepted")
        for plain_port in (port, base_port):
            try:
                with HTTP.open(f"http://127.0.0.1:{plain_port}/health", timeout=2) as response:
                    assert response.status != 200
            except (OSError, HTTPException):
                pass
        compose(project, env, "restart", separate=separate, tls=True, mcp=True)
        wait_healthy(port, certificate)
        status, state = request(port, "/state", token, certificate=certificate)
        assert status == 200 and state["intents"][intent_id]["status"] == "accepted"
        assert state["intents"][mcp_intent_id]["author"] == "alice"
        assert state["intents"][network_intent_id]["author"] == "alice"
        assert state["intents"][second_network_intent_id]["author"] == "bob"
        assert f"{network_intent_id}-spoof" not in state["intents"]
        assert f"{second_network_intent_id}-spoof" not in state["intents"]
    except BaseException:
        try:
            print(
                compose(project, env, "logs", "--no-color", separate=separate, tls=True, mcp=True)
            )
        except Exception:
            pass
        raise
    finally:
        compose(project, env, "down", separate=separate, tls=True, mcp=True)


def main() -> None:
    temporary_root = "/private/tmp" if sys.platform == "darwin" else "/tmp"
    with tempfile.TemporaryDirectory(prefix="backbone-compose-", dir=temporary_root) as temporary:
        root = Path(temporary)
        inline_repo = root / "inline"
        separate_repo = root / "separate"
        setup_repo(inline_repo)
        setup_repo(separate_repo)
        auth_dir = root / "auth"
        auth_dir.mkdir(mode=0o700)
        issued = create_token_file(
            auth_dir / "backbone-http-tokens.json",
            inline_repo,
            "owner",
            ["alice", "bob"],
            ["carol"],
        )
        old_tokens = {entry["name"]: entry["token"] for entry in issued}
        project = f"backbone-compose-{os.getpid()}"
        new_token, reviewer_token, member_token, second_member_token = verify_inline(
            project,
            inline_repo,
            auth_dir,
            old_tokens["owner"],
            old_tokens["carol"],
            old_tokens["alice"],
            port_available(),
        )
        verify_separate(
            project,
            separate_repo,
            auth_dir,
            new_token,
            reviewer_token,
            member_token,
            port_available(),
        )
        tls_dir = root / "tls"
        create_test_certificate(tls_dir)
        verify_tls(
            project,
            inline_repo,
            auth_dir,
            tls_dir,
            new_token,
            reviewer_token,
            member_token,
            second_member_token,
            separate=False,
        )
        verify_tls(
            project,
            separate_repo,
            auth_dir,
            tls_dir,
            new_token,
            reviewer_token,
            member_token,
            second_member_token,
            separate=True,
        )
    print("Compose HTTP/HTTPS, ledger, and two-UID network member MCP verification passed")


if __name__ == "__main__":
    main()
