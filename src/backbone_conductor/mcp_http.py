"""Bearer-authenticated, request-bound member MCP over Streamable HTTP."""

from __future__ import annotations

from contextvars import ContextVar
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .audit import bind_http_actor, reset_http_actor
from .auth import TokenAuth
from .mcp_server import create_server

_MEMBER: ContextVar[str | None] = ContextVar("backbone_mcp_http_member", default=None)
_LOCAL_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")


def _current_member() -> str:
    member = _MEMBER.get()
    if member is None:
        raise PermissionError("No authenticated member is bound to this MCP request")
    return member


class MemberAuthApp:
    """Authenticate every HTTP request before the MCP transport sees it."""

    def __init__(self, app: ASGIApp, auth: TokenAuth):
        self.app = app
        self.auth = auth

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            principal = self.auth.authenticate(Headers(scope=scope).get("authorization"))
        except ValueError:
            await JSONResponse(
                {"detail": "HTTP credentials are unavailable or invalid"}, status_code=503
            )(scope, receive, send)
            return
        if principal is None:
            await JSONResponse(
                {"detail": "Valid bearer token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return
        if principal.role != "member":
            await JSONResponse({"detail": "Member role required"}, status_code=403)(
                scope, receive, send
            )
            return
        member_token = _MEMBER.set(principal.name)
        actor_token = bind_http_actor(principal.name, principal.role)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_http_actor(actor_token)
            _MEMBER.reset(member_token)


def create_member_http_app(
    repo: str | Path,
    auth: TokenAuth,
    *,
    ledger_branch: str | None = None,
    allowed_hosts: tuple[str, ...] = (),
) -> tuple[FastMCP, ASGIApp]:
    """Build member-only tools; the parent ASGI app owns the MCP lifespan."""
    server = create_server(
        repo,
        ledger_branch=ledger_branch,
        member_resolver=_current_member,
        http_allowed_hosts=(*_LOCAL_HOSTS, *allowed_hosts),
    )
    return server, MemberAuthApp(server.streamable_http_app(), auth)
