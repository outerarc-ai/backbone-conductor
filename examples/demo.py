"""Run the entire coordination flow in a disposable repository, without a model."""

import json
import subprocess
import tempfile
from pathlib import Path

from backbone_conductor.service import Conductor


def run_demo(repo: Path) -> dict:
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
        )

    git("init", "-b", "main")
    git("config", "user.name", "Backbone Demo")
    git("config", "user.email", "demo@example.invalid")
    (repo / "README.md").write_text("# Disposable Backbone demonstration\n")
    git("add", "README.md")
    git("commit", "-m", "Initial code")
    conductor = Conductor(repo)
    conductor.initialize()
    intent = conductor.create_intent(
        {
            "author": "owner",
            "problem": "The package cannot greet users",
            "proposed_outcome": "A deterministic hello(name) function",
            "affected_symbols": ["hello"],
            "operations": {"hello": "add"},
            "affected_paths": ["greeting.py"],
            "constraints": ["No external dependencies"],
        }
    )
    conductor.transition_intent(intent["id"], "accepted")
    task = conductor.dispatch_task(intent["id"], "alice")
    conductor.start_task(task["id"], "alice")
    git("switch", "-c", "feature/greeting")
    (repo / "greeting.py").write_text(
        'def hello(name: str) -> str:\n    return f"Hello, {name}!"\n'
    )
    git("add", "greeting.py")
    git("commit", "-m", "Implement greeting")
    git("switch", "main")
    submission = conductor.submit_artifact(
        "alice",
        {
            "intent_id": intent["id"],
            "branch": "feature/greeting",
            "summary": "Add the greeting function",
            "base_ref": "main",
        },
    )
    if not submission["accepted"]:
        raise RuntimeError(submission)
    # This example performs the human's review step for its fixed, known artifact.
    git("merge", "--no-edit", "feature/greeting")
    inspection = conductor.inspect_task(task["id"])
    approval = conductor.merge_task(
        task["id"],
        "demo-reviewer",
        expected_version=inspection["version"],
        expected_target_sha=inspection["git"]["target_sha"],
    )
    return {
        "intent": conductor.state()["intents"][intent["id"]]["status"],
        "task": approval["task"]["status"],
        "checks": submission["checks"],
        "audit_commits": len(conductor.log()),
        "version": conductor.state()["version"],
    }


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="backbone-demo-") as directory:
        print(json.dumps(run_demo(Path(directory)), ensure_ascii=False, indent=2))
