"""A small authenticated client for reviewers who do not hold the coordinator repo."""

from __future__ import annotations

import hashlib
import re
import ssl
from pathlib import Path

import httpx

from .audit_history import validate_history_page, verify_all_history_pages
from .private_token import read_private_token
from .remote_http import identifier, request_json, server_url

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
_SHA = re.compile(r"[0-9a-f]{40}\Z")


def _private_token(path: str) -> str:
    return read_private_token(path, "Reviewer")


def _server_url(value: str) -> str:
    return server_url(value, "Reviewer")


def _identifier(value: str) -> str:
    return identifier(value, "Reviewer")


def _validated_audit_report(report: object, limit: int) -> dict:
    """Fail closed when a remote signature report violates its response contract."""
    error = ValueError("Reviewer server returned an invalid audit report")
    if not isinstance(report, dict):
        raise error
    counts = ("checked", "limit", "total_metadata_commits", "valid", "unsigned", "invalid")
    if any(type(report.get(key)) is not int or report[key] < 0 for key in counts):
        raise error
    if (
        report["limit"] != limit
        or report["checked"] > limit
        or report["total_metadata_commits"] < report["checked"]
        or report["checked"] != report["valid"] + report["unsigned"] + report["invalid"]
        or type(report.get("truncated")) is not bool
        or report["truncated"] != (report["total_metadata_commits"] > report["checked"])
        or type(report.get("all_inspected_signed_and_valid")) is not bool
        or report["all_inspected_signed_and_valid"]
        != (report["checked"] > 0 and report["valid"] == report["checked"])
    ):
        raise error
    commits = report.get("commits")
    if not isinstance(commits, list) or len(commits) != report["checked"]:
        raise error
    statuses = {"valid": 0, "unsigned": 0, "invalid": 0}
    for item in commits:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("commit"), str)
            or not _SHA.fullmatch(item["commit"])
            or not isinstance(item.get("signature"), str)
            or item.get("signature") not in statuses
        ):
            raise error
        statuses[item["signature"]] += 1
    if any(statuses[key] != report[key] for key in statuses):
        raise error
    return report


def _validated_snapshot_report(report: object) -> dict:
    """Reject malformed snapshot reports without claiming independent verification."""
    error = ValueError("Reviewer server returned an invalid snapshot report")
    if not isinstance(report, dict):
        raise error
    required = {
        "version",
        "git_parent_count",
        "view_count",
        "missing_views",
        "extra_views",
        "changed_views",
        "parent_version",
        "expected_parent_version",
        "merged_parent_version",
        "expected_merged_parent_version",
        "parent_links_ok",
        "ok",
    }
    if not required <= report.keys():
        raise error

    def sha_or_none(value: object) -> bool:
        return value is None or (
            isinstance(value, str)
            and re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is not None
        )

    if not isinstance(report.get("version"), str) or not sha_or_none(report["version"]):
        raise error
    if any(
        type(report.get(key)) is not int or report[key] < 0
        for key in ("view_count", "git_parent_count")
    ):
        raise error
    for key in ("missing_views", "extra_views", "changed_views"):
        paths = report.get(key)
        if not isinstance(paths, list) or any(not isinstance(path, str) for path in paths):
            raise error
    links = (
        ("parent_version", "expected_parent_version"),
        ("merged_parent_version", "expected_merged_parent_version"),
    )
    if any(not sha_or_none(report.get(key)) for pair in links for key in pair):
        raise error
    links_ok = report["git_parent_count"] <= 2 and all(
        report[left] == report[right] for left, right in links
    )
    if (
        type(report.get("parent_links_ok")) is not bool
        or report["parent_links_ok"] != links_ok
        or type(report.get("ok")) is not bool
        or report["ok"]
        != (
            not any(report[key] for key in ("missing_views", "extra_views", "changed_views"))
            and links_ok
        )
    ):
        raise error
    return report


def _validated_history_report(
    report: object, limit: int, offset: int = 0, expected_head: str | None = None
) -> dict:
    """Check the shape and counts of the coordinator's bounded history report."""
    try:
        return validate_history_page(report, limit, offset, expected_head)
    except ValueError as exc:
        raise ValueError("Reviewer server returned an invalid history report") from exc


def _validated_full_patch(packet: object) -> dict:
    """Reject incomplete artifact and integrated-target patches from a remote packet."""
    error = ValueError("Reviewer server returned an invalid full review patch")
    if not isinstance(packet, dict):
        raise error

    def check_diff(diff: object) -> dict:
        if not isinstance(diff, dict):
            raise error
        patch = diff.get("patch")
        digest = diff.get("sha256")
        if not isinstance(patch, str) or not isinstance(digest, str):
            raise error
        try:
            patch_bytes = patch.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise error from exc
        if (
            diff.get("truncated") is not False
            or len(patch_bytes) > 1_000_000
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or hashlib.sha256(patch_bytes).hexdigest() != digest
        ):
            raise error
        return diff

    artifact_diff = check_diff(packet.get("diff"))
    git = packet.get("git")
    if not isinstance(git, dict) or type(git.get("integrated_into_target")) is not bool:
        raise error
    target_diff = packet.get("target_diff")
    if git["integrated_into_target"]:
        target_diff = check_diff(target_diff)
        if (
            target_diff.get("base_sha") != git.get("base_sha")
            or target_diff.get("target_sha") != git.get("target_sha")
            or target_diff.get("changed_paths") != artifact_diff.get("changed_paths")
        ):
            raise error
    elif target_diff is not None:
        raise error
    return packet


