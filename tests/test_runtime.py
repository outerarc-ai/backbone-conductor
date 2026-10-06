"""Advisory DSH integration tests without network access or paid model calls."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import ModuleType, SimpleNamespace

import pytest

from backbone_conductor.dsh_agent import (
    DSHCoordinatorRunner,
    DSHMemberRunner,
    DSHRemoteMemberRunner,
)
from backbone_conductor.runtime import DSHReviewer, _review_patch
from backbone_conductor.service import Conductor


def initialized_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "project"
    repo.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.name", "Runtime Tests"),
        ("config", "user.email", "runtime@example.invalid"),
        ("config", "commit.gpgsign", "false"),
    ):
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    Conductor(repo).initialize()
    return repo


@pytest.fixture
def harness_stub(monkeypatch):
    state = SimpleNamespace(
        response='{"verdict":"aligned","rationale":"Export follows the requested contract"}',
        finish_reason="completed",
        run_error=None,
        start_error=None,
        closed=False,
        options=None,
        workspace=None,
        patch=None,
        prompt=None,
    )

    class HarnessError(Exception):
        pass

    class Harness:
        def __init__(self, **kwargs):
            state.options = kwargs
            state.workspace = Path(kwargs["cwd"])

        def start(self):
            if state.start_error is not None:
                raise state.start_error

        def __enter__(self):
            self.start()
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self):
            state.closed = True

        def run(self, prompt):
            self.start()
            state.patch = json.loads(Path(state.options["patches"][0]).read_text())
            state.prompt = prompt
            if state.run_error is not None:
                raise state.run_error
            return SimpleNamespace(
                final_response=state.response,
                finish_reason=state.finish_reason,
                session_id="session-review-001",
            )

    module = ModuleType("deepseek_harness")
    module.DeepSeekHarness = Harness
    module.HarnessError = HarnessError
    errors = ModuleType("deepseek_harness.errors")
    errors.HarnessError = HarnessError
    monkeypatch.setitem(sys.modules, "deepseek_harness", module)
    monkeypatch.setitem(sys.modules, "deepseek_harness.errors", errors)
    state.HarnessError = HarnessError
    return state


def test_review_isolated_workspace_explicit_home_and_advisory_output(
    tmp_path: Path, monkeypatch, harness_stub
) -> None:
    monkeypatch.setattr(
        "backbone_conductor.runtime.monotonic_ns", iter([1_000_000_000, 1_250_000_000]).__next__
    )
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "existing-shared-home"))
    before_home = os.environ["DSH_HOME"]
    home = tmp_path / "dedicated-review-home"
    context = {"intent": {"problem": "导出数据"}, "constraints": ["No network requests"]}
    diff = "diff --git a/export.py b/export.py\n+def export(): return []\n"
    review = DSHReviewer(home, "chosen-model", "chosen-provider").review(context, diff)
    assert review == {
        "verdict": "aligned",
        "rationale": "Export follows the requested contract",
        "concerns": [],
        "runtime": {
            "elapsed_ms": 250.0,
            "session_id": "session-review-001",
            "finish_reason": "completed",
        },
    }
    assert json.dumps({"context": context, "diff": diff}, ensure_ascii=False) in harness_stub.prompt
    assert harness_stub.options["dsh_home"] == str(home.resolve())
    assert harness_stub.options["model"] == "chosen-model"
    assert harness_stub.options["provider"] == "chosen-provider"
    assert harness_stub.options["profile"] == "sdk-minimal"
    assert harness_stub.options["request_timeout_seconds"] == 120
    assert harness_stub.patch == [
        {
            "id": "sandbox-policy",
            "config": {"mode": "read-only", "workspaceRoot": str(harness_stub.workspace)},
        },
        {"id": "persistent-bash", "disabled": True},
        {"id": "persistent-pwsh", "disabled": True},
    ]
    assert harness_stub.workspace != home
    assert "advisory" in harness_stub.prompt
    assert "untrusted" in harness_stub.prompt
    assert harness_stub.closed
    assert not harness_stub.workspace.exists()
    assert os.environ["DSH_HOME"] == before_home


def test_review_accepts_fenced_json(tmp_path: Path, harness_stub) -> None:
    harness_stub.response = '```json\n{"verdict":"concerns","rationale":"Missing validation","concerns":["No input validation"]}\n```'
    result = DSHReviewer(tmp_path, "test").review({}, "")
    assert result["verdict"] == "concerns"
    assert result["concerns"] == ["No input validation"]


def test_conflict_advice_is_structured_and_tool_free(tmp_path: Path, harness_stub) -> None:
    harness_stub.response = json.dumps(
        {
            "verdict": "conflict",
            "rationale": "Both plans change the export contract",
            "evidence": ["The consumers expect different return types"],
            "coordination": ["Agree one return type before implementation"],
        }
    )
    context = {"intents": [{"id": "intent-a"}, {"id": "intent-b"}]}
    advice = DSHReviewer(tmp_path / "private-home", "test-model").advise_conflict(context)
    assert advice["verdict"] == "conflict"
    assert advice["evidence"] == ["The consumers expect different return types"]
    assert advice["runtime"]["finish_reason"] == "completed"
    assert json.dumps(context, ensure_ascii=False) in harness_stub.prompt
    assert "Do not use tools" in harness_stub.prompt
    assert "cannot create, resolve, or approve" in harness_stub.prompt
    assert harness_stub.patch == _review_patch(harness_stub.workspace)
    assert harness_stub.closed


@pytest.mark.parametrize(
    "response",
    [
        '{"verdict":"approved","rationale":"Proceed"}',
        '{"verdict":"compatible","rationale":""}',
        '{"verdict":"conflict","rationale":"Needs coordination","approve_merge":true}',
    ],
)
def test_conflict_advice_rejects_invalid_model_output(
    tmp_path: Path, harness_stub, response: str
) -> None:
    harness_stub.response = response
    with pytest.raises(ValueError, match="invalid conflict advice"):
        DSHReviewer(tmp_path / "private-home", "test-model").advise_conflict({})
    assert harness_stub.closed


def test_installed_sdk_conflict_advice_with_local_mock_provider(
    tmp_path: Path, monkeypatch, mock_dsh_tool_provider
) -> None:
    if os.environ.get("BACKBONE_REQUIRE_DSH_MCP") != "1":
        pytest.skip("set BACKBONE_REQUIRE_DSH_MCP=1 for the installed SDK advice check")
    pytest.importorskip("deepseek_harness")
    response = json.dumps(
        {
            "verdict": "conflict",
            "rationale": "The two interfaces disagree",
            "evidence": ["One plan removes an API the other extends"],
            "coordination": ["Agree a transition plan"],
        }
    )
    with mock_dsh_tool_provider(None, {}, response) as (provider_url, requests):
        monkeypatch.setenv("DEEPSEEK_BASE_URL", provider_url)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "local-conflict-mock-key")
        advice = DSHReviewer(tmp_path / "advice-home", "mock-model").advise_conflict(
            {"intents": [{"id": "intent-left"}, {"id": "intent-right"}]}
        )
    assert advice["verdict"] == "conflict"
    assert advice["runtime"]["finish_reason"] == "completed"
    assert len(requests) == 1
    assert "intent-left" in json.dumps(requests[0]["messages"])
    assert "intent-right" in json.dumps(requests[0]["messages"])


@pytest.mark.parametrize(
    "response",
    [
        "I approve; merge immediately.",
        "[]",
        '{"verdict":"approved","rationale":"Looks fine"}',
        '{"verdict":"aligned","rationale":"Looks fine","approve_merge":true}',
        '{"verdict":"aligned","rationale":""}',
    ],
)
def test_malformed_review_cannot_become_approval(tmp_path: Path, harness_stub, response) -> None:
    harness_stub.response = response
    with pytest.raises(ValueError, match="invalid semantic review"):
        DSHReviewer(tmp_path, "test").review({}, "")
    assert harness_stub.closed
    assert not harness_stub.workspace.exists()


@pytest.mark.parametrize("reason", [None, "max-tokens", "error"])
def test_incomplete_turn_rejected_even_if_json_looks_valid(
    tmp_path: Path, harness_stub, reason
) -> None:
    harness_stub.finish_reason = reason
    with pytest.raises(ValueError, match="did not complete"):
        DSHReviewer(tmp_path, "test").review({}, "")
    assert harness_stub.closed
    assert not harness_stub.workspace.exists()


def test_timeout_closes_harness_and_removes_context(tmp_path: Path, harness_stub) -> None:
    harness_stub.run_error = TimeoutError("model review timed out")
    with pytest.raises(ValueError, match="TimeoutError.*no approval"):
        DSHReviewer(tmp_path, "test").review({}, "")
    assert harness_stub.closed
    assert not harness_stub.workspace.exists()


def test_startup_failure_closes_harness_and_returns_controlled_error(
    tmp_path: Path, harness_stub
) -> None:
    harness_stub.start_error = harness_stub.HarnessError("provider initialization failed")
    with pytest.raises(ValueError, match="HarnessError.*no approval"):
        DSHReviewer(tmp_path, "test").review({}, "")
    assert harness_stub.closed
    assert not harness_stub.workspace.exists()


def test_sdk_error_is_controlled_and_cleans_up(tmp_path: Path, harness_stub) -> None:
    harness_stub.run_error = harness_stub.HarnessError("runtime protocol failed")
    with pytest.raises(ValueError, match="HarnessError.*no approval"):
        DSHReviewer(tmp_path, "test").review({}, "")
    assert harness_stub.closed
    assert not harness_stub.workspace.exists()


def test_missing_optional_sdk_explains_installation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "deepseek_harness", None)
    with pytest.raises(ValueError, match="uv sync --extra dsh"):
        DSHReviewer(tmp_path, "test").review({}, "")


def test_reviewer_requires_private_dsh_home(tmp_path: Path) -> None:
    shared = tmp_path / "shared-review-home"
    shared.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="mode 0700"):
        DSHReviewer(shared, "model")

    private = tmp_path / "private-review-home"
    private.mkdir(mode=0o700)
    alias = tmp_path / "alias-review-home"
    alias.symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        DSHReviewer(alias, "model")

    assert DSHReviewer(private, "model").home == private


@pytest.mark.parametrize("model,provider", [("", "provider"), ("model", " ")])
def test_review_requires_explicit_model_and_provider(tmp_path: Path, model, provider) -> None:
    with pytest.raises(ValueError, match="nonempty"):
        DSHReviewer(tmp_path, model, provider)


def test_installed_sdk_accepts_adapter_configuration_without_starting_runtime(
    tmp_path: Path,
) -> None:
    sdk = pytest.importorskip("deepseek_harness")
    # Construction is lazy in the official SDK. This verifies the installed
    # constructor contract without entering the context or invoking a model.
    harness = sdk.DeepSeekHarness(
        dsh_home=str(tmp_path / "home"),
        cwd=str(tmp_path),
        profile="sdk-minimal",
        model="test-model",
        provider="deepseek-official",
        request_timeout_seconds=120,
    )
    assert harness.config.profile == "sdk-minimal"
    assert harness.config.request_timeout_seconds == 120
    assert harness.config.dsh_home == str(tmp_path / "home")
    harness.close()


def test_installed_sdk_starts_read_only_review_without_shell(tmp_path: Path) -> None:
    if os.environ.get("BACKBONE_REQUIRE_DSH_MCP") != "1":
        pytest.skip("set BACKBONE_REQUIRE_DSH_MCP=1 for the installed SDK startup checks")
    from deepseek_harness import DeepSeekHarness

    executable = Path(sys.executable).with_name("dsh")
    assert executable.is_file(), "the installed SDK must provide the dsh executable"
    patch = tmp_path / "review.patch.yml"
    patch.write_text(json.dumps(_review_patch(tmp_path)), encoding="utf-8")
    home = tmp_path / "review-home"
    effective = subprocess.run(
        [str(executable), "--profile", "sdk-minimal", "--patch", str(patch), "--dump-config"],
        env={**os.environ, "DSH_HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows = {section.splitlines()[0]: section for section in effective.split("- id: ")[1:]}
    for tool in ("persistent-bash", "persistent-pwsh"):
        assert "disabled: true" in rows[tool]
    assert "mode: read-only" in rows["sandbox-policy"]

    harness = DeepSeekHarness(
        dsh_home=str(home),
        cwd=str(tmp_path),
        profile="sdk-minimal",
        patches=(str(patch),),
        provider="deepseek-official",
        model="placeholder",
        initialize_timeout_seconds=30,
    )
    try:
        harness.start()
    finally:
        harness.close()


def test_member_runner_mounts_scoped_mcp_and_closes_sdk(tmp_path: Path, monkeypatch) -> None:
    repo = initialized_repo(tmp_path)
    workspace = tmp_path / "coding-worktree"
    workspace.mkdir()
    home = tmp_path / "dsh-home"
    observed = SimpleNamespace(options=None, patch=None, prompt=None, session_id=None, closed=False)

    class Harness:
        def __init__(self, **options):
            observed.options = options
            observed.patch = json.loads(Path(options["patches"][0]).read_text())
            observed.patch_path = Path(options["patches"][0])
            observed.patch_mode = observed.patch_path.stat().st_mode & 0o777

        def run(self, prompt, *, session_id):
            observed.prompt = prompt
            observed.session_id = session_id
            return SimpleNamespace(
                session_id=session_id,
                finish_reason="completed",
                final_response="Work recorded through member tools",
            )

        def close(self):
            observed.closed = True

    module = ModuleType("deepseek_harness")
    module.DeepSeekHarness = Harness
    monkeypatch.setitem(sys.modules, "deepseek_harness", module)
    runner = DSHMemberRunner(repo, workspace, home, "alice", "chosen-model")
    session_id = f"{runner.session_prefix}session-member-001"
    result = runner.run("Read my Backbone task", session_id=session_id)

    assert result["member"] == "alice"
    assert result["final_response"] == "Work recorded through member tools"
    assert result["elapsed_ms"] >= 0
    assert observed.closed
    assert observed.prompt == "Read my Backbone task"
    assert observed.session_id == session_id
    assert observed.options["cwd"] == str(workspace)
    assert observed.options["dsh_home"] == str(home)
    assert observed.options["profile"] == "sdk-minimal"
    assert "never edit .backbone directly" in observed.options["env"]["DSH_SYSTEM_PROMPT"]
    assert observed.patch[0] == {
        "id": "sandbox-policy",
        "config": {"mode": "workspace-write", "workspaceRoot": str(workspace)},
    }
    mcp = observed.patch[1]["insert"][0]
    assert mcp["name"] == "@deepseek-ai/dsh-mcp-client"
    assert mcp["config"]["serverName"] == "backbone"
    assert mcp["config"]["args"] == [
        "-m",
        "backbone_conductor",
        "--repo",
        str(repo),
        "mcp",
        "--member",
        "alice",
    ]
    assert mcp["config"]["env"]["PYTHONPATH"].endswith("/src")
    assert mcp["config"]["failOnStartupError"] is True
    assert observed.patch_mode == 0o600
    assert not observed.patch_path.exists()
    assert not subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_member_runner_rejects_shared_workspace_and_home(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with pytest.raises(ValueError, match="separate"):
        DSHMemberRunner(repo, repo, tmp_path / "home", "alice", "model")
    with pytest.raises(ValueError, match="outside"):
        DSHMemberRunner(repo, workspace, repo / "dsh-home", "alice", "model")
    with pytest.raises(ValueError, match="control characters"):
        DSHMemberRunner(repo, workspace, tmp_path / "home", "alice\nadmin", "model")
    runner = DSHMemberRunner(repo, workspace, tmp_path / "home", "alice", "model")
    with pytest.raises(ValueError, match="different repository or member"):
        runner.run("Read task", session_id="session-from-another-member")


def test_member_runner_requires_private_dsh_home(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    shared = tmp_path / "shared-home"
    shared.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="mode 0700"):
        DSHMemberRunner(repo, workspace, shared, "alice", "model")

    private = tmp_path / "private-home"
    private.mkdir(mode=0o700)
    alias = tmp_path / "alias-home"
    alias.symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        DSHMemberRunner(repo, workspace, alias, "alice", "model")

    runner = DSHMemberRunner(repo, workspace, private, "alice", "model")
    assert runner.home == private
    assert runner.home.stat().st_mode & 0o777 == 0o700


def test_member_runner_preflight_rejects_admin_tool_scope(tmp_path: Path, monkeypatch) -> None:
    repo = initialized_repo(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    runner = DSHMemberRunner(repo, workspace, tmp_path / "home", "alice", "model")
    patch = runner.member_patch()
    patch[1]["insert"][0]["config"]["args"] = patch[1]["insert"][0]["config"]["args"][:-2]
    monkeypatch.setattr(runner, "member_patch", lambda: patch)
    with pytest.raises(ValueError, match="preflight failed"):
        runner._preflight_mcp()


def test_member_runner_closes_sdk_on_failed_turn(tmp_path: Path, monkeypatch) -> None:
    repo = initialized_repo(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    closed = []

    class Harness:
        def __init__(self, **_options):
            pass

        def run(self, _prompt, *, session_id):
            raise TimeoutError("provider deadline")

        def close(self):
            closed.append(True)

    module = ModuleType("deepseek_harness")
    module.DeepSeekHarness = Harness
    monkeypatch.setitem(sys.modules, "deepseek_harness", module)
    runner = DSHMemberRunner(repo, workspace, tmp_path / "home", "alice", "model")
    with pytest.raises(ValueError, match="TimeoutError"):
        runner.run("Read my task")
    assert closed == [True]


def test_remote_member_runner_requires_private_token_and_https(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "dsh-home"
    token_file = tmp_path / "member.token"
    token_file.write_text("a" * 48 + "\n")
    token_file.chmod(0o600)
    runner = DSHRemoteMemberRunner(
        workspace, home, "alice", "model", "https://coordinator.example/mcp", token_file
    )
    config = runner.member_patch()[1]["insert"][0]["config"]
    assert config["transport"] == "streamable-http"
    assert config["url"] == "https://coordinator.example/mcp"
    assert config["headers"] == {"Authorization": "Bearer " + "a" * 48}
    assert runner.session_prefix.startswith("backbone-")
    assert home.stat().st_mode & 0o777 == 0o700
    with pytest.raises(ValueError, match="different repository or member"):
        runner.run("Read task", session_id="foreign-session")
    with pytest.raises(ValueError, match="HTTPS"):
        DSHRemoteMemberRunner(
            workspace, home, "alice", "model", "http://coordinator.example/mcp", token_file
        )
    with pytest.raises(ValueError, match="absolute /mcp"):
        DSHRemoteMemberRunner(
            workspace, home, "alice", "model", "https://coordinator.example/other", token_file
        )
    token_file.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        runner.member_patch()
    token_file.chmod(0o600)
    workspace_token = workspace / "member.token"
    workspace_token.write_text("a" * 48)
    workspace_token.chmod(0o600)
    with pytest.raises(ValueError, match="outside"):
        DSHRemoteMemberRunner(
            workspace,
            home,
            "alice",
            "model",
            "https://coordinator.example/mcp",
            workspace_token,
        )
    home.chmod(0o755)
    with pytest.raises(ValueError, match="0700"):
        DSHRemoteMemberRunner(
            workspace, home, "alice", "model", "https://coordinator.example/mcp", token_file
        )


def test_remote_member_runner_does_not_require_local_coordinator(
    tmp_path: Path, monkeypatch
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token_file = tmp_path / "member.token"
    token_file.write_text("a" * 48)
    token_file.chmod(0o600)
    observed = SimpleNamespace(patch=None, patch_mode=None, closed=False)

    class Harness:
        def __init__(self, **options):
            path = Path(options["patches"][0])
            observed.patch = json.loads(path.read_text())
            observed.patch_mode = path.stat().st_mode & 0o777

        def run(self, _prompt, *, session_id):
            return SimpleNamespace(
                session_id=session_id, finish_reason="completed", final_response="Ready"
            )

        def close(self):
            observed.closed = True

    module = ModuleType("deepseek_harness")
    module.DeepSeekHarness = Harness
    monkeypatch.setitem(sys.modules, "deepseek_harness", module)
    runner = DSHRemoteMemberRunner(
        workspace,
        tmp_path / "dsh-home",
        "alice",
        "model",
        "https://coordinator.example/mcp",
        token_file,
    )
    monkeypatch.setattr(
        runner, "_preflight_mcp", lambda patch: observed.__dict__.update(preflight=patch)
    )
    assert runner.run("Read my task")["final_response"] == "Ready"
    assert observed.closed
    assert observed.patch_mode == 0o600
    assert observed.patch[1]["insert"][0]["config"]["transport"] == "streamable-http"
    assert observed.preflight == observed.patch


def test_installed_sdk_starts_member_mcp_without_model_call(tmp_path: Path) -> None:
    if os.environ.get("BACKBONE_REQUIRE_DSH_MCP") != "1":
        pytest.skip("set BACKBONE_REQUIRE_DSH_MCP=1 for the installed SDK startup check")
    from deepseek_harness import DeepSeekHarness

    repo = initialized_repo(tmp_path)
    workspace = tmp_path / "coding-worktree"
    workspace.mkdir()
    runner = DSHMemberRunner(repo, workspace, tmp_path / "dsh-home", "alice", "placeholder")
    patch = tmp_path / "backbone.patch.yml"
    patch.write_text(json.dumps(runner.member_patch()))
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
        assert any("ListToolsRequest" in line for line in harness.client._stderr_lines)
    finally:
        harness.close()


def test_installed_sdk_member_tools_and_in_process_history_with_local_mock_provider(
    tmp_path: Path, monkeypatch
) -> None:
    if os.environ.get("BACKBONE_REQUIRE_DSH_MCP") != "1":
        pytest.skip("set BACKBONE_REQUIRE_DSH_MCP=1 for the installed SDK mock-provider check")
    pytest.importorskip("deepseek_harness")

    requests: list[dict] = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != "/v1/chat/completions":
                self.send_error(404)
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            request_number = len(requests)
            chunks = [
                {
                    "id": f"mock-{request_number}",
                    "object": "chat.completion.chunk",
                    "created": 0,
                    "model": "mock-model",
                    "choices": [
                        {"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}
                    ],
                },
            ]
            if request_number == 1:
                delta = {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "mock-call-1",
                            "type": "function",
                            "function": {
                                "name": "mcp__backbone__get_my_task",
                                "arguments": "{}",
                            },
                        }
                    ]
                }
                finish_reason = "tool_calls"
            else:
                delta = {"content": f"Mock turn {request_number - 1}"}
                finish_reason = "stop"
            chunks.extend(
                [
                    {
                        "id": f"mock-{request_number}",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "mock-model",
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    },
                    {
                        "id": f"mock-{request_number}",
                        "object": "chat.completion.chunk",
                        "created": 0,
                        "model": "mock-model",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                    },
                ]
            )
            payload = (
                "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *_args: object) -> None:
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    except OSError:
        if os.environ.get("BACKBONE_REQUIRE_LIVE_HTTP") == "1":
            raise
        pytest.skip("This sandbox does not permit loopback listening sockets")
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv("DEEPSEEK_BASE_URL", f"http://127.0.0.1:{server.server_port}/v1")
        monkeypatch.setenv("DEEPSEEK_API_KEY", "local-mock-key")
        repo = initialized_repo(tmp_path)
        workspace = tmp_path / "coding-worktree"
        workspace.mkdir()
        home = tmp_path / "dsh-home"
        runner = DSHMemberRunner(repo, workspace, home, "alice", "mock-model")
        session_id = f"{runner.session_prefix}durable-test"
        emitted: list[dict] = []

        def prompts():
            yield "Remember marker alpha"
            assert [turn["final_response"] for turn in emitted] == ["Mock turn 1"]
            yield "Recall marker beta"

        turns = runner.run_turns(prompts(), session_id=session_id, on_turn=emitted.append)
        assert [turn["final_response"] for turn in turns["turns"]] == [
            "Mock turn 1",
            "Mock turn 2",
        ]
        assert emitted == turns["turns"]
        assert len(requests) == 3
        assert "get_my_task" in json.dumps(requests[0].get("tools", []))
        tool_messages = [
            message for message in requests[1]["messages"] if message.get("role") == "tool"
        ]
        assert "member_id" in json.dumps(tool_messages)
        assert "alice" in json.dumps(tool_messages)
        history = json.dumps(requests[2].get("messages"), ensure_ascii=False)
        assert "Remember marker alpha" in history
        assert "Mock turn 1" in history
        assert "Recall marker beta" in history

        resumed = DSHMemberRunner(repo, workspace, home, "alice", "mock-model")
        with pytest.raises(ValueError, match="cannot resume a persisted session"):
            resumed.run("Third turn after runtime restart", session_id=session_id)
        assert len(requests) == 3
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_coordinator_runner_preflight_and_limited_patch(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    runner = DSHCoordinatorRunner(repo, tmp_path / "private-dsh-home", "placeholder")
    workspace = tmp_path / "ephemeral-workspace"
    workspace.mkdir()
    patch = runner.coordinator_patch(workspace)
    assert patch[0]["config"] == {"mode": "read-only", "workspaceRoot": str(workspace)}
    assert patch[1:3] == [
        {"id": "persistent-bash", "disabled": True},
        {"id": "persistent-pwsh", "disabled": True},
    ]
    assert patch[3]["insert"][0]["config"]["args"][-2:] == ["mcp", "--coordinator"]
    runner._preflight_mcp(patch)
    with pytest.raises(ValueError, match="different coordinator repository"):
        runner.run("Read state", session_id="foreign-session")


def test_coordinator_runner_rejects_elevated_scope_and_public_home(tmp_path: Path) -> None:
    repo = initialized_repo(tmp_path)
    public_home = tmp_path / "public-home"
    public_home.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="0700"):
        DSHCoordinatorRunner(repo, public_home, "placeholder")
    with pytest.raises(ValueError, match="outside the repository"):
        DSHCoordinatorRunner(repo, repo / "home", "placeholder")
    runner = DSHCoordinatorRunner(repo, tmp_path / "private-home", "placeholder")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    patch = runner.coordinator_patch(workspace)
    patch[3]["insert"][0]["config"]["args"] = patch[3]["insert"][0]["config"]["args"][:-1]
    with pytest.raises(ValueError, match="preflight failed"):
        runner._preflight_mcp(patch)


def test_coordinator_runner_closes_sdk_and_removes_patch(tmp_path: Path, monkeypatch) -> None:
    repo = initialized_repo(tmp_path)
    runner = DSHCoordinatorRunner(repo, tmp_path / "private-dsh-home", "chosen-model")
    observed = SimpleNamespace(closed=False, prompts=[], constructed=0)

    class Harness:
        def __init__(self, **options):
            observed.constructed += 1
            observed.options = options
            observed.patch_path = Path(options["patches"][0])
            observed.patch = json.loads(observed.patch_path.read_text())
            observed.patch_mode = observed.patch_path.stat().st_mode & 0o777

        def run(self, prompt, *, session_id):
            observed.prompts.append(prompt)
            return SimpleNamespace(
                session_id=session_id,
                finish_reason="completed",
                final_response="Draft intent proposed",
            )

        def close(self):
            observed.closed = True

    module = ModuleType("deepseek_harness")
    module.DeepSeekHarness = Harness
    monkeypatch.setitem(sys.modules, "deepseek_harness", module)
    emitted: list[dict] = []

    def prompts():
        yield "Check current coordination state"
        assert len(emitted) == 1
        yield "Propose a draft"

    result = runner.run_turns(prompts(), on_turn=emitted.append)
    assert result["session_id"].startswith(runner.session_prefix)
    assert len(result["turns"]) == 2
    assert emitted == result["turns"]
    assert {turn["session_id"] for turn in result["turns"]} == {result["session_id"]}
    assert all(turn["final_response"] == "Draft intent proposed" for turn in result["turns"])
    assert all(turn["elapsed_ms"] >= 0 for turn in result["turns"])
    assert observed.prompts == ["Check current coordination state", "Propose a draft"]
    assert observed.constructed == 1
    assert observed.closed
    assert observed.patch_mode == 0o600
    assert not observed.patch_path.exists()
    assert observed.options["cwd"] != str(repo)
    assert observed.options["env"]["DSH_SYSTEM_PROMPT"].startswith(
        "You are a limited Backbone coordination agent"
    )


def test_installed_sdk_starts_coordinator_mcp_without_model_call(tmp_path: Path) -> None:
    if os.environ.get("BACKBONE_REQUIRE_DSH_MCP") != "1":
        pytest.skip("set BACKBONE_REQUIRE_DSH_MCP=1 for the installed SDK startup check")
    from deepseek_harness import DeepSeekHarness

    repo = initialized_repo(tmp_path)
    runner = DSHCoordinatorRunner(repo, tmp_path / "private-dsh-home", "placeholder")
    workspace = tmp_path / "ephemeral-workspace"
    workspace.mkdir()
    patch = tmp_path / "coordinator.patch.yml"
    patch.write_text(json.dumps(runner.coordinator_patch(workspace)))
    executable = Path(sys.executable).with_name("dsh")
    effective = subprocess.run(
        [str(executable), "--profile", "sdk-minimal", "--patch", str(patch), "--dump-config"],
        env={**os.environ, "DSH_HOME": str(runner.home)},
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    rows = {section.splitlines()[0]: section for section in effective.split("- id: ")[1:]}
    assert "mode: read-only" in rows["sandbox-policy"]
    for tool in ("persistent-bash", "persistent-pwsh"):
        assert "disabled: true" in rows[tool]
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
        assert any("ListToolsRequest" in line for line in harness.client._stderr_lines)
    finally:
        harness.close()
