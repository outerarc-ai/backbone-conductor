"""Command line interface for a repository's local Backbone ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
import unicodedata
from itertools import chain
from pathlib import Path
from typing import Any

from .audit_history import verify_all_history_pages
from .service import Conductor
from .storage import AUDIT_EVENT_TYPES


def _json_file(filename: str) -> dict[str, Any]:
    text = sys.stdin.read() if filename == "-" else Path(filename).read_text(encoding="utf-8")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("JSON input must be an object")
    return value


def _visible(value: str, *, multiline: bool = False) -> str:
    """Keep untrusted review content from emitting terminal controls."""
    output = []
    for character in value:
        code = ord(character)
        if character == "\n" and multiline:
            output.append(character)
        elif character == "\t" and multiline:
            output.append(character)
        elif (
            code < 32
            or 127 <= code <= 159
            or unicodedata.category(character)
            in {
                "Cf",
                "Zl",
                "Zp",
            }
        ):
            output.append(f"\\u{code:04x}" if code > 255 else f"\\x{code:02x}")
        else:
            output.append(character)
    return "".join(output)


def _format_inspection(packet: object) -> str:
    """Render a review packet for humans while keeping JSON as the default CLI format."""
    if not isinstance(packet, dict):
        raise ValueError("Task inspection response is invalid")
    task, intent, git = packet.get("task"), packet.get("intent"), packet.get("git")
    if not all(isinstance(value, dict) for value in (task, intent, git)):
        raise ValueError("Task inspection response is invalid")

    def field(source: dict, key: str) -> str:
        value = source.get(key)
        if not isinstance(value, str):
            raise ValueError("Task inspection response is invalid")
        return _visible(value)

    def items(source: dict, key: str) -> list[str]:
        value = source.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError("Task inspection response is invalid")
        return [_visible(item) for item in value]

    def line_items(label: str, values: list[str]) -> str:
        return f"{label}: {', '.join(values) if values else '(none)'}"

    def prose(label: str, source: dict, key: str, *, indent: str = "") -> str:
        value = source.get(key)
        if not isinstance(value, str):
            raise ValueError("Task inspection response is invalid")
        prefix = f"{indent}  "
        display = _visible(value, multiline=True).replace("\n", "\n" + prefix)
        return f"{indent}{label}:\n{prefix}{display or '(none)'}"

    required = ("base_sha", "artifact_sha", "target_sha", "integrated_into_target")
    if (
        any(key not in git for key in required)
        or not all(isinstance(git[key], str) for key in required[:3])
        or type(git["integrated_into_target"]) is not bool
        or type(git.get("branch_unchanged")) is not bool
        or not isinstance(packet.get("version"), str)
        or not isinstance(packet.get("current_version"), str)
        or not isinstance(packet.get("current_task_status"), str)
        or packet.get("inspection_kind") not in {"current", "approval"}
        or not isinstance(git.get("current_target_sha"), (str, type(None)))
    ):
        raise ValueError("Task inspection response is invalid")
    approval = packet.get("approval")
    if packet["inspection_kind"] == "approval":
        if (
            not isinstance(approval, dict)
            or not isinstance(approval.get("decision"), dict)
            or approval.get("reviewed_version") != packet["version"]
            or approval.get("target_sha") != git["target_sha"]
            or packet["current_task_status"] != "merged"
            or task.get("status") != "submitted"
        ):
            raise ValueError("Task approval inspection response is invalid")
    elif (
        approval is not None
        or packet["current_version"] != packet["version"]
        or packet["current_task_status"] != task.get("status")
        or git["current_target_sha"] != git["target_sha"]
    ):
        raise ValueError("Task inspection response is invalid")
    artifact = task.get("artifact")
    delta = packet.get("decision_delta")
    decisions = packet.get("accepted_decisions")
    blockers = packet.get("blocking_conflicts")
    if (
        not isinstance(artifact, dict)
        or not isinstance(delta, dict)
        or not isinstance(decisions, list)
        or not isinstance(blockers, list)
        or any(not isinstance(item, dict) for item in chain(decisions, blockers))
        or any(not isinstance(item.get("evidence"), dict) for item in blockers)
    ):
        raise ValueError("Task inspection response is invalid")

    lines = [
        f"Task: {field(task, 'id')} (now {field(packet, 'current_task_status')})",
        f"Inspection: {field(packet, 'inspection_kind')}",
        f"Reviewed ledger version: {_visible(packet['version'])}",
        f"Current ledger version: {_visible(packet['current_version'])}",
        f"Base commit: {_visible(git['base_sha'])}",
        f"Artifact commit: {_visible(git['artifact_sha'])}",
        f"Reviewed target commit: {_visible(git['target_sha'])}",
        "Current target branch commit: "
        + (_visible(git["current_target_sha"]) if git["current_target_sha"] else "(missing)"),
        f"Artifact branch unchanged now: {git['branch_unchanged']}",
        f"Integrated into reviewed target: {git['integrated_into_target']}",
        line_items("Net changed target paths", items(git, "net_changed_paths")),
        line_items("Divergent target paths", items(git, "divergent_paths")),
        "The default JSON packet contains all fields and structured evidence.",
        "Terminal control characters in review content are escaped for display.",
        "",
        "=== Review context ===",
        f"Intent: {field(intent, 'id')} ({field(intent, 'status')}) by {field(intent, 'author')}",
        prose("Problem", intent, "problem"),
        prose("Proposed outcome", intent, "proposed_outcome"),
        line_items("Affected paths", items(intent, "affected_paths")),
        line_items("Affected symbols", items(intent, "affected_symbols")),
        line_items("Intent constraints", items(intent, "constraints")),
        f"Member: {field(task, 'member_id')}",
        f"Target branch: {field(task, 'base_ref')}",
        prose("Task specification", task, "spec"),
        line_items("Task constraints", items(task, "constraints")),
        line_items("Forbidden paths", items(task, "forbidden_paths")),
        f"Artifact branch: {field(artifact, 'branch')}",
        prose("Artifact summary", artifact, "summary"),
        line_items("Artifact paths", items(artifact, "changed_paths")),
        line_items("New accepted decisions since fork", items(delta, "new_decisions")),
        line_items("Withdrawn decisions since fork", items(delta, "withdrawn_decisions")),
        f"Accepted decisions at inspection ({len(decisions)}):",
    ]
    if approval is not None:
        decision = approval["decision"]
        lines.extend(
            [
                f"Approval decision: {field(decision, 'id')} "
                f"(now {field(decision, 'status')}) by {field(decision, 'author')}",
                prose("Approval rationale", decision, "rationale"),
            ]
        )
    for decision in decisions:
        lines.extend(
            [
                f"  {field(decision, 'id')} [{field(decision, 'decision_type')}]",
                prose("Summary", decision, "summary", indent="  "),
                prose("Rationale", decision, "rationale", indent="  "),
            ]
        )
    lines.append(f"Blocking conflicts at inspection ({len(blockers)}):")
    for conflict in blockers:
        lines.extend(
            [
                f"  {field(conflict, 'id')} [{field(conflict, 'severity')}, "
                f"{field(conflict, 'conflict_type')}]",
                f"    Rule: {field(conflict, 'rule')}",
                f"    Parties: {', '.join(items(conflict, 'parties'))}",
                f"    Evidence: {_visible(json.dumps(conflict.get('evidence'), ensure_ascii=False, sort_keys=True))}",
            ]
        )

    def append_patch(title: str, value: object) -> None:
        if not isinstance(value, dict):
            raise ValueError("Task inspection response is invalid")
        patch, digest, truncated = (
            value.get("patch"),
            value.get("sha256"),
            value.get("truncated"),
        )
        if (
            not isinstance(patch, str)
            or not isinstance(digest, str)
            or type(truncated) is not bool
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ValueError("Task inspection response is invalid")
        if not truncated and hashlib.sha256(patch.encode("utf-8")).hexdigest() != digest:
            raise ValueError("Task inspection patch does not match its SHA-256")
        lines.extend(
            [
                "",
                f"=== {title}{' — TRUNCATED PREVIEW' if truncated else ''} ===",
                f"Full patch SHA-256: {digest}",
            ]
        )
        if truncated:
            lines.append("Review the complete patch with --full or in a Git checkout.")
        displayed = _visible(patch, multiline=True)
        if displayed:
            lines.append(displayed.removesuffix("\n"))

    append_patch("Submitted artifact diff", packet.get("diff"))
    target = packet.get("target_diff")
    if git["integrated_into_target"]:
        if not isinstance(target, dict) or (
            target.get("base_sha") != git["base_sha"]
            or target.get("target_sha") != git["target_sha"]
            or target.get("changed_paths") != packet["diff"].get("changed_paths")
        ):
            raise ValueError("Task inspection target diff is not bound to the target commit")
        append_patch("Integrated target paths versus base", target)
    elif target is not None:
        raise ValueError("Task inspection response is invalid")
    else:
        lines.extend(["", "Integrated target diff: pending Git merge."])
    return "\n".join(lines)


def _prompts_file(filename: str) -> list[str]:
    value = json.loads(Path(filename).read_text(encoding="utf-8"))
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError("DSH prompts file must be a nonempty JSON array of nonempty strings")
    return value


def _interactive_prompts():
    """Read one prompt at a time; EOF or :quit ends the current SDK session."""
    while True:
        if sys.stdin.isatty():
            print("backbone> ", end="", file=sys.stderr, flush=True)
        line = sys.stdin.readline()
        if not line:
            return
        prompt = line.rstrip("\r\n")
        if prompt.strip().lower() in {":quit", ":exit"}:
            return
        if prompt.strip():
            yield prompt


def _run_interactive(runner: Any, session_id: str | None) -> None:
    prompts = _interactive_prompts()
    first_prompt = next(prompts, None)
    if first_prompt is None:
        return

    def emit(turn: dict) -> None:
        print(json.dumps(turn, ensure_ascii=False), flush=True)

    runner.run_turns(chain((first_prompt,), prompts), session_id=session_id, on_turn=emit)


def _validate_tls_key(filename: str, repo: str) -> None:
    """Keep the server's private key out of the ledger and other users' reach."""
    key = Path(filename).expanduser().absolute()
    if key.is_symlink():
        raise ValueError("TLS private key must be a regular file, not a symlink")
    resolved = key.resolve(strict=True)
    if resolved.is_relative_to(Path(repo).expanduser().resolve()):
        raise ValueError("TLS private key must be outside the repository")
    mode = key.stat(follow_symlinks=False).st_mode
    if not stat.S_ISREG(mode):
        raise ValueError("TLS private key must be a regular file")
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("TLS private key must be accessible only to its owner (0600)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="backbone", description="Git-native intent and decision coordination."
    )
    parser.add_argument(
        "--repo", default=".", help="Git repository path (default: current directory)"
    )
    parser.add_argument(
        "--ledger-branch", help="Use the separate backbone metadata branch worktree"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    ledger = commands.add_parser("ledger", help="Manage a separate metadata branch")
    ledger_actions = ledger.add_subparsers(dest="action", required=True)
    ledger_actions.add_parser("create", help="Create a metadata-only backbone branch")
    ledger_actions.add_parser("migrate", help="Move a quiescent inline ledger with Git history")
    attach = ledger_actions.add_parser("attach", help="Attach a fetched backbone branch")
    attach.add_argument("--remote", default="origin")
    commands.add_parser("init", help="Initialize the Backbone ledger in an existing Git repository")
    commands.add_parser("status", help="Print the complete ledger state")
    for kind in ("intent", "decision"):
        group = commands.add_parser(kind).add_subparsers(dest="action", required=True)
        create = group.add_parser("create")
        create.add_argument("--file", required=True, help="JSON object file, or - for stdin")
        transition = group.add_parser("transition")
        transition.add_argument("id")
        transition.add_argument("status")
        group.add_parser("list")
        if kind == "intent":
            revise = group.add_parser("revise", help="Revise an undispatched intent")
            revise.add_argument("id")
            revise.add_argument("--file", required=True, help="JSON patch file, or - for stdin")
            revise.add_argument("--author", required=True)
            revise.add_argument(
                "--version", required=True, help="Backbone version observed before editing"
            )
            replace = group.add_parser(
                "replace", help="Draft an audited replacement for an accepted intent"
            )
            replace.add_argument("id")
            replace.add_argument("--file", required=True, help="JSON patch file, or - for stdin")
            replace.add_argument("--author", required=True)
            replace.add_argument("--reason", required=True)
            replace.add_argument(
                "--version", required=True, help="Backbone version observed before editing"
            )
            review = group.add_parser("review", help="Accept or reject a draft with a rationale")
            review.add_argument("id")
            review.add_argument("--outcome", required=True, choices=["accepted", "rejected"])
            review.add_argument("--author", required=True)
            review.add_argument("--rationale", required=True)
            review.add_argument(
                "--version", required=True, help="Backbone version observed before review"
            )

    revert = commands.add_parser("revert", help="Withdraw an accepted decision with audit evidence")
    revert.add_argument("decision_id")
    revert.add_argument("--author", required=True)
    revert.add_argument("--rationale", required=True)
    revert.add_argument("--version", required=True, help="Backbone version observed before revert")

    tasks = commands.add_parser("task").add_subparsers(dest="action", required=True)
    dispatch = tasks.add_parser("dispatch", help="Assign an accepted intent to a member")
    dispatch.add_argument("intent_id")
    dispatch.add_argument("--member", required=True)
    spec = dispatch.add_mutually_exclusive_group()
    spec.add_argument("--spec", default="", help="Task specification text")
    spec.add_argument("--spec-file", help="Read the task specification from this file")
    dispatch.add_argument(
        "--forbid", action="append", default=[], help="Forbidden path (repeatable)"
    )
    listing = tasks.add_parser("list")
    listing.add_argument("--member", required=True)
    start = tasks.add_parser("start")
    start.add_argument("task_id")
    start.add_argument("--member", required=True)
    inspect = tasks.add_parser("inspect", help="Inspect submitted or approved code review")
    inspect.add_argument("task_id")
    inspect.add_argument(
        "--full", action="store_true", help="Include the complete bounded Git patch"
    )
    inspect.add_argument(
        "--format", choices=("json", "text"), default="json", help="Review packet output format"
    )
    fetch = tasks.add_parser("fetch", help="Fetch an assigned member's pushed code branch")
    fetch.add_argument("task_id")
    fetch.add_argument("--member", required=True)
    fetch.add_argument("--branch", required=True)
    fetch.add_argument("--expected-sha", required=True)
    fetch.add_argument("--remote", default="origin")
    submit = tasks.add_parser("submit")
    submit.add_argument("--member", required=True)
    submit.add_argument("--file", required=True, help="Artifact JSON object, or - for stdin")
    merge = tasks.add_parser(
        "merge", help="Record human approval after the artifact has been merged with Git"
    )
    merge.add_argument("task_id")
    merge.add_argument("--author", required=True)
    merge.add_argument(
        "--rationale", help="Required when decisions or integrated code changed after submission"
    )
    merge.add_argument("--version", required=True, help="Version from task inspect after Git merge")
    merge.add_argument(
        "--target-sha", required=True, help="Target SHA from task inspect after Git merge"
    )
    cancel = tasks.add_parser("cancel", help="Cancel active work with an audited reason")
    cancel.add_argument("task_id")
    cancel.add_argument("--author", required=True)
    cancel.add_argument("--reason", required=True)
    rebase = tasks.add_parser("rebase", help="Refresh task context without changing code")
    rebase.add_argument("task_id")
    rebase.add_argument("--member", required=True)
    rebase.add_argument("--version", required=True, help="Backbone version observed before refresh")

    conflicts = commands.add_parser("conflict").add_subparsers(dest="action", required=True)
    conflicts.add_parser("check", help="Detect and record deterministic conflicts")
    conflicts.add_parser("list")
    advise = conflicts.add_parser("advise", help="Get read-only DSH advice for two live intents")
    advise.add_argument("left_intent_id")
    advise.add_argument("right_intent_id")
    advise.add_argument("--dsh-home", required=True, help="Private DSH home outside the repo")
    advise.add_argument("--model", required=True)
    advise.add_argument("--provider", default="deepseek-official")
    resolve = conflicts.add_parser("resolve")
    resolve.add_argument("conflict_id")
    resolve.add_argument("--author", required=True)
    resolve.add_argument("--action", dest="resolution_action", required=True)
    resolve.add_argument("--rationale", required=True)
    resolve.add_argument("--version", required=True, help="Version observed in state")

    sync = commands.add_parser("sync", help="Synchronize with the configured Git remote")
    sync.add_argument("--remote", default="origin")
    sync.add_argument("--branch")
    refresh = commands.add_parser("refresh", help="Fetch and fast-forward from the Git remote")
    refresh.add_argument("--remote", default="origin")
    refresh.add_argument("--branch")
    reconcile = commands.add_parser("reconcile", help="Merge reviewed Backbone metadata")
    reconcile.add_argument("--remote", default="origin")
    reconcile.add_argument("--branch")
    reconcile.add_argument("--local-head", required=True)
    reconcile.add_argument("--remote-head", required=True)
    reconcile.add_argument("--author", required=True)
    reconcile.add_argument("--rationale", required=True)
    reconcile.add_argument(
        "--resolutions-file", help="JSON decisions for every competing metadata object"
    )
    updates = commands.add_parser("updates", help="Check ledger changes affecting a member")
    updates.add_argument("--member")
    updates.add_argument("--since-version")
    log = commands.add_parser("log")
    log.add_argument("--limit", type=int, default=50)
    log.add_argument("--author", help="Exact Git author name")
    log.add_argument("--http-principal", help="Exact authenticated HTTP principal")
    log.add_argument("--type", dest="event_type", choices=AUDIT_EVENT_TYPES)
    log.add_argument("--since", help="Inclusive ISO 8601 author timestamp with timezone")
    log.add_argument("--until", help="Inclusive ISO 8601 author timestamp with timezone")
    audit = commands.add_parser("audit", help="Verify Git audit signatures and snapshot integrity")
    audit_actions = audit.add_subparsers(dest="action", required=True)
    verify = audit_actions.add_parser("verify", help="Verify recent Backbone commit signatures")
    verify.add_argument("--limit", type=int, default=50)
    verify.add_argument(
        "--require-signatures",
        action="store_true",
        help="Exit nonzero unless all inspected commits have valid signatures",
    )
    audit_actions.add_parser(
        "verify-snapshot", help="Check current metadata views and Git parent version links"
    )
    history = audit_actions.add_parser(
        "verify-history", help="Check reachable metadata snapshots, up to a bounded limit"
    )
    history.add_argument("--limit", type=int, default=50)
    history_scope = history.add_mutually_exclusive_group()
    history_scope.add_argument("--offset", type=int, default=0)
    history_scope.add_argument("--all", action="store_true")
    history.add_argument("--expected-head")
    commands.add_parser("schema", help="Print protocol JSON Schema")
    review = commands.add_parser("review", help="Request advisory semantic review through DSH")
    review.add_argument("task_id")
    review.add_argument("--dsh-home", required=True, help="Path to the configured DSH home")
    review.add_argument("--model", required=True)
    review.add_argument("--provider", default="deepseek-official")
    review.add_argument(
        "--attempt-log", help="Private JSONL outcome log outside the repository for DSH reviews"
    )
    review_stats = commands.add_parser("review-stats", help="Summarize private DSH review attempts")
    review_stats.add_argument("--attempt-log", required=True, help="Private review JSONL log")
    dsh = commands.add_parser("dsh", help="Run a member-scoped DSH agent with Backbone MCP")
    dsh.add_argument("--member", required=True)
    dsh.add_argument("--workspace", required=True, help="Separate coding workspace or worktree")
    dsh.add_argument(
        "--dsh-home", required=True, help="Dedicated DSH home outside both repositories"
    )
    dsh.add_argument("--model", required=True)
    dsh.add_argument("--provider", default="deepseek-official")
    dsh.add_argument("--session-id", help="Session ID (cross-process resume is unavailable)")
    dsh.add_argument("--mcp-url", help="Authenticated remote coordinator /mcp URL")
    dsh.add_argument("--mcp-token-file", help="Private file containing this member's bearer token")
    dsh.add_argument("--mcp-ca-file", help="CA certificate for a remote HTTPS coordinator")
    dsh_prompt = dsh.add_mutually_exclusive_group(required=True)
    dsh_prompt.add_argument("--prompt")
    dsh_prompt.add_argument("--prompt-file", help="UTF-8 prompt file")
    dsh_prompt.add_argument(
        "--prompts-file", help="JSON array of prompts run in one persistent SDK process"
    )
    dsh_prompt.add_argument(
        "--interactive", action="store_true", help="Read prompts from stdin until EOF or :quit"
    )
    coordinator = commands.add_parser(
        "conductor", help="Run a limited DSH coordination agent with Backbone MCP"
    )
    coordinator.add_argument("--dsh-home", required=True, help="Private DSH home outside the repo")
    coordinator.add_argument("--model", required=True)
    coordinator.add_argument("--provider", default="deepseek-official")
    coordinator.add_argument(
        "--session-id", help="Session ID (cross-process resume is unavailable)"
    )
    coordinator_prompt = coordinator.add_mutually_exclusive_group(required=True)
    coordinator_prompt.add_argument("--prompt")
    coordinator_prompt.add_argument("--prompt-file", help="UTF-8 prompt file")
    coordinator_prompt.add_argument(
        "--prompts-file", help="JSON array of prompts run in one persistent SDK process"
    )
    coordinator_prompt.add_argument(
        "--interactive", action="store_true", help="Read prompts from stdin until EOF or :quit"
    )

    reviewer = commands.add_parser(
        "reviewer", help="Review through an authenticated HTTP server without a local Git clone"
    )
    member_check = commands.add_parser(
        "member-check", help="Verify remote MCP member identity and tool scope without a model"
    )
    member_check.add_argument("--mcp-url", required=True, help="Remote coordinator /mcp URL")
    member_check.add_argument("--member", required=True, help="Expected member principal")
    member_check.add_argument(
        "--token-file", required=True, help="Private member bearer-token file"
    )
    member_check.add_argument("--ca-file", help="CA certificate for a trusted HTTPS server")
    member = commands.add_parser(
        "member", help="Use authenticated HTTP member workflow without a local coordinator clone"
    )
    member.add_argument("--url", required=True, help="Coordinator HTTP(S) server origin")
    member.add_argument("--token-file", required=True, help="Private member bearer-token file")
    member.add_argument("--ca-file", help="CA certificate for a trusted HTTPS server")
    member_actions = member.add_subparsers(dest="action", required=True)
    member_actions.add_parser("whoami", help="Check the token's member identity")
    member_actions.add_parser("tasks", help="Read assigned task context")
    member_updates = member_actions.add_parser(
        "updates", help="Check changed decisions and conflicts"
    )
    member_updates.add_argument("--since-version", help="Previously observed ledger version")
    for action in ("create-intent", "propose-decision"):
        proposal = member_actions.add_parser(action)
        proposal.add_argument("--file", required=True, help="JSON object file, or - for stdin")
    member_start = member_actions.add_parser("start", help="Start an assigned task")
    member_start.add_argument("task_id")
    member_rebase = member_actions.add_parser("rebase", help="Refresh task decision context")
    member_rebase.add_argument("task_id")
    member_rebase.add_argument("--version", required=True, help="Observed ledger version")
    member_fetch = member_actions.add_parser(
        "fetch", help="Fetch a pushed code branch by exact SHA"
    )
    member_fetch.add_argument("task_id")
    member_fetch.add_argument("--branch", required=True, help="Pushed feature branch name")
    member_fetch.add_argument("--sha", required=True, help="Full lowercase pushed commit SHA")
    member_fetch.add_argument("--remote", default="origin", help="Configured code Git remote")
    member_submit = member_actions.add_parser("submit", help="Submit a fetched Git artifact")
    member_submit.add_argument("task_id")
    member_submit.add_argument(
        "--file", required=True, help="Artifact JSON object file, or - for stdin"
    )
    reviewer.add_argument("--url", required=True, help="Coordinator HTTP(S) server origin")
    reviewer.add_argument("--token-file", required=True, help="Private reviewer bearer-token file")
    reviewer.add_argument("--ca-file", help="CA certificate for a trusted HTTPS server")
    reviewer_actions = reviewer.add_subparsers(dest="action", required=True)
    for action in ("whoami", "state", "intents", "decisions", "conflicts", "tasks"):
        reviewer_actions.add_parser(action)
    reviewer_timeline = reviewer_actions.add_parser(
        "timeline", help="Read the coordinator's Git audit timeline"
    )
    reviewer_timeline.add_argument("--limit", type=int, default=50)
    reviewer_timeline.add_argument("--author", help="Exact Git author name")
    reviewer_timeline.add_argument("--http-principal", help="Exact authenticated HTTP principal")
    reviewer_timeline.add_argument("--type", dest="event_type", choices=AUDIT_EVENT_TYPES)
    reviewer_timeline.add_argument("--since", help="Inclusive ISO 8601 timestamp with timezone")
    reviewer_timeline.add_argument("--until", help="Inclusive ISO 8601 timestamp with timezone")
    reviewer_audit = reviewer_actions.add_parser(
        "audit-verify", help="Read the coordinator's Git signature verification report"
    )
    reviewer_audit.add_argument("--limit", type=int, default=50)
    reviewer_audit.add_argument(
        "--require-signatures",
        action="store_true",
        help="Exit nonzero unless every inspected commit has a valid signature",
    )
    reviewer_actions.add_parser(
        "audit-snapshot", help="Read the coordinator's current metadata consistency report"
    )
    reviewer_history = reviewer_actions.add_parser(
        "audit-history", help="Read the coordinator's bounded metadata history report"
    )
    reviewer_history.add_argument("--limit", type=int, default=50)
    reviewer_history_scope = reviewer_history.add_mutually_exclusive_group()
    reviewer_history_scope.add_argument("--offset", type=int, default=0)
    reviewer_history_scope.add_argument("--all", action="store_true")
    reviewer_history.add_argument("--expected-head")
    reviewer_inspect = reviewer_actions.add_parser(
        "inspect", help="Read submitted or approved task review"
    )
    reviewer_inspect.add_argument("task_id")
    reviewer_inspect.add_argument(
        "--full", action="store_true", help="Fetch and verify the complete bounded Git patch"
    )
    reviewer_inspect.add_argument(
        "--format", choices=("json", "text"), default="json", help="Review packet output format"
    )
    reviewer_intent = reviewer_actions.add_parser("review-intent", help="Accept or reject a draft")
    reviewer_intent.add_argument("intent_id")
    reviewer_intent.add_argument("--outcome", required=True, choices=["accepted", "rejected"])
    reviewer_intent.add_argument("--rationale", required=True)
    reviewer_intent.add_argument("--version", required=True, help="Version observed in state")
    reviewer_revert = reviewer_actions.add_parser(
        "revert-decision", help="Withdraw an accepted decision with an audit rationale"
    )
    reviewer_revert.add_argument("decision_id")
    reviewer_revert.add_argument("--rationale", required=True)
    reviewer_revert.add_argument("--version", required=True, help="Version observed in state")
    reviewer_resolve = reviewer_actions.add_parser(
        "resolve-conflict", help="Record a human arbitration decision"
    )
    reviewer_resolve.add_argument("conflict_id")
    reviewer_resolve.add_argument(
        "--action",
        dest="resolution_action",
        required=True,
        choices=["accept_existing", "override_existing", "coordinate", "accept_risk"],
    )
    reviewer_resolve.add_argument("--rationale", required=True)
    reviewer_resolve.add_argument("--version", required=True, help="Version observed in state")
    reviewer_approve = reviewer_actions.add_parser(
        "approve", help="Record approval after the code has been merged with Git"
    )
    reviewer_approve.add_argument("task_id")
    reviewer_approve.add_argument("--rationale", required=True)
    reviewer_approve.add_argument("--version", required=True, help="Version from task inspect")
    reviewer_approve.add_argument("--target-sha", required=True, help="SHA from task inspect")

    serve = commands.add_parser("serve", help="Run the HTTP API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--auth-file", help="Private JSON file of bearer-token digests")
    serve.add_argument(
        "--mcp-http", action="store_true", help="Expose bearer-authenticated member MCP at /mcp"
    )
    serve.add_argument(
        "--mcp-allowed-host",
        action="append",
        default=[],
        help="Additional public Host header accepted by MCP (repeatable)",
    )
    serve.add_argument("--tls-certfile", help="PEM certificate for direct HTTPS")
    serve.add_argument("--tls-keyfile", help="PEM private key for direct HTTPS")
    auth = commands.add_parser("auth", help="Create a private HTTP token-digest file")
    auth_commands = auth.add_subparsers(dest="action", required=True)
    create_auth = auth_commands.add_parser("create")
    create_auth.add_argument("--file", required=True, help="New file outside the Git repository")
    create_auth.add_argument("--admin", required=True, help="Administrator principal name")
    create_auth.add_argument("--member", action="append", default=[], help="Member principal name")
    create_auth.add_argument(
        "--reviewer", action="append", default=[], help="Reviewer principal name"
    )
    rotate_auth = auth_commands.add_parser("rotate", help="Replace tokens without restarting HTTP")
    rotate_auth.add_argument("--file", required=True, help="Existing private token file")
    rotate_auth.add_argument("--admin", required=True, help="Administrator principal name")
    rotate_auth.add_argument("--member", action="append", default=[], help="Member principal name")
    rotate_auth.add_argument(
        "--reviewer", action="append", default=[], help="Reviewer principal name"
    )
    rotate_one = auth_commands.add_parser(
        "rotate-one", help="Replace one principal's token without changing other tokens"
    )
    rotate_one.add_argument("--file", required=True, help="Existing private token file")
    rotate_one.add_argument("--name", required=True, help="Existing principal to rotate")
    add_principal = auth_commands.add_parser(
        "add", help="Issue a new principal without changing existing tokens"
    )
    add_principal.add_argument("--file", required=True, help="Existing private token file")
    add_principal.add_argument("--name", required=True, help="New principal name")
    add_principal.add_argument("--role", required=True, choices=["admin", "member", "reviewer"])
    revoke_principal = auth_commands.add_parser("revoke", help="Remove one existing principal")
    revoke_principal.add_argument("--file", required=True, help="Existing private token file")
    revoke_principal.add_argument("--name", required=True, help="Principal to revoke")
    mcp = commands.add_parser("mcp", help="Run the MCP server over stdio")
    mcp_scope = mcp.add_mutually_exclusive_group()
    mcp_scope.add_argument("--member", help="Bind member operations and omit administrator tools")
    mcp_scope.add_argument("--coordinator", action="store_true", help="Expose limited agent tools")
    remote_member_mcp = commands.add_parser(
        "mcp-remote-member", help="Bridge an authenticated remote member MCP into local stdio"
    )
    remote_member_mcp.add_argument("--url", required=True, help="Remote HTTPS /mcp URL")
    remote_member_mcp.add_argument("--member", required=True, help="Expected member principal")
    remote_member_mcp.add_argument("--token-file", required=True, help="Private member token file")
    remote_member_mcp.add_argument("--ca-file", help="Trusted private CA certificate")
    return parser


def _run(args: argparse.Namespace) -> Any:
    if args.command == "ledger":
        from .ledger import attach_ledger, create_ledger, migrate_ledger

        if args.ledger_branch is not None:
            raise ValueError("Ledger setup uses --repo only; omit --ledger-branch")
        if args.action == "create":
            return create_ledger(args.repo)
        if args.action == "migrate":
            return migrate_ledger(args.repo)
        return attach_ledger(args.repo, args.remote)
    if args.command == "schema":
        from .models import BackboneState

        return BackboneState.model_json_schema()
    if args.command == "serve":
        import uvicorn

        from .api import create_app

        mcp_setting = os.environ.get("BACKBONE_MCP_HTTP", "0")
        if mcp_setting not in {"0", "1"}:
            raise ValueError("BACKBONE_MCP_HTTP must be 0 or 1")
        mcp_http = args.mcp_http or mcp_setting == "1"
        env_hosts = os.environ.get("BACKBONE_MCP_ALLOWED_HOSTS", "")
        mcp_allowed_hosts = [*args.mcp_allowed_host]
        if env_hosts:
            mcp_allowed_hosts.extend(host.strip() for host in env_hosts.split(","))
        if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.auth_file:
            raise ValueError("Non-loopback HTTP binding requires --auth-file")
        if mcp_http and not args.auth_file:
            raise ValueError("Streamable HTTP MCP requires --auth-file")
        if mcp_allowed_hosts and not mcp_http:
            raise ValueError("MCP allowed hosts require --mcp-http or BACKBONE_MCP_HTTP=1")
        if any(
            not host.strip()
            or "/" in host
            or "@" in host
            or "*" in host
            or any(char.isspace() for char in host)
            for host in mcp_allowed_hosts
        ):
            raise ValueError("MCP allowed hosts must be Host header values, not URLs")
        if bool(args.tls_certfile) != bool(args.tls_keyfile):
            raise ValueError("Direct HTTPS requires both --tls-certfile and --tls-keyfile")
        if args.tls_keyfile:
            _validate_tls_key(args.tls_keyfile, args.repo)
        uvicorn.run(
            create_app(
                args.repo,
                auth_file=args.auth_file,
                ledger_branch=args.ledger_branch,
                mcp_http=mcp_http,
                mcp_allowed_hosts=tuple(mcp_allowed_hosts),
            ),
            host=args.host,
            port=args.port,
            ssl_certfile=args.tls_certfile,
            ssl_keyfile=args.tls_keyfile,
        )
        return None
    if args.command == "auth":
        from .auth import (
            add_principal_token,
            create_token_file,
            revoke_principal_token,
            rotate_principal_token,
            rotate_token_file,
        )

        conductor = Conductor(args.repo, ledger_branch=args.ledger_branch)
        if args.action == "revoke":
            return {
                "file": args.file,
                "revoked": revoke_principal_token(args.file, conductor.code_store.root, args.name),
            }
        if args.action == "rotate-one":
            credentials = [rotate_principal_token(args.file, conductor.code_store.root, args.name)]
        elif args.action == "add":
            credentials = [
                add_principal_token(args.file, conductor.code_store.root, args.name, args.role)
            ]
        elif args.action == "create":
            credentials = create_token_file(
                args.file, conductor.code_store.root, args.admin, args.member, args.reviewer
            )
        else:
            credentials = rotate_token_file(
                args.file, conductor.code_store.root, args.admin, args.member, args.reviewer
            )
        return {
            "file": args.file,
            "credentials": credentials,
            "detail": "Save these plaintext tokens now; only SHA-256 digests are stored in the file.",
        }
    if args.command == "mcp":
        from .mcp_server import create_coordinator_server, create_server

        server = (
            create_coordinator_server(args.repo, ledger_branch=args.ledger_branch)
            if args.coordinator
            else create_server(args.repo, member_id=args.member, ledger_branch=args.ledger_branch)
        )
        server.run(transport="stdio")
        return None
    if args.command == "conductor":
        from .dsh_agent import DSHCoordinatorRunner

        prompts = _prompts_file(args.prompts_file) if args.prompts_file else None
        runner = DSHCoordinatorRunner(
            args.repo, args.dsh_home, args.model, args.provider, ledger_branch=args.ledger_branch
        )
        if args.interactive:
            return _run_interactive(runner, args.session_id)
        if prompts is not None:
            return runner.run_turns(prompts, session_id=args.session_id)
        prompt = (
            Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
        )
        return runner.run(prompt, session_id=args.session_id)
    if args.command == "dsh":
        from .dsh_agent import DSHMemberRunner, DSHRemoteMemberRunner

        prompts = _prompts_file(args.prompts_file) if args.prompts_file else None
        prompt = (
            (
                Path(args.prompt_file).read_text(encoding="utf-8")
                if args.prompt_file
                else args.prompt
            )
            if prompts is None
            else None
        )
        if args.mcp_url:
            if not args.mcp_token_file:
                raise ValueError("Remote DSH requires --mcp-token-file")
            if args.ledger_branch:
                raise ValueError("Remote DSH does not use --ledger-branch")
            runner = DSHRemoteMemberRunner(
                args.workspace,
                args.dsh_home,
                args.member,
                args.model,
                args.mcp_url,
                args.mcp_token_file,
                args.provider,
                ca_file=args.mcp_ca_file,
            )
            if args.interactive:
                return _run_interactive(runner, args.session_id)
            return (
                runner.run_turns(prompts, session_id=args.session_id)
                if prompts is not None
                else runner.run(prompt, session_id=args.session_id)
            )
        if args.mcp_token_file or args.mcp_ca_file:
            raise ValueError("--mcp-token-file and --mcp-ca-file require --mcp-url")
        runner = DSHMemberRunner(
            args.repo,
            args.workspace,
            args.dsh_home,
            args.member,
            args.model,
            args.provider,
            ledger_branch=args.ledger_branch,
        )
        if args.interactive:
            return _run_interactive(runner, args.session_id)
        return (
            runner.run_turns(prompts, session_id=args.session_id)
            if prompts is not None
            else runner.run(prompt, session_id=args.session_id)
        )
    if args.command == "reviewer":
        from .reviewer_client import run_reviewer_command

        if args.ledger_branch:
            raise ValueError("Remote reviewer does not use --ledger-branch")
        return run_reviewer_command(args)
    if args.command == "member-check":
        from .dsh_agent import preflight_remote_member

        if args.ledger_branch:
            raise ValueError("Remote member check does not use --ledger-branch")
        return preflight_remote_member(args.mcp_url, args.member, args.token_file, args.ca_file)
    if args.command == "mcp-remote-member":
        from .remote_member_bridge import create_remote_member_bridge

        if args.ledger_branch:
            raise ValueError("Remote member MCP does not use --ledger-branch")
        server = create_remote_member_bridge(args.url, args.member, args.token_file, args.ca_file)
        server.run(transport="stdio")
        return None
    if args.command == "member":
        from .member_client import run_member_command

        if args.ledger_branch:
            raise ValueError("Remote member does not use --ledger-branch")
        payload = (
            _json_file(args.file)
            if args.action in {"create-intent", "propose-decision", "submit"}
            else None
        )
        return run_member_command(args, payload)

    conductor = Conductor(args.repo, ledger_branch=args.ledger_branch)
    if args.command == "init":
        return conductor.initialize()
    if args.command == "status":
        return conductor.state()
    if args.command == "revert":
        return conductor.revert_decision(
            args.decision_id, args.author, args.rationale, args.version
        )
    if args.command in {"intent", "decision"}:
        if args.action == "list":
            return list(conductor.state()[f"{args.command}s"].values())
        if args.action == "create":
            method = conductor.create_intent if args.command == "intent" else conductor.log_decision
            return method(_json_file(args.file))
        if args.command == "intent" and args.action == "revise":
            return conductor.revise_intent(
                args.id, _json_file(args.file), args.author, args.version
            )
        if args.command == "intent" and args.action == "replace":
            return conductor.replace_intent(
                args.id, _json_file(args.file), args.author, args.reason, args.version
            )
        if args.command == "intent" and args.action == "review":
            return conductor.review_intent(
                args.id, args.outcome, args.author, args.rationale, args.version
            )
        method = (
            conductor.transition_intent
            if args.command == "intent"
            else conductor.transition_decision
        )
        return method(args.id, args.status)
    if args.command == "task":
        if args.action == "dispatch":
            spec = Path(args.spec_file).read_text(encoding="utf-8") if args.spec_file else args.spec
            return conductor.dispatch_task(args.intent_id, args.member, spec, args.forbid)
        if args.action == "list":
            return conductor.get_my_task(args.member)
        if args.action == "start":
            return conductor.start_task(args.task_id, args.member)
        if args.action == "inspect":
            return conductor.inspect_task(args.task_id, full_patch=args.full)
        if args.action == "fetch":
            return conductor.fetch_artifact_branch(
                args.task_id, args.member, args.branch, args.expected_sha, args.remote
            )
        if args.action == "submit":
            return conductor.submit_artifact(args.member, _json_file(args.file))
        if args.action == "cancel":
            return conductor.cancel_task(args.task_id, args.author, args.reason)
        if args.action == "rebase":
            return conductor.rebase_task(args.task_id, args.member, args.version)
        return conductor.merge_task(
            args.task_id,
            args.author,
            args.rationale,
            expected_version=args.version,
            expected_target_sha=args.target_sha,
        )
    if args.command == "conflict":
        if args.action == "check":
            return conductor.detect_conflicts()
        if args.action == "list":
            return list(conductor.state()["conflicts"].values())
        if args.action == "advise":
            return conductor.advise_intent_conflict(
                args.left_intent_id,
                args.right_intent_id,
                args.dsh_home,
                args.model,
                args.provider,
            )
        return conductor.resolve_conflict(
            args.conflict_id,
            args.author,
            args.resolution_action,
            args.rationale,
            args.version,
        )
    if args.command == "sync":
        return conductor.sync(args.remote, args.branch)
    if args.command == "refresh":
        return conductor.refresh(args.remote, args.branch)
    if args.command == "reconcile":
        return conductor.reconcile(
            args.local_head,
            args.remote_head,
            args.author,
            args.rationale,
            args.remote,
            args.branch,
            _json_file(args.resolutions_file) if args.resolutions_file else None,
        )
    if args.command == "updates":
        return conductor.check_backbone_sync(args.member, args.since_version)
    if args.command == "review":
        return conductor.review_task(
            args.task_id, args.dsh_home, args.model, args.provider, attempt_log=args.attempt_log
        )
    if args.command == "review-stats":
        return conductor.review_stats(args.attempt_log)
    if args.command == "log":
        if not 1 <= args.limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        return conductor.log(
            args.limit,
            author=args.author,
            http_principal=args.http_principal,
            event_type=args.event_type,
            since=args.since,
            until=args.until,
        )
    if args.command == "audit":
        if args.action == "verify":
            return conductor.verify_audit_signatures(args.limit)
        if args.action == "verify-history":
            return (
                verify_all_history_pages(
                    lambda offset, head: conductor.verify_audit_history(
                        args.limit, offset, head or args.expected_head
                    ),
                    args.limit,
                )
                if args.all
                else conductor.verify_audit_history(args.limit, args.offset, args.expected_head)
            )
        return conductor.verify_current_snapshot()
    raise ValueError(f"Unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = _run(args)
        rendered = None
        if result is not None:
            rendered = (
                _format_inspection(result)
                if args.command in ("task", "reviewer")
                and args.action == "inspect"
                and args.format == "text"
                else json.dumps(result, ensure_ascii=False, indent=2)
            )
    except (ValueError, KeyError, OSError, RuntimeError) as exc:
        message = str(exc.args[0]) if isinstance(exc, KeyError) else str(exc)
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    if rendered is not None:
        print(rendered)
    if (args.command == "audit" and args.action == "verify") or (
        args.command == "reviewer" and args.action == "audit-verify"
    ):
        return int(
            result["invalid"] > 0
            or (args.require_signatures and not result["all_inspected_signed_and_valid"])
        )
    if (args.command == "audit" and args.action == "verify-snapshot") or (
        args.command == "reviewer" and args.action == "audit-snapshot"
    ):
        return int(not result["ok"])
    if (args.command == "audit" and args.action == "verify-history") or (
        args.command == "reviewer" and args.action == "audit-history"
    ):
        return int(not result["ok"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