def run_reviewer_command(args) -> dict | list:
    """Call the reviewer HTTP API once, binding write authors to its token identity."""
    origin = _server_url(args.url)
    token = _private_token(args.token_file)
    verify: ssl.SSLContext | bool = True
    if args.ca_file:
        certificate = Path(args.ca_file).expanduser().resolve(strict=True)
        if not certificate.is_file():
            raise ValueError("Reviewer CA file must be a regular file")
        verify = ssl.create_default_context(cafile=str(certificate))

    def request(
        client: httpx.Client,
        method: str,
        path: str,
        data: dict | None = None,
        params: dict | None = None,
    ):
        return request_json(client, method, path, "Reviewer", data, params)

    with httpx.Client(
        base_url=origin,
        headers={"Authorization": f"Bearer {token}"},
        verify=verify,
        trust_env=False,
        follow_redirects=False,
        timeout=20,
    ) as client:
        identity = request(client, "GET", "/whoami")
        if not isinstance(identity, dict) or identity.get("role") != "reviewer":
            raise ValueError("Reviewer token must identify a reviewer role")
        author = identity.get("name")
        if not isinstance(author, str) or not _ID.fullmatch(author):
            raise ValueError("Reviewer server returned an invalid principal")
        if args.action == "whoami":
            return identity
        if args.action == "state":
            return request(client, "GET", "/state")
        if args.action == "intents":
            return request(client, "GET", "/intents")
        if args.action == "decisions":
            return request(client, "GET", "/decisions")
        if args.action == "conflicts":
            return request(client, "GET", "/conflicts")
        if args.action == "tasks":
            return request(client, "GET", "/tasks")
        if args.action == "timeline":
            return request(
                client,
                "GET",
                "/timeline",
                params={
                    key: value
                    for key, value in {
                        "limit": args.limit,
                        "author": args.author,
                        "http_principal": args.http_principal,
                        "event_type": args.event_type,
                        "since": args.since,
                        "until": args.until,
                    }.items()
                    if value is not None
                },
            )
        if args.action == "audit-verify":
            report = request(client, "GET", "/audit/verify", params={"limit": args.limit})
            return _validated_audit_report(report, args.limit)
        if args.action == "audit-snapshot":
            return _validated_snapshot_report(request(client, "GET", "/audit/snapshot"))
        if args.action == "audit-history":

            def fetch(offset: int, head: str | None) -> dict:
                params = {"limit": args.limit, "offset": offset}
                if head is not None or args.expected_head is not None:
                    params["expected_head"] = head or args.expected_head
                report = request(client, "GET", "/audit/history", params=params)
                return _validated_history_report(
                    report, args.limit, offset, head or args.expected_head
                )

            return (
                verify_all_history_pages(fetch, args.limit)
                if args.all
                else fetch(args.offset, args.expected_head)
            )
        if args.action == "inspect":
            packet = request(
                client,
                "GET",
                f"/tasks/{_identifier(args.task_id)}/inspection",
                params={"full_patch": True} if args.full else None,
            )
            return _validated_full_patch(packet) if args.full else packet
        if args.action == "review-intent":
            return request(
                client,
                "POST",
                f"/intents/{_identifier(args.intent_id)}/review",
                {
                    "author": author,
                    "outcome": args.outcome,
                    "rationale": args.rationale,
                    "expected_version": args.version,
                },
            )
        if args.action == "revert-decision":
            return request(
                client,
                "POST",
                f"/decisions/{_identifier(args.decision_id)}/revert",
                {
                    "author": author,
                    "rationale": args.rationale,
                    "expected_version": args.version,
                },
            )
        if args.action == "resolve-conflict":
            return request(
                client,
                "POST",
                f"/conflicts/{_identifier(args.conflict_id)}/resolve",
                {
                    "author": author,
                    "action": args.resolution_action,
                    "rationale": args.rationale,
                    "expected_version": args.version,
                },
            )
        if args.action == "approve":
            return request(
                client,
                "POST",
                f"/tasks/{_identifier(args.task_id)}/merge",
                {
                    "author": author,
                    "rationale": args.rationale,
                    "expected_version": args.version,
                    "expected_target_sha": args.target_sha,
                },
            )
        raise ValueError(f"Unknown reviewer command: {args.action}")
