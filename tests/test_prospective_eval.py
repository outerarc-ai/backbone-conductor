"""Prospective evaluation must keep predictions and independent labels separate."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backbone_conductor.ledger import create_ledger
from backbone_conductor.service import Conductor

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "evaluate_prospective.py"
sys.path.insert(0, str(ROOT / "scripts"))

import evaluate_prospective as study  # noqa: E402
from evaluate_prospective import (  # noqa: E402
    capture,
    freeze,
    freeze_semantic,
    main,
    prepare_review,
    score,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _capture_repo(tmp_path: Path, *, separate_ledger: bool = False) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Study")
    _git(repo, "config", "user.email", "study@example.invalid")
    (repo / "app.py").write_text("value = 1\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "Initial code")
    if separate_ledger:
        create_ledger(repo)
        conductor = Conductor(repo, ledger_branch="backbone")
    else:
        conductor = Conductor(repo)
        conductor.initialize()
    for name, path in (
        ("alice", "src/shared.py"),
        ("bob", "src/shared.py"),
        ("carol", "src/other.py"),
    ):
        conductor.create_intent(
            {
                "id": f"intent-{name}",
                "author": name,
                "problem": f"Plan {name}'s work",
                "proposed_outcome": "Complete the planned change",
                "affected_paths": [path],
            }
        )
    return repo


def test_capture_freezes_all_unassigned_pairs_from_real_git_baseline(tmp_path: Path):
    repo = _capture_repo(tmp_path)
    dataset = tmp_path / "cases.json"
    predictions = tmp_path / "predictions.json"
    baseline = _git(repo, "rev-parse", "HEAD")
    command = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "capture",
            "--repo",
            str(repo),
            "--project",
            "example/project",
            "--sampling",
            "All unassigned pairs at this snapshot",
            "--dataset",
            str(dataset),
            "--predictions",
            str(predictions),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert command.returncode == 0, command.stderr
    result = json.loads(command.stdout)
    assert result["case_count"] == 3
    assert result["base_sha"] == baseline
    assert result["backbone_version"] == Conductor(repo).state()["version"]
    data = json.loads(dataset.read_text())
    assert len(data["cases"]) == 3
    assert {case["base_sha"] for case in data["cases"]} == {baseline}
    assert all(len({item["author"] for item in case["intents"]}) == 2 for case in data["cases"])
    frozen = json.loads(predictions.read_text())
    assert sum(bool(case["findings"]) for case in frozen["cases"]) == 1
    assert dataset.stat().st_mode & 0o777 == 0o600
    assert predictions.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        capture(repo, "example/project", "Same snapshot", dataset, predictions)
    assert _git(repo, "status", "--porcelain") == ""


def test_capture_rejects_dirty_or_public_checkout_outputs(tmp_path: Path):
    repo = _capture_repo(tmp_path)
    dataset = tmp_path / "cases.json"
    predictions = tmp_path / "predictions.json"
    with pytest.raises(ValueError, match="outside the public code repository"):
        capture(repo, "example/project", "All pairs", repo / "cases.json", predictions)
    (repo / "untracked.txt").write_text("unfinished work\n")
    with pytest.raises(ValueError, match="clean"):
        capture(repo, "example/project", "All pairs", dataset, predictions)
    assert not dataset.exists() and not predictions.exists()


def test_capture_excludes_reopened_intents_with_task_history(tmp_path: Path):
    repo = _capture_repo(tmp_path)
    conductor = Conductor(repo)
    conductor.transition_intent("intent-carol", "accepted")
    task = conductor.dispatch_task("intent-carol", "carol")
    conductor.cancel_task(task["id"], "coordinator", "Replan before implementation")
    assert conductor.state()["intents"]["intent-carol"]["status"] == "accepted"
    dataset = tmp_path / "cases.json"
    capture(repo, "example/project", "All eligible pairs", dataset, tmp_path / "predictions.json")
    assert [case["id"] for case in json.loads(dataset.read_text())["cases"]] == [
        "pair-intent-alice-intent-bob"
    ]


def test_capture_separate_ledger_records_code_baseline(tmp_path: Path):
    repo = _capture_repo(tmp_path, separate_ledger=True)
    code_sha = _git(repo, "rev-parse", "HEAD")
    result = capture(
        repo,
        "example/project",
        "All unassigned pairs in the separate ledger",
        tmp_path / "cases.json",
        tmp_path / "predictions.json",
        ledger_branch="backbone",
    )
    assert result["base_sha"] == code_sha
    assert result["backbone_version"] != code_sha
    assert _git(repo, "status", "--porcelain") == ""


@pytest.mark.parametrize("mutation", ["code_commit", "dirty_code", "ledger"])
def test_capture_rechecks_snapshot_after_freezing_predictions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
):
    separate_ledger = mutation == "ledger"
    repo = _capture_repo(tmp_path, separate_ledger=separate_ledger)
    dataset = tmp_path / "cases.json"
    predictions = tmp_path / "predictions.json"
    original_freeze = study._freeze_and_identity

    def freeze_then_change(dataset_path: Path, predictions_path: Path):
        result = original_freeze(dataset_path, predictions_path)
        if mutation == "code_commit":
            (repo / "app.py").write_text("value = 2\n")
            _git(repo, "add", "app.py")
            _git(repo, "commit", "-m", "Concurrent code change")
        elif mutation == "dirty_code":
            (repo / "app.py").write_text("value = 2\n")
        else:
            Conductor(repo, ledger_branch="backbone").create_intent(
                {
                    "id": "intent-later",
                    "author": "dave",
                    "problem": "Concurrent plan",
                    "proposed_outcome": "New work",
                }
            )
        return result

    monkeypatch.setattr(study, "_freeze_and_identity", freeze_then_change)
    expected = {
        "code_commit": "code HEAD changed",
        "dirty_code": "code worktree changed",
        "ledger": "Backbone state changed",
    }[mutation]
    with pytest.raises(ValueError, match=expected):
        capture(
            repo,
            "example/project",
            "All unassigned pairs",
            dataset,
            predictions,
            ledger_branch="backbone" if separate_ledger else None,
        )
    assert not dataset.exists()
    assert not predictions.exists()


def test_capture_failure_preserves_prediction_file_created_by_another_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    repo = _capture_repo(tmp_path)
    dataset = tmp_path / "cases.json"
    predictions = tmp_path / "predictions.json"
    original_freeze = study._freeze_and_identity

    def freeze_after_other_writer(dataset_path: Path, predictions_path: Path):
        predictions_path.write_text("other writer\n")
        return original_freeze(dataset_path, predictions_path)

    monkeypatch.setattr(study, "_freeze_and_identity", freeze_after_other_writer)
    with pytest.raises(FileExistsError):
        capture(repo, "example/project", "All pairs", dataset, predictions)
    assert not dataset.exists()
    assert predictions.read_text() == "other writer\n"


@pytest.mark.parametrize("changed_file", ["dataset", "predictions"])
def test_capture_rejects_evidence_changed_after_freeze(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_file: str
):
    repo = _capture_repo(tmp_path)
    dataset = tmp_path / "cases.json"
    predictions = tmp_path / "predictions.json"
    original_freeze = study._freeze_and_identity

    def freeze_then_change(dataset_path: Path, predictions_path: Path):
        result = original_freeze(dataset_path, predictions_path)
        path = dataset_path if changed_file == "dataset" else predictions_path
        path.write_text(path.read_text() + " ")
        return result

    monkeypatch.setattr(study, "_freeze_and_identity", freeze_then_change)
    expected = (
        "dataset changed after deterministic predictions"
        if changed_file == "dataset"
        else "deterministic predictions changed during capture"
    )
    with pytest.raises(ValueError, match=expected):
        capture(repo, "example/project", "All pairs", dataset, predictions)
    assert not dataset.exists()
    assert not predictions.exists()


@pytest.mark.parametrize("changed_file", ["dataset", "predictions"])
@pytest.mark.parametrize("same_bytes", [False, True])
def test_capture_preserves_evidence_replaced_by_another_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_file: str, same_bytes: bool
):
    repo = _capture_repo(tmp_path)
    dataset, predictions = tmp_path / "cases.json", tmp_path / "predictions.json"
    original_freeze = study._freeze_and_identity
    replacement: bytes | None = None

    def freeze_then_replace(dataset_path: Path, predictions_path: Path):
        nonlocal replacement
        result = original_freeze(dataset_path, predictions_path)
        path = dataset_path if changed_file == "dataset" else predictions_path
        replacement = path.read_bytes() if same_bytes else b"other writer\n"
        path.unlink()
        path.write_bytes(replacement)
        return result

    monkeypatch.setattr(study, "_freeze_and_identity", freeze_then_replace)
    with pytest.raises(ValueError, match="changed"):
        capture(repo, "example/project", "All pairs", dataset, predictions)
    replaced = dataset if changed_file == "dataset" else predictions
    removed = predictions if changed_file == "dataset" else dataset
    assert replacement is not None
    assert replaced.read_bytes() == replacement
    assert not removed.exists()


def test_freeze_rejects_dataset_mutation_during_rule_detection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    dataset = tmp_path / "cases.json"
    predictions = tmp_path / "predictions.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sampling": "All prospective pairs",
                "cases": [_case("pair-one", "a.py", "a.py")],
            }
        )
    )
    original_detect = study.detect_conflicts

    def detect_after_dataset_change(state):
        dataset.write_text(dataset.read_text() + " ")
        return original_detect(state)

    monkeypatch.setattr(study, "detect_conflicts", detect_after_dataset_change)
    with pytest.raises(ValueError, match="dataset changed while deterministic predictions"):
        freeze(dataset, predictions)
    assert not predictions.exists()


def test_freeze_rejects_dataset_change_during_publication(tmp_path: Path, monkeypatch):
    dataset, predictions = tmp_path / "cases.json", tmp_path / "predictions.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sampling": "All prospective pairs",
                "cases": [_case("pair-one", "a.py", "a.py")],
            }
        )
    )
    original_write = study._write_exclusive

    def write_then_change(path: Path, value: dict):
        identity = original_write(path, value)
        dataset.write_text(dataset.read_text() + " ")
        return identity

    monkeypatch.setattr(study, "_write_exclusive", write_then_change)
    with pytest.raises(ValueError, match="dataset changed while deterministic predictions"):
        freeze(dataset, predictions)
    assert not predictions.exists()


def _case(identifier: str, left_path: str, right_path: str) -> dict:
    return {
        "id": identifier,
        "project": "example/project",
        "base_sha": "a" * 40,
        "captured_at": "2025-01-01T10:00:00Z",
        "intents": [
            {
                "id": f"{identifier}-alice",
                "author": "alice",
                "problem": "Change the first component",
                "proposed_outcome": "Complete planned work",
                "affected_paths": [left_path],
                "created_at": "2025-01-01T09:00:00Z",
            },
            {
                "id": f"{identifier}-bob",
                "author": "bob",
                "problem": "Change the second component",
                "proposed_outcome": "Complete planned work",
                "affected_paths": [right_path],
                "created_at": "2025-01-01T09:30:00Z",
            },
        ],
    }


def _files(tmp_path: Path, cases: list[dict]) -> tuple[Path, Path]:
    dataset = tmp_path / "cases.json"
    predictions = tmp_path / "predictions.json"
    dataset.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sampling": "Consecutive eligible task pairs before work starts",
                "cases": cases,
            }
        ),
        encoding="utf-8",
    )
    freeze(dataset, predictions)
    return dataset, predictions


def _reviews(
    path: Path,
    dataset: Path,
    predictions: Path,
    reviewer: str,
    cases: list[dict],
    semantic: Path | None = None,
) -> None:
    content = {
        "schema_version": 1,
        "dataset_sha256": hashlib.sha256(dataset.read_bytes()).hexdigest(),
        "predictions_sha256": hashlib.sha256(predictions.read_bytes()).hexdigest(),
        "reviewer": reviewer,
        "cases": cases,
    }
    if semantic is not None:
        content["semantic_predictions_sha256"] = hashlib.sha256(semantic.read_bytes()).hexdigest()
    path.write_text(
        json.dumps(content),
        encoding="utf-8",
    )


def _review(identifier: str, conflict: bool) -> dict:
    return {"id": identifier, "conflict": conflict, "rationale": "Reviewed both planned changes"}


def test_blind_review_packets_feed_scoring_without_exposing_predictions(tmp_path, capsys):
    dataset, predictions = _files(
        tmp_path,
        [
            _case("overlap", "src/shared.py", "src/shared.py"),
            _case("separate", "src/one.py", "src/two.py"),
        ],
    )
    first, second = tmp_path / "carol.json", tmp_path / "dave.json"
    assert (
        main(
            [
                "prepare-review",
                "--dataset",
                str(dataset),
                "--predictions",
                str(predictions),
                "--reviewer",
                "carol",
                "--output",
                str(first),
            ]
        )
        == 0
    )
    prepared = json.loads(capsys.readouterr().out)
    assert prepared["case_count"] == 2
    assert prepared["packet_sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    prepare_review(dataset, predictions, second, "dave")
    assert first.stat().st_mode & 0o777 == second.stat().st_mode & 0o777 == 0o600
    packet = json.loads(first.read_text())
    assert packet["schema_version"] == 2
    assert "coordination of scope" in packet["label_definition"]
    assert [case["id"] for case in packet["cases"]] == ["overlap", "separate"]
    assert all(case["conflict"] is None and case["rationale"] == "" for case in packet["cases"])
    content = first.read_text()
    assert '"findings"' not in content
    assert '"verdict"' not in content
    assert '"prediction":' not in content
    assert score(dataset, predictions, first, second)["status"] == "incomplete"

    partial = json.loads(first.read_text())
    partial["cases"][0]["conflict"] = True
    partial["cases"][0]["rationale"] = "The original plans needed a shared interface decision"
    first.write_text(json.dumps(partial), encoding="utf-8")
    incomplete = score(dataset, predictions, first, second)
    assert incomplete["status"] == "incomplete"
    assert incomplete["resolved_count"] == 0
    assert incomplete["cases"][0]["status"] == "awaiting_review"

    for path in (first, second):
        labeled = json.loads(path.read_text())
        for case in labeled["cases"]:
            case["conflict"] = case["id"] == "overlap"
            case["rationale"] = "Compared both completed changes with the original plans"
        path.write_text(json.dumps(labeled), encoding="utf-8")
    result = score(dataset, predictions, first, second)
    assert result["status"] == "complete_sample"
    assert result["counts"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 1}

    altered = json.loads(second.read_text())
    altered["cases"][0]["intents"][0]["affected_paths"] = ["other.py"]
    second.write_text(json.dumps(altered), encoding="utf-8")
    with pytest.raises(ValueError, match="review packet does not match frozen dataset"):
        score(dataset, predictions, first, second)
    with pytest.raises(FileExistsError, match="never overwrites"):
        prepare_review(dataset, predictions, first, "carol")
    with pytest.raises(ValueError, match="reviewer must differ"):
        prepare_review(dataset, predictions, tmp_path / "alice.json", "alice")


@pytest.mark.parametrize(
    "mutation",
    ["dataset", "predictions", "output_modified", "output_replaced", "output_replaced_same"],
)
def test_review_packet_publication_rechecks_inputs_and_preserves_other_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str
):
    dataset, predictions = _files(tmp_path, [_case("pair", "a.py", "a.py")])
    output = tmp_path / "carol.json"
    original_write = study._write_exclusive

    def write_then_change(path: Path, value: dict):
        identity = original_write(path, value)
        if path == output:
            if mutation in {"output_replaced", "output_replaced_same"}:
                replacement = (
                    output.read_bytes() if mutation == "output_replaced_same" else b"other writer\n"
                )
                output.unlink()
                output.write_bytes(replacement)
            elif mutation == "output_modified":
                output.write_text("tampered\n")
            else:
                changed = dataset if mutation == "dataset" else predictions
                changed.write_text(changed.read_text() + " ")
        return identity

    monkeypatch.setattr(study, "_write_exclusive", write_then_change)
    expected = (
        "blind review packet changed during publication"
        if mutation in {"output_modified", "output_replaced", "output_replaced_same"}
        else "study inputs changed while blind review was publishing"
    )
    with pytest.raises(ValueError, match=expected):
        prepare_review(dataset, predictions, output, "carol")
    if mutation == "output_replaced":
        assert output.read_text() == "other writer\n"
    elif mutation == "output_replaced_same":
        assert output.read_bytes() == study._serialized(json.loads(output.read_text()))
    else:
        assert not output.exists()


def test_prospective_score_exposes_false_positives_and_missed_semantic_conflicts(tmp_path):
    dataset, predictions = _files(
        tmp_path,
        [
            _case("true-positive", "src/shared.py", "src/shared.py"),
            _case("false-positive", "src/shared.py", "src/shared.py"),
            _case("false-negative", "src/one.py", "src/two.py"),
            _case("true-negative", "src/one.py", "src/two.py"),
        ],
    )
    first = tmp_path / "carol.json"
    second = tmp_path / "dave.json"
    cases = [
        _review(identifier, conflict)
        for identifier, conflict in (
            ("true-positive", True),
            ("false-positive", False),
            ("false-negative", True),
            ("true-negative", False),
        )
    ]
    _reviews(first, dataset, predictions, "carol", cases)
    _reviews(second, dataset, predictions, "dave", cases)
    result = score(dataset, predictions, first, second)
    assert result["status"] == "complete_sample"
    assert result["counts"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}
    assert result["precision"] == result["recall"] == result["f1"] == 0.5
    assert result["first_review_sha256"] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert result["second_review_sha256"] == hashlib.sha256(second.read_bytes()).hexdigest()
    assert "adjudications_sha256" not in result
    assert "cannot prove" in result["limitation"]


def test_semantic_advice_is_frozen_and_scored_separately(tmp_path, monkeypatch, capsys):
    from backbone_conductor.runtime import DSHReviewer

    dataset, predictions = _files(
        tmp_path,
        [
            _case("rule-hit", "src/shared.py", "src/shared.py"),
            _case("both-alert", "src/shared.py", "src/shared.py"),
            _case("model-only", "src/one.py", "src/two.py"),
            _case("abstain", "src/one.py", "src/two.py"),
            _case("abstain-positive", "src/one.py", "src/two.py"),
        ],
    )
    semantic = tmp_path / "semantic.json"
    contexts = []

    def advise(_self, context):
        contexts.append(context)
        case_id = context["intents"][0]["id"].removesuffix("-alice")
        verdict = {
            "rule-hit": "compatible",
            "both-alert": "conflict",
            "model-only": "conflict",
            "abstain": "uncertain",
            "abstain-positive": "uncertain",
        }[case_id]
        return {
            "verdict": verdict,
            "rationale": f"Prospective advice for {case_id}",
            "evidence": [],
            "coordination": [],
            "runtime": {"finish_reason": "completed", "session_id": f"session-{case_id}"},
        }

    monkeypatch.setattr(DSHReviewer, "advise_conflict", advise)
    frozen = freeze_semantic(dataset, predictions, semantic, tmp_path / "private-dsh", "mock-model")
    assert len(frozen["cases"]) == 5
    assert semantic.stat().st_mode & 0o777 == 0o600
    assert all(
        set(context) == {"base_sha", "intents", "accepted_decisions", "deterministic_conflicts"}
        for context in contexts
    )
    assert all(context["accepted_decisions"] == [] for context in contexts)
    assert contexts[0]["deterministic_conflicts"]
    assert contexts[2]["deterministic_conflicts"] == []
    blind = prepare_review(dataset, predictions, tmp_path / "blind.json", "carol", semantic)
    assert blind["semantic_predictions_sha256"] == hashlib.sha256(semantic.read_bytes()).hexdigest()
    assert "verdict" not in (tmp_path / "blind.json").read_text()
    with pytest.raises(FileExistsError):
        freeze_semantic(dataset, predictions, semantic, tmp_path / "private-dsh", "mock-model")

    first = tmp_path / "carol.json"
    second = tmp_path / "dave.json"
    labels = [
        _review(identifier, conflict)
        for identifier, conflict in (
            ("rule-hit", True),
            ("both-alert", False),
            ("model-only", True),
            ("abstain", False),
            ("abstain-positive", True),
        )
    ]
    _reviews(first, dataset, predictions, "carol", labels, semantic)
    _reviews(second, dataset, predictions, "dave", labels, semantic)
    report = score(dataset, predictions, first, second, semantic_predictions=semantic)
    assert report["status"] == "complete_sample"
    assert report["counts"] == {"tp": 1, "fp": 1, "fn": 2, "tn": 1}
    assert report["semantic"]["counts"] == {"tp": 1, "fp": 1, "fn": 2, "tn": 1}
    assert report["semantic"]["resolved_abstentions"] == 2
    assert report["combined"]["counts"] == {"tp": 2, "fp": 1, "fn": 1, "tn": 1}
    assert report["combined"]["recall"] == 2 / 3
    assert (
        report["project_summaries"]["example/project"]["semantic"]["counts"]
        == report["semantic"]["counts"]
    )
    assert (
        report["project_summaries"]["example/project"]["combined"]["counts"]
        == report["combined"]["counts"]
    )
    assert "not an independent model-only detector" in report["limitation"]
    second_blind = tmp_path / "dave-blind.json"
    prepare_review(dataset, predictions, second_blind, "dave", semantic)
    truth = {item["id"]: item["conflict"] for item in labels}
    for path in (tmp_path / "blind.json", second_blind):
        packet = json.loads(path.read_text())
        for case in packet["cases"]:
            case["conflict"] = truth[case["id"]]
            case["rationale"] = "Compared the original plans and completed artifacts"
        path.write_text(json.dumps(packet), encoding="utf-8")
    blind_report = score(
        dataset,
        predictions,
        tmp_path / "blind.json",
        second_blind,
        semantic_predictions=semantic,
    )
    assert blind_report["combined"]["counts"] == report["combined"]["counts"]
    assert (
        main(
            [
                "score",
                "--dataset",
                str(dataset),
                "--predictions",
                str(predictions),
                "--semantic-predictions",
                str(semantic),
                "--review",
                str(first),
                "--review",
                str(second),
                "--json",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["combined"]["counts"] == report["combined"]["counts"]

    tampered = json.loads(semantic.read_text())
    tampered["cases"][0]["context_sha256"] = "0" * 64
    semantic.write_text(json.dumps(tampered))
    _reviews(first, dataset, predictions, "carol", labels, semantic)
    _reviews(second, dataset, predictions, "dave", labels, semantic)
    with pytest.raises(ValueError, match="semantic context does not match"):
        score(dataset, predictions, first, second, semantic_predictions=semantic)


def test_semantic_freeze_failure_leaves_no_prediction_artifact(tmp_path, monkeypatch):
    from backbone_conductor.runtime import DSHReviewer

    dataset, predictions = _files(tmp_path, [_case("pair", "src/one.py", "src/two.py")])
    semantic = tmp_path / "semantic.json"

    def change_input(_self, _context):
        dataset.write_text(dataset.read_text() + "\n")
        return {
            "verdict": "uncertain",
            "rationale": "Input changed while running",
            "runtime": {"finish_reason": "completed"},
        }

    monkeypatch.setattr(DSHReviewer, "advise_conflict", change_input)
    with pytest.raises(ValueError, match="inputs changed"):
        freeze_semantic(dataset, predictions, semantic, tmp_path / "private-dsh", "mock-model")
    assert not semantic.exists()


def test_semantic_freeze_rechecks_inputs_after_publication(tmp_path, monkeypatch):
    from backbone_conductor.runtime import DSHReviewer

    dataset, predictions = _files(tmp_path, [_case("pair", "a.py", "a.py")])
    semantic = tmp_path / "semantic.json"

    def advise(_self, _context):
        return {
            "verdict": "uncertain",
            "rationale": "The original plans need human review",
            "evidence": [],
            "coordination": [],
            "runtime": {"finish_reason": "completed"},
        }

    monkeypatch.setattr(DSHReviewer, "advise_conflict", advise)
    original_write = study._write_exclusive

    def write_then_change(path: Path, value: dict):
        identity = original_write(path, value)
        if path == semantic:
            predictions.write_text(predictions.read_text() + " ")
        return identity

    monkeypatch.setattr(study, "_write_exclusive", write_then_change)
    with pytest.raises(
        ValueError, match="study inputs changed while semantic advice was publishing"
    ):
        freeze_semantic(dataset, predictions, semantic, tmp_path / "private-dsh", "mock-model")
    assert not semantic.exists()


def test_installed_sdk_freezes_semantic_study_with_local_mock_provider(
    tmp_path, monkeypatch, mock_dsh_tool_provider, capsys
):
    if os.environ.get("BACKBONE_REQUIRE_DSH_MCP") != "1":
        pytest.skip("set BACKBONE_REQUIRE_DSH_MCP=1 for installed SDK study check")
    pytest.importorskip("deepseek_harness")
    dataset, predictions = _files(tmp_path, [_case("pair", "src/one.py", "src/two.py")])
    semantic = tmp_path / "semantic.json"
    response = json.dumps(
        {
            "verdict": "conflict",
            "rationale": "The planned APIs may be incompatible",
            "evidence": ["Different return contracts"],
            "coordination": ["Agree one contract"],
        }
    )
    with mock_dsh_tool_provider(None, {}, response) as (provider_url, requests):
        monkeypatch.setenv("DEEPSEEK_BASE_URL", provider_url)
        monkeypatch.setenv("DEEPSEEK_API_KEY", "local-study-mock-key")
        assert (
            main(
                [
                    "freeze-semantic",
                    "--dataset",
                    str(dataset),
                    "--predictions",
                    str(predictions),
                    "--output",
                    str(semantic),
                    "--dsh-home",
                    str(tmp_path / "private-dsh"),
                    "--model",
                    "mock-model",
                ]
            )
            == 0
        )
    assert "Frozen 1 semantic cases" in capsys.readouterr().out
    assert len(requests) == 1
    assert "pair-alice" in json.dumps(requests[0]["messages"])
    frozen = json.loads(semantic.read_text())
    assert frozen["cases"][0]["advice"]["verdict"] == "conflict"
    assert frozen["cases"][0]["advice"]["runtime"]["finish_reason"] == "completed"


def test_missing_and_disputed_labels_do_not_become_negative_cases(tmp_path):
    dataset, predictions = _files(
        tmp_path,
        [
            _case("disputed", "src/shared.py", "src/shared.py"),
            _case("unlabeled", "src/one.py", "src/two.py"),
        ],
    )
    first = tmp_path / "carol.json"
    second = tmp_path / "dave.json"
    adjudication = tmp_path / "erin.json"
    _reviews(first, dataset, predictions, "carol", [_review("disputed", True)])
    _reviews(second, dataset, predictions, "dave", [_review("disputed", False)])
    result = score(dataset, predictions, first, second)
    assert result["status"] == "incomplete"
    assert result["resolved_count"] == 0
    assert result["recall"] is None
    assert [row["status"] for row in result["cases"]] == ["disputed", "unlabeled"]
    assert result["labeling"]["disagreed_count"] == 1
    assert result["labeling"]["unresolved_disagreement_count"] == 1

    _reviews(adjudication, dataset, predictions, "erin", [_review("disputed", True)])
    result = score(dataset, predictions, first, second, adjudication)
    assert result["resolved_count"] == 1
    assert result["counts"]["tp"] == 1
    assert result["status"] == "incomplete"
    assert result["adjudications_sha256"] == hashlib.sha256(adjudication.read_bytes()).hexdigest()
    assert result["labeling"]["adjudicated_count"] == 1
    assert result["labeling"]["unresolved_disagreement_count"] == 0


def test_score_reports_review_agreement_and_project_coverage(tmp_path: Path):
    cases = [
        _case("positive", "shared.py", "shared.py"),
        _case("negative", "one.py", "two.py"),
        _case("dispute", "shared.py", "shared.py"),
        _case("awaiting", "one.py", "two.py"),
    ]
    for case in cases[:2]:
        case["project"] = "example/alpha"
    for case in cases[2:]:
        case["project"] = "example/beta"
    dataset, predictions = _files(tmp_path, cases)
    first, second, adjudication = (
        tmp_path / "carol.json",
        tmp_path / "dave.json",
        tmp_path / "erin.json",
    )
    _reviews(
        first,
        dataset,
        predictions,
        "carol",
        [
            _review("positive", True),
            _review("negative", False),
            _review("dispute", False),
            _review("awaiting", True),
        ],
    )
    _reviews(
        second,
        dataset,
        predictions,
        "dave",
        [_review("positive", True), _review("negative", False), _review("dispute", True)],
    )
    _reviews(adjudication, dataset, predictions, "erin", [_review("dispute", False)])

    report = score(dataset, predictions, first, second, adjudication)
    assert report["status"] == "incomplete"
    assert report["resolved_count"] == 3
    assert report["labeling"] == {
        "first_labeled_count": 4,
        "second_labeled_count": 3,
        "both_labeled_count": 3,
        "agreed_count": 2,
        "disagreed_count": 1,
        "adjudicated_count": 1,
        "unresolved_disagreement_count": 0,
        "observed_agreement": 2 / 3,
    }
    assert report["project_summaries"]["example/alpha"]["sample_count"] == 2
    assert report["project_summaries"]["example/alpha"]["resolved_count"] == 2
    assert report["project_summaries"]["example/alpha"]["deterministic"]["counts"] == {
        "tp": 1,
        "fp": 0,
        "fn": 0,
        "tn": 1,
    }
    assert report["project_summaries"]["example/beta"]["sample_count"] == 2
    assert report["project_summaries"]["example/beta"]["resolved_count"] == 1
    assert report["project_summaries"]["example/beta"]["deterministic"]["counts"] == {
        "tp": 0,
        "fp": 1,
        "fn": 0,
        "tn": 0,
    }
    assert report["project_summaries"]["example/beta"]["deterministic"]["recall"] is None


@pytest.mark.parametrize("changed_review", ["first", "second", "adjudication"])
def test_scoring_rejects_review_file_changed_after_labels_were_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed_review: str
):
    dataset, predictions = _files(tmp_path, [_case("disputed", "src/shared.py", "src/shared.py")])
    first, second, adjudication = (
        tmp_path / "carol.json",
        tmp_path / "dave.json",
        tmp_path / "erin.json",
    )
    _reviews(first, dataset, predictions, "carol", [_review("disputed", True)])
    _reviews(second, dataset, predictions, "dave", [_review("disputed", False)])
    _reviews(adjudication, dataset, predictions, "erin", [_review("disputed", True)])
    changed_path = {"first": first, "second": second, "adjudication": adjudication}[changed_review]
    original_metrics = study._metrics
    changed = False

    def metrics_after_review_change(counts):
        nonlocal changed
        if not changed:
            changed_labels = json.loads(changed_path.read_text(encoding="utf-8"))
            changed_labels["cases"][0]["conflict"] = not changed_labels["cases"][0]["conflict"]
            changed_path.write_text(json.dumps(changed_labels), encoding="utf-8")
            changed = True
        return original_metrics(counts)

    monkeypatch.setattr(study, "_metrics", metrics_after_review_change)
    with pytest.raises(ValueError, match="review labels changed during scoring"):
        score(dataset, predictions, first, second, adjudication)


def test_freeze_is_exclusive_and_labels_bind_exact_inputs(tmp_path):
    dataset, predictions = _files(tmp_path, [_case("pair", "src/shared.py", "src/shared.py")])
    with pytest.raises(FileExistsError):
        freeze(dataset, predictions)
    first = tmp_path / "carol.json"
    second = tmp_path / "dave.json"
    _reviews(first, dataset, predictions, "carol", [_review("pair", True)])
    _reviews(second, dataset, predictions, "dave", [_review("pair", True)])
    contents = json.loads(dataset.read_text())
    contents["sampling"] = "Changed after predictions were frozen"
    dataset.write_text(json.dumps(contents))
    with pytest.raises(ValueError, match="frozen dataset"):
        score(dataset, predictions, first, second)


def test_self_review_and_prediction_tampering_are_rejected(tmp_path):
    dataset, predictions = _files(tmp_path, [_case("pair", "src/shared.py", "src/shared.py")])
    first = tmp_path / "alice.json"
    second = tmp_path / "dave.json"
    _reviews(first, dataset, predictions, "alice", [_review("pair", True)])
    _reviews(second, dataset, predictions, "dave", [_review("pair", True)])
    with pytest.raises(ValueError, match="reviewer must differ"):
        score(dataset, predictions, first, second)

    _reviews(first, dataset, predictions, "carol", [_review("pair", True)])
    frozen = json.loads(predictions.read_text())
    frozen["cases"][0]["findings"] = []
    predictions.write_text(json.dumps(frozen))
    with pytest.raises(ValueError, match="review does not match"):
        score(dataset, predictions, first, second)


def test_cli_reports_incomplete_sample_without_claiming_target(tmp_path, capsys):
    dataset, predictions = _files(tmp_path, [_case("pair", "src/one.py", "src/two.py")])
    first = tmp_path / "carol.json"
    second = tmp_path / "dave.json"
    _reviews(first, dataset, predictions, "carol", [])
    _reviews(second, dataset, predictions, "dave", [])
    result = subprocess.run(
        [
            sys.executable,
            str(RUNNER),
            "score",
            "--dataset",
            str(dataset),
            "--predictions",
            str(predictions),
            "--review",
            str(first),
            "--review",
            str(second),
            "--json",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "incomplete"
    assert report["recall"] is None
    assert report["precision"] is None
    assert report["labeling"]["observed_agreement"] is None
    assert report["project_summaries"]["example/project"]["resolved_count"] == 0
    assert (
        main(
            [
                "score",
                "--dataset",
                str(dataset),
                "--predictions",
                str(predictions),
                "--review",
                str(first),
                "--review",
                str(second),
            ]
        )
        == 0
    )
    assert "agreement n/a | unresolved disputes 0" in capsys.readouterr().out


@pytest.mark.parametrize("mutation", ["future_intent", "same_author", "post_work_status"])
def test_freeze_rejects_cases_that_are_not_two_prework_intents(tmp_path, mutation):
    case = _case("pair", "src/one.py", "src/two.py")
    if mutation == "future_intent":
        case["intents"][1]["created_at"] = "2025-01-01T11:00:00Z"
    elif mutation == "same_author":
        case["intents"][1]["author"] = "alice"
    else:
        case["intents"][1]["status"] = "in_progress"
    dataset = tmp_path / "cases.json"
    dataset.write_text(
        json.dumps({"schema_version": 1, "sampling": "Consecutive task pairs", "cases": [case]})
    )
    with pytest.raises(ValueError):
        freeze(dataset, tmp_path / "predictions.json")
