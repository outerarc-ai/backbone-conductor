"""HTTP API with optional bearer authentication and member authorization."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .audit import bind_http_actor, reset_http_actor
from .auth import Principal, TokenAuth
from .service import Conductor

_DECISION_MAP_FILES = {
    "/decision-map": "decision_map.html",
    "/decision-map.css": "decision_map.css",
    "/decision-map.js": "decision_map.js",
    "/lifecycle-map": "lifecycle_map.html",
    "/lifecycle-map.js": "lifecycle_map.js",
}
_DECISION_MAP_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'"
)


def _member_route(method: str, path: str) -> bool:
    if (method, path) in {
        ("GET", "/schema"),
        ("GET", "/whoami"),
        ("GET", "/decision-map/data"),
        ("GET", "/lifecycle-map/data"),
        ("GET", "/tasks"),
        ("GET", "/sync"),
        ("POST", "/intents"),
        ("POST", "/decisions"),
        ("POST", "/artifacts"),
    }:
        return True
    if method == "GET":
        return re.fullmatch(r"/tasks/[^/]+", path) is not None
    if method == "POST":
        return re.fullmatch(r"/tasks/[^/]+/(start|rebase|submit|fetch)", path) is not None
    return False


def _reviewer_route(method: str, path: str) -> bool:
    if method == "GET":
        if path in {
            "/state",
            "/schema",
            "/whoami",
            "/intents",
            "/decisions",
            "/decision-map/data",
            "/lifecycle-map/data",
            "/tasks",
            "/conflicts",
            "/timeline",
            "/audit/verify",
            "/audit/snapshot",
            "/audit/history",
            "/sync",
            "/docs",
            "/redoc",
            "/openapi.json",
        }:
            return True
        return (
            re.fullmatch(r"/(intents|decisions|tasks)/[^/]+", path) is not None
            or re.fullmatch(r"/tasks/[^/]+/inspection", path) is not None
        )
    if method == "POST":
        return (
            re.fullmatch(r"/intents/[^/]+/review", path) is not None
            or re.fullmatch(r"/decisions/[^/]+/revert", path) is not None
            or re.fullmatch(r"/tasks/[^/]+/merge", path) is not None
            or re.fullmatch(r"/conflicts/[^/]+/resolve", path) is not None
        )
    return False


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Transition(Action):
    status: str = Field(min_length=1)


class Dispatch(Action):
    intent_id: str = Field(min_length=1)
    member_id: str = Field(min_length=1)
    spec: str = ""
    forbidden_paths: list[str] = Field(default_factory=list)


class Member(Action):
    member_id: str = Field(min_length=1)


class Submission(Member):
    artifact: dict[str, Any]


class BranchFetch(Member):
    branch: str = Field(min_length=1)
    expected_sha: str = Field(min_length=1)
    remote: str = "origin"


class Approval(Action):
    author: str = Field(min_length=1)
    rationale: str | None = None


class MergeApproval(Approval):
    expected_version: str = Field(min_length=1)
    expected_target_sha: str = Field(min_length=1)


class IntentRevision(Approval):
    patch: dict[str, Any]
    expected_version: str = Field(min_length=1)


class IntentReplacement(IntentRevision):
    reason: str = Field(min_length=1)


class IntentReviewAction(Approval):
    outcome: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    expected_version: str = Field(min_length=1)


class DecisionRevertAction(Approval):
    rationale: str = Field(min_length=1)
    expected_version: str = Field(min_length=1)


class Cancellation(Approval):
    reason: str = Field(min_length=1)


class TaskRebase(Member):
    expected_version: str = Field(min_length=1)


class Resolution(Approval):
    action: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    expected_version: str = Field(min_length=1)


class Sync(Action):
    remote: str = "origin"
    branch: str | None = None


class Reconciliation(Sync):
    local_head: str = Field(min_length=1)
    remote_head: str = Field(min_length=1)
    author: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    resolutions: dict[str, dict[str, dict[str, Any]]] | None = None


def create_app(
    repo: str | Path,
    *,
    auth_file: str | Path | None = None,
    ledger_branch: str | None = None,
    mcp_http: bool = False,
    mcp_allowed_hosts: tuple[str, ...] = (),
) -> FastAPI:
    """Create a local admin API or an authenticated admin/member/reviewer API."""
    if mcp_http and auth_file is None:
        raise ValueError("Streamable HTTP MCP requires --auth-file")
    if mcp_allowed_hosts and not mcp_http:
        raise ValueError("MCP allowed hosts require Streamable HTTP MCP")
    conductor = Conductor(repo, ledger_branch=ledger_branch)
    auth = TokenAuth(auth_file, conductor.code_store.root) if auth_file is not None else None
    member_mcp = None
    member_mcp_app = None
    if mcp_http:
        from .mcp_http import create_member_http_app

        assert auth is not None
        member_mcp, member_mcp_app = create_member_http_app(
            repo, auth, ledger_branch=ledger_branch, allowed_hosts=mcp_allowed_hosts
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if member_mcp is None:
            yield
        else:
            async with member_mcp.session_manager.run():
                yield

    app = FastAPI(
        title="Backbone Conductor",
        version=__version__,
        description=(
            "Without --auth-file, bind to loopback for trusted local administrators. "
            "With --auth-file, bearer tokens authorize admin, member and reviewer operations. "
            "Use TLS at a trusted reverse proxy or configure direct HTTPS for remote access. "
            "Merge approval records require an actual Git merge and human semantic review."
        ),
        lifespan=lifespan,
    )
    app.state.conductor = conductor
    app.state.member_mcp = member_mcp

    @app.middleware("http")
    async def authenticate(request: Request, call_next):
        if mcp_http and request.url.path == "/mcp":
            # The mounted MCP app authenticates every request, including initialize.
            return await call_next(request)
        if request.method == "GET" and request.url.path in _DECISION_MAP_FILES:
            # The shell contains no ledger data. Browser clients provide a bearer
            # token in memory when fetching the role-protected projection below.
            return await call_next(request)
        if auth is None:
            request.state.principal = Principal("local", "admin")
        elif request.url.path == "/health":
            request.state.principal = None
        else:
            try:
                principal = auth.authenticate(request.headers.get("authorization"))
            except ValueError:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "HTTP credentials are unavailable or invalid"},
                )
            if principal is None:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Valid bearer token required"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            request.state.principal = principal
            if principal.role == "member" and not _member_route(request.method, request.url.path):
                return JSONResponse(status_code=403, content={"detail": "Admin role required"})
            if principal.role == "reviewer" and not _reviewer_route(
                request.method, request.url.path
            ):
                return JSONResponse(
                    status_code=403, content={"detail": "Reviewer role cannot access this endpoint"}
                )
        if auth is None or request.state.principal is None:
            return await call_next(request)
        token = bind_http_actor(request.state.principal.name, request.state.principal.role)
        try:
            return await call_next(request)
        finally:
            reset_http_actor(token)

    def bind_member(request: Request, member_id: str | None) -> str | None:
        principal = request.state.principal
        if principal.role == "member":
            if member_id is not None and member_id != principal.name:
                raise PermissionError("Member identity is bound to the bearer token")
            return principal.name
        return member_id

    def bind_author(request: Request, data: dict[str, Any], required_status: str) -> dict:
        principal = request.state.principal
        if auth is None:
            return data
        if data.get("author", principal.name) != principal.name:
            raise PermissionError("Author identity is bound to the bearer token")
        if principal.role == "member" and data.get("status", required_status) != required_status:
            raise PermissionError(f"Members may create {required_status} records only")
        return {**data, "author": principal.name}

    def actor(request: Request, claimed: str) -> str:
        if auth is not None and claimed != request.state.principal.name:
            raise PermissionError("Author identity is bound to the bearer token")
        return claimed

    def visible_task(request: Request, task_id: str) -> dict:
        result = conductor.state()["tasks"][task_id]
        bind_member(request, result["member_id"])
        return result

    def member_projection(snapshot: dict, name: str) -> tuple[list[dict], list[dict], list[dict]]:
        tasks = [task for task in snapshot["tasks"].values() if task["member_id"] == name]
        visible_intents = {task["intent_id"] for task in tasks}
        visible_intents.update(
            intent["id"] for intent in snapshot["intents"].values() if intent["author"] == name
        )
        intents = [
            intent for intent in snapshot["intents"].values() if intent["id"] in visible_intents
        ]
        visible_decisions = {
            decision_id for task in tasks for decision_id in task["decisions_at_fork"]
        }
        visible_decisions.update(
            task["approval"]["decision_id"] for task in tasks if task["approval"] is not None
        )
        decisions = [
            decision
            for decision in snapshot["decisions"].values()
            if decision["id"] in visible_decisions
            or visible_intents.intersection(decision["related_intents"])
        ]
        return intents, tasks, decisions

    async def domain_error(_request: Request, exc: Exception) -> JSONResponse:
        if isinstance(exc, PermissionError):
            status = 403
        elif isinstance(exc, (KeyError, FileNotFoundError)):
            status = 404
        elif isinstance(exc, ValueError):
            status = 422
        else:
            status = 409
        message = str(exc.args[0]) if isinstance(exc, KeyError) else str(exc)
        return JSONResponse(status_code=status, content={"detail": message})

    for error in (ValueError, KeyError, PermissionError, FileNotFoundError, RuntimeError):
        app.add_exception_handler(error, domain_error)

    @app.get("/health")
    def health() -> Any:
        if auth is not None:
            try:
                auth.check_available()
            except ValueError:
                return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ok"}

    def map_file(path: str) -> FileResponse:
        return FileResponse(
            Path(__file__).with_name("web") / _DECISION_MAP_FILES[path],
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": _DECISION_MAP_CSP,
                "X-Content-Type-Options": "nosniff",
            },
        )

    @app.get("/decision-map", include_in_schema=False)
    def decision_map() -> FileResponse:
        return map_file("/decision-map")

    @app.get("/decision-map.css", include_in_schema=False)
    def decision_map_css() -> FileResponse:
        return map_file("/decision-map.css")

    @app.get("/decision-map.js", include_in_schema=False)
    def decision_map_js() -> FileResponse:
        return map_file("/decision-map.js")

    @app.get("/decision-map/data")
    def decision_map_data(request: Request) -> JSONResponse:
        snapshot = conductor.state()
        decisions = list(snapshot["decisions"].values())
        principal = request.state.principal
        if principal.role == "member":
            _, _, decisions = member_projection(snapshot, principal.name)
        return JSONResponse(
            content={
                "version": snapshot["version"],
                "decisions": decisions,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/lifecycle-map", include_in_schema=False)
    def lifecycle_map() -> FileResponse:
        return map_file("/lifecycle-map")

    @app.get("/lifecycle-map.js", include_in_schema=False)
    def lifecycle_map_js() -> FileResponse:
        return map_file("/lifecycle-map.js")

    @app.get("/lifecycle-map/data")
    def lifecycle_map_data(request: Request) -> JSONResponse:
        snapshot = conductor.state()
        intents = list(snapshot["intents"].values())
        tasks = list(snapshot["tasks"].values())
        decisions = list(snapshot["decisions"].values())
        principal = request.state.principal
        if principal.role == "member":
            intents, tasks, decisions = member_projection(snapshot, principal.name)
        return JSONResponse(
            content={
                "version": snapshot["version"],
                "intents": intents,
                "tasks": tasks,
                "decisions": decisions,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/initialize")
    def initialize() -> dict:
        return conductor.initialize()

    @app.get("/state")
    def state() -> dict:
        return conductor.state()

    @app.get("/whoami")
    def whoami(request: Request) -> dict[str, str]:
        principal = request.state.principal
        return {"name": principal.name, "role": principal.role}

    @app.get("/schema")
    def schema() -> dict:
        from .models import BackboneState

        return BackboneState.model_json_schema()

    @app.get("/intents")
    def intents() -> list[dict]:
        return list(conductor.state()["intents"].values())

    @app.post("/intents", status_code=201)
    def create_intent(data: dict[str, Any], request: Request) -> dict:
        return conductor.create_intent(bind_author(request, data, "draft"))

    @app.get("/intents/{intent_id}")
    def intent(intent_id: str) -> dict:
        return conductor.state()["intents"][intent_id]

    @app.post("/intents/{intent_id}/transition")
    def transition_intent(intent_id: str, data: Transition) -> dict:
        return conductor.transition_intent(intent_id, data.status)

    @app.post("/intents/{intent_id}/review")
    def review_intent(intent_id: str, data: IntentReviewAction, request: Request) -> dict:
        return conductor.review_intent(
            intent_id,
            data.outcome,
            actor(request, data.author),
            data.rationale,
            data.expected_version,
        )

    @app.post("/intents/{intent_id}/revise")
    def revise_intent(intent_id: str, data: IntentRevision, request: Request) -> dict:
        return conductor.revise_intent(
            intent_id, data.patch, actor(request, data.author), data.expected_version
        )

    @app.post("/intents/{intent_id}/replace", status_code=201)
    def replace_intent(intent_id: str, data: IntentReplacement, request: Request) -> dict:
        return conductor.replace_intent(
            intent_id, data.patch, actor(request, data.author), data.reason, data.expected_version
        )

    @app.get("/decisions")
    def decisions() -> list[dict]:
        return list(conductor.state()["decisions"].values())

    @app.post("/decisions", status_code=201)
    def create_decision(data: dict[str, Any], request: Request) -> dict:
        return conductor.log_decision(bind_author(request, data, "proposed"))

    @app.get("/decisions/{decision_id}")
    def decision(decision_id: str) -> dict:
        return conductor.state()["decisions"][decision_id]

    @app.post("/decisions/{decision_id}/transition")
    def transition_decision(decision_id: str, data: Transition) -> dict:
        return conductor.transition_decision(decision_id, data.status)

    @app.post("/decisions/{decision_id}/revert")
    def revert_decision(decision_id: str, data: DecisionRevertAction, request: Request) -> dict:
        return conductor.revert_decision(
            decision_id, actor(request, data.author), data.rationale, data.expected_version
        )

    @app.get("/tasks")
    def tasks(request: Request, member_id: str | None = None) -> dict:
        member_id = bind_member(request, member_id)
        if member_id is not None:
            return conductor.get_my_task(member_id)
        return {"tasks": list(conductor.state()["tasks"].values())}

    @app.post("/tasks", status_code=201)
    def dispatch_task(data: Dispatch) -> dict:
        return conductor.dispatch_task(
            data.intent_id, data.member_id, data.spec, data.forbidden_paths
        )

    @app.get("/tasks/{task_id}")
    def task(task_id: str, request: Request) -> dict:
        return visible_task(request, task_id)

    @app.get("/tasks/{task_id}/inspection")
    def task_inspection(task_id: str, full_patch: bool = False) -> dict:
        return conductor.inspect_task(task_id, full_patch=full_patch)

    @app.post("/tasks/{task_id}/start")
    def start_task(task_id: str, data: Member, request: Request) -> dict:
        return conductor.start_task(task_id, bind_member(request, data.member_id))

    @app.post("/tasks/{task_id}/rebase")
    def rebase_task(task_id: str, data: TaskRebase, request: Request) -> dict:
        return conductor.rebase_task(
            task_id, bind_member(request, data.member_id), data.expected_version
        )

    @app.post("/tasks/{task_id}/fetch")
    def fetch_task_branch(task_id: str, data: BranchFetch, request: Request) -> dict:
        return conductor.fetch_artifact_branch(
            task_id,
            bind_member(request, data.member_id),
            data.branch,
            data.expected_sha,
            data.remote,
        )

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, data: Cancellation, request: Request) -> dict:
        return conductor.cancel_task(task_id, actor(request, data.author), data.reason)

    @app.post("/artifacts", status_code=201)
    def submit_artifact(data: Submission, request: Request) -> dict:
        return conductor.submit_artifact(bind_member(request, data.member_id), data.artifact)

    @app.post("/tasks/{task_id}/submit")
    def submit_task(task_id: str, data: Submission, request: Request) -> dict:
        assigned = visible_task(request, task_id)
        bind_member(request, data.member_id)
        if assigned["member_id"] != data.member_id:
            raise PermissionError("Task belongs to another member")
        artifact = dict(data.artifact)
        if artifact.get("intent_id", assigned["intent_id"]) != assigned["intent_id"]:
            raise ValueError("artifact.intent_id must match the task in the request path")
        artifact["intent_id"] = assigned["intent_id"]
        return conductor.submit_artifact(data.member_id, artifact)

    @app.post("/tasks/{task_id}/merge")
    def merge_task(task_id: str, data: MergeApproval, request: Request) -> dict:
        """Record human approval after performing the actual Git merge externally."""
        principal = request.state.principal
        if principal.role == "reviewer":
            if not data.rationale or not data.rationale.strip():
                raise ValueError("Reviewer approval requires a rationale")
            if conductor.state()["tasks"][task_id]["member_id"] == principal.name:
                raise PermissionError("Reviewers cannot approve their own assigned task")
        return conductor.merge_task(
            task_id,
            actor(request, data.author),
            data.rationale,
            expected_version=data.expected_version,
            expected_target_sha=data.expected_target_sha,
        )

    @app.get("/conflicts")
    def conflicts() -> list[dict]:
        return list(conductor.state()["conflicts"].values())

    @app.post("/conflicts/check")
    def detect_conflicts() -> dict:
        return conductor.detect_conflicts()

    @app.post("/conflicts/{conflict_id}/resolve")
    def resolve_conflict(conflict_id: str, data: Resolution, request: Request) -> dict:
        return conductor.resolve_conflict(
            conflict_id,
            actor(request, data.author),
            data.action,
            data.rationale,
            data.expected_version,
        )

    @app.get("/sync")
    def check_sync(
        request: Request, member_id: str | None = None, since_version: str | None = None
    ) -> dict:
        return conductor.check_backbone_sync(bind_member(request, member_id), since_version)

    @app.post("/sync")
    def sync(data: Sync) -> dict:
        return conductor.sync(data.remote, data.branch)

    @app.post("/refresh")
    def refresh(data: Sync) -> dict:
        return conductor.refresh(data.remote, data.branch)

    @app.post("/reconcile")
    def reconcile(data: Reconciliation, request: Request) -> dict:
        return conductor.reconcile(
            data.local_head,
            data.remote_head,
            actor(request, data.author),
            data.rationale,
            data.remote,
            data.branch,
            data.resolutions,
        )

    @app.get("/timeline")
    def timeline(
        limit: int = Query(default=50, ge=1, le=1000),
        author: str | None = None,
        http_principal: str | None = None,
        event_type: str | None = Query(
            default=None,
            pattern="^(initialize|intent|decision|task|artifact|conflict|reconcile|migrate|other)$",
        ),
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict]:
        if (since is not None and since.tzinfo is None) or (
            until is not None and until.tzinfo is None
        ):
            raise ValueError("Timeline dates must include a timezone")
        if since is not None and until is not None and since > until:
            raise ValueError("since must not be after until")
        return conductor.log(
            limit,
            author=author,
            http_principal=http_principal,
            event_type=event_type,
            since=since.isoformat() if since is not None else None,
            until=until.isoformat() if until is not None else None,
        )

    @app.get("/audit/verify")
    def verify_audit(limit: int = Query(default=50, ge=1, le=1000)) -> dict:
        return conductor.verify_audit_signatures(limit)

    @app.get("/audit/snapshot")
    def verify_snapshot() -> dict:
        return conductor.verify_current_snapshot()

    @app.get("/audit/history")
    def verify_history(
        limit: int = Query(default=50, ge=1, le=1000),
        offset: int = Query(default=0, ge=0),
        expected_head: str | None = None,
    ) -> dict:
        return conductor.verify_audit_history(limit, offset, expected_head)

    if member_mcp_app is not None:
        app.mount("/", member_mcp_app)
    return app
