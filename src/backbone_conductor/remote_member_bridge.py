"""Expose an authenticated remote member's existing MCP scope over local stdio."""

from __future__ import annotations

import json
import ssl
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.fastmcp import FastMCP

from .dsh_agent import preflight_remote_member
from .private_token import read_private_token


def create_remote_member_bridge(
    url: str, member: str, token_file: str, ca_file: str | None = None
) -> FastMCP:
    """Pin the remote role and tool set before advertising a local member interface."""
    preflight_remote_member(url, member, token_file, ca_file)
    certificate = Path(ca_file).expanduser().resolve(strict=True) if ca_file else None
    server = FastMCP(
        "Backbone Remote Member",
        instructions=(
            "Authenticated remote member tools for one Backbone coordinator. "
            "Read the assigned task and current decisions before editing. "
            "Proposals need review; artifact checks do not approve code or merge Git branches."
        ),
    )

    async def forward(name: str, arguments: dict[str, Any]) -> dict:
        token = read_private_token(token_file, "Member MCP")
        verify: ssl.SSLContext | bool = (
            ssl.create_default_context(cafile=str(certificate)) if certificate else True
        )
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            verify=verify,
            trust_env=False,
        ) as http_client:
            async with streamable_http_client(url, http_client=http_client) as (read, write, _):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    response = await session.call_tool(name, arguments)
        if response.isError or not response.content:
            raise ValueError(f"Remote member MCP {name} failed")
        try:
            result = json.loads(response.content[0].text)
        except (AttributeError, ValueError) as exc:
            raise ValueError(f"Remote member MCP {name} returned invalid JSON") from exc
        if not isinstance(result, dict):
            raise ValueError(f"Remote member MCP {name} returned an invalid result")
        return result

    @server.tool()
    async def get_my_task() -> dict:
        """Read this authenticated member's assigned tasks and accepted context."""
        return await forward("get_my_task", {})

    @server.tool()
    async def create_intent(intent_data: dict[str, Any]) -> dict:
        """Propose a draft intention under the authenticated member's identity."""
        return await forward("create_intent", {"intent_data": intent_data})

    @server.tool()
    async def log_decision(decision_data: dict[str, Any]) -> dict:
        """Propose a decision under the authenticated member's identity."""
        return await forward("log_decision", {"decision_data": decision_data})

    @server.tool()
    async def start_task(task_id: str) -> dict:
        """Start an assigned task after reading its accepted intent and constraints."""
        return await forward("start_task", {"task_id": task_id})

    @server.tool()
    async def fetch_artifact_branch(
        task_id: str, branch: str, expected_sha: str, remote: str = "origin"
    ) -> dict:
        """Fetch one assigned member's pushed code branch by full commit SHA."""
        return await forward(
            "fetch_artifact_branch",
            {"task_id": task_id, "branch": branch, "expected_sha": expected_sha, "remote": remote},
        )

    @server.tool()
    async def rebase_task(task_id: str, expected_version: str) -> dict:
        """Refresh an assigned task against the observed ledger version."""
        return await forward(
            "rebase_task", {"task_id": task_id, "expected_version": expected_version}
        )

    @server.tool()
    async def submit_artifact(artifact: dict[str, Any]) -> dict:
        """Submit a pinned Git artifact for checks and later human review."""
        return await forward("submit_artifact", {"artifact": artifact})

    @server.tool()
    async def check_backbone_sync(since_version: str | None = None) -> dict:
        """Read newly accepted or withdrawn decisions and blocking conflicts."""
        return await forward("check_backbone_sync", {"since_version": since_version})

    return server
