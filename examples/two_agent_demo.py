"""Exercise two planned agents, one intent conflict, and real Git integration.

Everything runs in a disposable repository. The known demo changes receive a
scripted review decision; real work still needs a person to review the code.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from backbone_conductor.service import Conductor


async def member_calls(repo: Path, calls: list[tuple[str, str, dict]]) -> list[dict]:
    """Use separate member-bound MCP processes; each phase can reconnect."""

    async def call(member: str, tool: str, arguments: dict) -> dict:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "backbone_conductor", "--repo", str(repo), "mcp", "--member", member],
            env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            available = {item.name for item in (await session.list_tools()).tools}
            if tool not in available or available & {
                "dispatch_task",
                "review_intent",
                "resolve_conflict",
                "merge_task",
            }:
                raise RuntimeError(
                    f"{member} has an unexpected MCP tool scope: {sorted(available)}"
                )
            result = await session.call_tool(tool, arguments)
            if result.isError:
                raise RuntimeError(f"{member} {tool} failed: {result.content}")
            return json.loads(result.content[0].text)

    return await asyncio.wait_for(
        asyncio.gather(*(call(member, tool, args) for member, tool, args in calls)),
        timeout=60,
    )


def run_demo(repo: Path, *, use_mcp: bool = False) -> dict:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Backbone Demo Owner")
    git("config", "user.email", "owner@example.invalid")
    git("config", "commit.gpgsign", "false")
    (repo / "engine.py").write_text("def format_value(value: str) -> str:\n    return value\n")
    (repo / "client.py").write_text(
        "from engine import format_value\n\n"
        "def present(value: str) -> str:\n    return format_value(value)\n"
    )
    git("add", "engine.py", "client.py")
    git("commit", "-m", "Initial formatter and client")

    conductor = Conductor(repo)
    conductor.initialize()
    plans = (
        {
            "id": "intent-core-format",
            "author": "agent-a",
            "problem": "The core formatter leaves values unmarked",
            "proposed_outcome": "format_value wraps values in brackets",
            "affected_symbols": ["format_value"],
            "operations": {"format_value": "replace"},
            "affected_paths": ["engine.py"],
        },
        {
            "id": "intent-client-format",
            "author": "agent-b",
            "problem": "The client cannot identify formatted output",
            "proposed_outcome": "present prefixes the core formatter result",
            "affected_symbols": ["format_value"],
            "operations": {"format_value": "extend"},
            "affected_paths": ["client.py"],
        },
    )
    if use_mcp:
        created = asyncio.run(
            member_calls(
                repo,
                [
                    (
                        plan["author"],
                        "create_intent",
                        {
                            "intent_data": {
                                key: value for key, value in plan.items() if key != "author"
                            }
                        },
                    )
                    for plan in plans
                ],
            )
        )
        if any(item["author"] != plan["author"] for plan, item in zip(plans, created, strict=True)):
            raise RuntimeError("MCP member binding did not match the plans")
    else:
        for plan in plans:
            conductor.create_intent(plan)
    for plan in plans:
        conductor.review_intent(
            plan["id"],
            "accepted",
            "demo-owner",
            "Fixed demo plan has a known, bounded code change",
            conductor.state()["version"],
        )

    conflicts = list(conductor.state()["conflicts"].values())
    blocking = [item for item in conflicts if item["severity"] != "advisory"]
    if len(blocking) != 1 or blocking[0]["rule"] != "replace_vs_extend":
        raise RuntimeError(f"Expected one replace-vs-extend conflict: {conflicts}")
    conflict = blocking[0]
    resolution = conductor.resolve_conflict(
        conflict["id"],
        "demo-owner",
        "coordinate",
        "Fix the core output first, then let the client prefix its result",
        conductor.state()["version"],
    )

    tasks = {}
    for plan in plans:
        agent = plan["author"]
        task = conductor.dispatch_task(plan["id"], agent)
        tasks[agent] = task
    if use_mcp:
        started = asyncio.run(
            member_calls(
                repo,
                [(agent, "start_task", {"task_id": tasks[agent]["id"]}) for agent in tasks],
            )
        )
        if any(item["status"] != "in_progress" for item in started):
            raise RuntimeError("An MCP member task did not start")
    else:
        for agent, task in tasks.items():
            conductor.start_task(task["id"], agent)
    base = git("rev-parse", "HEAD")

    changes = (
        (
            "agent-a",
            "feature/core-format",
            "engine.py",
            'def format_value(value: str) -> str:\n    return f"[{value}]"\n',
        ),
        (
            "agent-b",
            "feature/client-format",
            "client.py",
            "from engine import format_value\n\n"
            "def present(value: str) -> str:\n"
            '    return f"result={format_value(value)}"\n',
        ),
    )
    branches = {}
    for agent, branch, path, content in changes:
        git("switch", "-c", branch, base)
        (repo / path).write_text(content)
        git("add", path)
        git(
            "-c",
            f"user.name={agent}",
            "-c",
            f"user.email={agent}@example.invalid",
            "commit",
            "-m",
            f"Implement {agent} formatter plan",
        )
        branches[agent] = {"name": branch, "sha": git("rev-parse", "HEAD")}
        git("switch", "main")

    artifact_payloads = {
        plan["author"]: {
            "intent_id": plan["id"],
            "branch": branches[plan["author"]]["name"],
            "commit_sha": branches[plan["author"]]["sha"],
            "base_ref": "main",
            "summary": f"Implement {plan['id']}",
        }
        for plan in plans
    }
    if use_mcp:
        results = asyncio.run(
            member_calls(
                repo,
                [
                    (agent, "submit_artifact", {"artifact": artifact_payloads[agent]})
                    for agent in artifact_payloads
                ],
            )
        )
        submitted = dict(zip(artifact_payloads, results, strict=True))
    else:
        submitted = {
            agent: conductor.submit_artifact(agent, payload)
            for agent, payload in artifact_payloads.items()
        }
    for result in submitted.values():
        if not result["accepted"] or not result["requires_human_review"]:
            raise RuntimeError(f"Artifact was not ready for code review: {result}")

    decision_change_rejected = False
    for plan in plans:
        agent = plan["author"]
        git("merge", "--no-ff", "--no-edit", branches[agent]["name"])
        packet = conductor.inspect_task(tasks[agent]["id"])
        if not packet["git"]["integrated_into_target"]:
            raise RuntimeError(f"Artifact is not integrated: {agent}")
        if agent == "agent-b":
            try:
                conductor.merge_task(
                    tasks[agent]["id"],
                    "demo-owner",
                    expected_version=packet["version"],
                    expected_target_sha=packet["git"]["target_sha"],
                )
            except ValueError as exc:
                if "Decisions changed after submission" not in str(exc):
                    raise
                decision_change_rejected = True
            else:
                raise RuntimeError("Missing review rationale was unexpectedly accepted")
        conductor.merge_task(
            tasks[agent]["id"],
            "demo-owner",
            rationale=(
                "Reviewed agent-a's integrated core formatter; agent-b only prefixes its result"
                if agent == "agent-b"
                else None
            ),
            expected_version=packet["version"],
            expected_target_sha=packet["git"]["target_sha"],
        )

    code = subprocess.run(
        [sys.executable, "-B", "-c", "from client import present; print(present('x'))"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    snapshot = conductor.verify_current_snapshot()
    history = conductor.verify_audit_history(limit=1000)
    state = conductor.state()
    final_checks = {
        "code": code == "result=[x]",
        "tasks": all(state["tasks"][task["id"]]["status"] == "merged" for task in tasks.values()),
        "snapshot": snapshot["ok"],
        "history": history["ok"],
        "decision_change_rejected": decision_change_rejected,
        "clean": not git("status", "--porcelain"),
    }
    if not all(final_checks.values()):
        raise RuntimeError(
            f"Integrated demo state failed final verification: {final_checks}; "
            f"invalid_history={[item for item in history['commits'] if not item['ok']]}; "
            f"status={git('status', '--porcelain')}"
        )
    return {
        "conflict": {
            "rule": conflict["rule"],
            "severity": conflict["severity"],
            "resolved": resolution["conflict"]["resolved"],
            "decision_id": resolution["decision"]["id"],
        },
        "agents": {
            agent: {
                "intent": state["intents"][plan["id"]]["status"],
                "task": state["tasks"][tasks[agent]["id"]]["status"],
                "artifact_sha": branches[agent]["sha"],
                "checks": submitted[agent]["checks"],
            }
            for plan in plans
            for agent in (plan["author"],)
        },
        "integrated_result": code,
        "member_transport": "stdio MCP" if use_mcp else "direct Conductor API",
        "decision_change_without_rationale_rejected": decision_change_rejected,
        "audit": {"snapshot_ok": snapshot["ok"], "history_ok": history["ok"]},
        "review_note": (
            "Agent identities and approval are scripted for fixed demo changes; "
            "real work requires human code review."
        ),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mcp", action="store_true", help="Use two member-bound MCP stdio clients")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="backbone-two-agent-demo-") as directory:
        print(json.dumps(run_demo(Path(directory), use_mcp=args.mcp), ensure_ascii=False, indent=2))
