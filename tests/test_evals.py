import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "evaluate.py"
MERGE_RUNNER = ROOT / "scripts" / "evaluate_git_merges.py"
DATASET = ROOT / "evals" / "conflicts.json"


def run_evaluation(*arguments, cwd=None):
    return subprocess.run(
        [sys.executable, str(RUNNER), *arguments],
        cwd=cwd or ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def run_merge_cases(*arguments):
    return subprocess.run(
        [sys.executable, str(MERGE_RUNNER), *arguments],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )


def test_historical_git_merge_cases_expose_shared_path_false_positive():
    result = run_merge_cases("--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["sample_count"] == 4
    assert report["counts"] == {"tp": 2, "fp": 1, "fn": 0, "tn": 1}
    assert report["precision_against_textual_merge"] == pytest.approx(2 / 3)
    assert report["recall_against_textual_merge"] == 1.0
    assert not report["upstream_verified"]
    assert (
        next(row for row in report["cases"] if row["outcome"] == "fp")["id"]
        == "jsoup-shared-path-clean"
    )
    assert "not human-adjudicated intent conflicts" in report["limitation"]


def test_git_merge_source_verification_detects_mislabeled_history(tmp_path):
    repo = tmp_path / "upstream"
    repo.mkdir()

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], text=True, capture_output=True, check=True
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Eval Test")
    git("config", "user.email", "eval@example.test")
    (repo / "README.md").write_text("baseline\n")
    git("add", "README.md")
    git("commit", "-m", "baseline")
    git("switch", "-c", "left")
    (repo / "left.txt").write_text("left\n")
    git("add", "left.txt")
    git("commit", "-m", "left")
    left = git("rev-parse", "HEAD")
    git("switch", "main")
    (repo / "right.txt").write_text("right\n")
    git("add", "right.txt")
    git("commit", "-m", "right")
    right = git("rev-parse", "HEAD")
    git("merge", "--no-ff", "-m", "merge branches", "left")
    merge = git("rev-parse", "HEAD")

    case = {
        "id": "local-clean",
        "upstream": "example/project",
        "merge_commit": merge,
        "parents": [right, left],
        "source_url": f"https://github.com/example/project/commit/{merge}",
        "left_paths": ["right.txt"],
        "right_paths": ["left.txt"],
        "textual_conflict": False,
        "label_note": "Independent additions in a local test repository.",
    }
    dataset = tmp_path / "cases.json"
    payload = {
        "schema_version": 1,
        "label": "git_merge_tree_textual_conflict",
        "sampling": "Local verifier fixture, not a real-world sample.",
        "cases": [case],
    }
    dataset.write_text(json.dumps(payload))
    command = ("--dataset", str(dataset), "--verify-source", f"example/project={repo}")
    verified = run_merge_cases(*command, "--json")
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["upstream_verified"]
    assert json.loads(verified.stdout)["counts"] == {"tp": 0, "fp": 0, "fn": 0, "tn": 1}

    case["textual_conflict"] = True
    dataset.write_text(json.dumps(payload))
    rejected = run_merge_cases(*command)
    assert rejected.returncode == 2
    assert "label differs from Git merge replay" in rejected.stderr


def test_synthetic_dataset_passes_with_exact_finding_labels():
    result = run_evaluation("--json")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["scenario_count"] == report["passed_scenarios"] == 24
    assert report["true_positives"] == 13
    assert report["true_negative_scenarios"] == 11
    assert report["false_positives"] == report["false_negatives"] == 0
    assert report["precision"] == report["recall"] == report["f1"] == 1.0
    assert set(report["by_rule"]) == {
        "replace_vs_extend",
        "symbol_scope_overlap",
        "dependency_conflict",
        "resource_contention",
    }
    assert "Synthetic" in report["limitation"]
    assert "do not establish" in report["limitation"]


def test_runner_works_outside_repository_and_prints_limitations(tmp_path):
    result = run_evaluation(cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "24/24 scenarios passed" in result.stdout
    assert "Precision: 100.0% | Recall: 100.0%" in result.stdout
    assert "real-world conflict detection rates" in result.stdout


def test_bad_predictions_affect_metrics_and_fail_the_cli(tmp_path):
    dataset = json.loads(DATASET.read_text())
    # Delete a true label (the detection becomes a false positive), then add a
    # nonexistent finding to a negative scenario (a false negative).
    dataset["scenarios"][0]["expected"] = []
    dataset["scenarios"][5]["expected"] = [
        {
            "rule": "symbol_scope_overlap",
            "severity": "advisory",
            "parties": ["intent-a", "intent-b"],
        }
    ]
    path = tmp_path / "wrong-labels.json"
    path.write_text(json.dumps(dataset))
    result = run_evaluation("--json", "--dataset", str(path))
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["passed_scenarios"] == 22
    assert report["true_positives"] == 12
    assert report["false_positives"] == report["false_negatives"] == 1
    assert report["precision"] == pytest.approx(12 / 13)
    assert report["recall"] == pytest.approx(12 / 13)
    assert {failure["scenario"] for failure in report["failures"]} == {
        "shared-payment-api",
        "independent-services",
    }


@pytest.mark.parametrize("mutation", ["empty", "duplicate_id", "duplicate_label", "wrong_schema"])
def test_invalid_dataset_exits_with_usage_error(tmp_path, mutation):
    dataset = json.loads(DATASET.read_text())
    if mutation == "empty":
        dataset["scenarios"] = []
    elif mutation == "duplicate_id":
        dataset["scenarios"][1]["id"] = dataset["scenarios"][0]["id"]
    elif mutation == "duplicate_label":
        dataset["scenarios"][0]["expected"] *= 2
    elif mutation == "wrong_schema":
        dataset["schema_version"] = 2
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps(dataset))
    result = run_evaluation("--dataset", str(path))
    assert result.returncode == 2
    assert "invalid evaluation dataset" in result.stderr
