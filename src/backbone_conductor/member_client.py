"""Authenticated HTTP member workflow without a model or coordinator checkout."""

from __future__ import annotations

import re
import ssl
from pathlib import Path

import httpx

from .private_token import read_private_token
from .remote_http import identifier, request_json, server_url

_SHA = re.compile(r"[0-9a-f]{40}\Z")


def run_member_command(args, payload: dict | None = None) -> dict:
    """Execute one member-scoped request, deriving identity from the bearer token."""
    if args.action in {"create-intent", "propose-decision", "submit"} and payload is None:
        raise ValueError("Member command requires a JSON object payload")
    origin = server_url(args.url, "Member")
    token = read_private_token(args.token_file, "Member")
    verify: ssl.SSLContext | bool = True
    if args.ca_file:
        certificate = Path(args.ca_file).expanduser().resolve(strict=True)
        if not certificate.is_file():
            raise ValueError("Member CA file must be a regular file")
        verify = ssl.create_default_context(cafile=str(certificate))

    with httpx.Client(
        base_url=origin,
        headers={"Authorization": f"Bearer {token}"},
        verify=verify,
        trust_env=False,
        follow_redirects=False,
        timeout=20,
    ) as client:
        identity = request_json(client, "GET", "/whoami", "Member")
        if (
            not isinstance(identity, dict)
            or identity.get("role") != "member"
            or not isinstance(identity.get("name"), str)
        ):
            raise ValueError("Member token must identify a member role")
        member = identifier(identity["name"], "Member")
        if args.action == "whoami":
            return identity
        if args.action == "tasks":
            tasks = request_json(client, "GET", "/tasks", "Member")
            if (
                not isinstance(tasks, dict)
                or tasks.get("member_id") != member
                or not isinstance(tasks.get("version"), str)
                or not isinstance(tasks.get("tasks"), list)
            ):
                raise ValueError("Member server returned invalid task context")
            return tasks
        if args.action == "updates":
            return request_json(
                client,
                "GET",
                "/sync",
                "Member",
                params={"since_version": args.since_version} if args.since_version else None,
            )
        if args.action == "create-intent":
            if payload.get("author", member) != member:
                raise ValueError("Intent author must match the authenticated member")
            return request_json(client, "POST", "/intents", "Member", {**payload, "author": member})
        if args.action == "propose-decision":
            if payload.get("author", member) != member:
                raise ValueError("Decision author must match the authenticated member")
            return request_json(
                client, "POST", "/decisions", "Member", {**payload, "author": member}
            )
        task_id = identifier(args.task_id, "Member")
        path = f"/tasks/{task_id}"
        if args.action == "start":
            return request_json(client, "POST", f"{path}/start", "Member", {"member_id": member})
        if args.action == "rebase":
            return request_json(
                client,
                "POST",
                f"{path}/rebase",
                "Member",
                {"member_id": member, "expected_version": args.version},
            )
        if args.action == "fetch":
            if not _SHA.fullmatch(args.sha):
                raise ValueError("Member fetch needs a full lowercase Git commit SHA")
            return request_json(
                client,
                "POST",
                f"{path}/fetch",
                "Member",
                {
                    "member_id": member,
                    "branch": args.branch,
                    "expected_sha": args.sha,
                    "remote": args.remote,
                },
            )
        if args.action == "submit":
            return request_json(
                client,
                "POST",
                f"{path}/submit",
                "Member",
                {"member_id": member, "artifact": payload},
            )
        raise ValueError(f"Unknown member command: {args.action}")
