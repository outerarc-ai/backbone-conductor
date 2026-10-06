"""Start a role-scoped Backbone MCP process for a local Codex plugin install."""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def command_from_environment(environ: dict[str, str]) -> list[str]:
    role = environ.get("BACKBONE_CODEX_ROLE", "member")
    if role not in {"member", "coordinator"}:
        raise ValueError("BACKBONE_CODEX_ROLE must be member or coordinator")

    executable = environ.get("BACKBONE_EXECUTABLE", "backbone")
    if not executable or shutil.which(executable) is None:
        raise ValueError("Install backbone-conductor or set BACKBONE_EXECUTABLE")

    remote_url = environ.get("BACKBONE_MCP_URL", "")
    if remote_url:
        if role != "member":
            raise ValueError("Remote Backbone MCP supports the member role only")
        if environ.get("BACKBONE_REPO") or environ.get("BACKBONE_LEDGER_BRANCH"):
            raise ValueError("Remote member MCP does not use a local repository or ledger branch")
        member = environ.get("BACKBONE_MEMBER", "").strip()
        token_file = environ.get("BACKBONE_TOKEN_FILE", "")
        if not member or not token_file:
            raise ValueError("Remote member MCP needs BACKBONE_MEMBER and BACKBONE_TOKEN_FILE")
        command = [
            executable,
            "mcp-remote-member",
            "--url",
            remote_url,
            "--member",
            member,
            "--token-file",
            token_file,
        ]
        ca_file = environ.get("BACKBONE_MCP_CA_FILE")
        if ca_file:
            command.extend(["--ca-file", ca_file])
        return command

    repo_text = environ.get("BACKBONE_REPO", "")
    if not repo_text or not Path(repo_text).is_absolute() or not Path(repo_text).is_dir():
        raise ValueError("BACKBONE_REPO must be an absolute path to an existing repository")
    command = [executable, "--repo", str(Path(repo_text).resolve())]
    ledger_branch = environ.get("BACKBONE_LEDGER_BRANCH")
    if ledger_branch:
        command.extend(["--ledger-branch", ledger_branch])
    command.append("mcp")

    if role == "member":
        member = environ.get("BACKBONE_MEMBER", "").strip()
        if not member:
            raise ValueError("BACKBONE_MEMBER is required for the member MCP scope")
        command.extend(["--member", member])
    else:
        if environ.get("BACKBONE_MEMBER"):
            raise ValueError("Coordinator MCP does not use BACKBONE_MEMBER")
        command.append("--coordinator")
    return command


def _include_editable_checkout(executable: str) -> None:
    """Keep a checkout's src layout importable when a sandbox hides editable .pth files."""
    executable_path = shutil.which(executable)
    if executable_path is None:
        return
    executable_path = Path(executable_path).absolute()
    if executable_path.parent.name != "bin" or executable_path.parent.parent.name != ".venv":
        return
    source_root = executable_path.parent.parent.parent / "src"
    if not (source_root / "backbone_conductor" / "__init__.py").is_file():
        return
    current = os.environ.get("PYTHONPATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    if str(source_root) not in parts:
        os.environ["PYTHONPATH"] = os.pathsep.join([str(source_root), *parts])


def main() -> int:
    try:
        command = command_from_environment(dict(os.environ))
    except ValueError as exc:
        print(f"Backbone Codex MCP setup: {exc}", file=sys.stderr)
        return 2
    _include_editable_checkout(command[0])
    os.execvp(command[0], command)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
