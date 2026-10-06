#!/usr/bin/env python3
"""Freeze pre-work conflict warnings, then score separately reviewed outcomes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from itertools import combinations
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from backbone_conductor.conflicts import detect_conflicts  # noqa: E402
from backbone_conductor.models import BackboneState, Intent, IntentStatus  # noqa: E402
from backbone_conductor.runtime import DSHReviewer, SemanticConflictAdvice  # noqa: E402
from backbone_conductor.service import Conductor  # noqa: E402

SHA = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
ADVICE_FIELDS = {
    "id",
    "author",
    "status",
    "problem",
    "proposed_outcome",
    "constraints",
    "affected_symbols",
    "operations",
    "affected_paths",
}
BLIND_REVIEW_FIELDS = {
    "id",
    "author",
    "created_at",
    "problem",
    "proposed_outcome",
    "constraints",
    "affected_symbols",
    "operations",
    "affected_paths",
}
BLIND_LABEL_DEFINITION = (
    "Mark conflict true only when the two original plans, if implemented in parallel, "
    "required coordination of scope, sequencing, or design before integration. "
    "A textual Git merge conflict alone is not the label."
)
LIMITATION = (
    "Scores cover only independently reviewed, resolved cases in this submitted sample. "
    "Only pre-work intent-pair warnings are scored; decision and task rules are excluded. "
    "Names and timestamps are self-reported; this tool cannot prove prospective collection, "
    "reviewer independence, representative sampling, or broader real-world performance."
)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path}: expected a JSON object")
    return value


def _timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO 8601 timestamp with timezone")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO 8601 timestamp with timezone") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone")
    return result


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value.strip()


def _dataset(path: Path) -> tuple[dict[str, Any], list[tuple[dict[str, Any], list[Intent]]]]:
    data = _read(path)
    if set(data) != {"schema_version", "sampling", "cases"} or data["schema_version"] != 1:
        raise ValueError("dataset requires schema_version 1, sampling, and cases")
    _nonempty(data["sampling"], "sampling")
    if not isinstance(data["cases"], list) or not data["cases"]:
        raise ValueError("dataset requires at least one case")
    parsed: list[tuple[dict[str, Any], list[Intent]]] = []
    seen: set[str] = set()
    for case in data["cases"]:
        if not isinstance(case, dict) or set(case) != {
            "id",
            "project",
            "base_sha",
            "captured_at",
            "intents",
        }:
            raise ValueError("each case requires id, project, base_sha, captured_at, intents")
        case_id = _nonempty(case["id"], "case id")
        if case_id in seen:
            raise ValueError(f"duplicate case id: {case_id}")
        seen.add(case_id)
        _nonempty(case["project"], f"{case_id} project")
        if not isinstance(case["base_sha"], str) or not SHA.fullmatch(case["base_sha"]):
            raise ValueError(f"{case_id} base_sha must be a full Git SHA")
        captured_at = _timestamp(case["captured_at"], f"{case_id} captured_at")
        if not isinstance(case["intents"], list) or len(case["intents"]) != 2:
            raise ValueError(f"{case_id} requires exactly two pre-work intents")
        for raw in case["intents"]:
            if not isinstance(raw, dict) or "id" not in raw or "created_at" not in raw:
                raise ValueError(f"{case_id} intents require explicit id and created_at")
        intents = [Intent.model_validate(item) for item in case["intents"]]
        if len({item.id for item in intents}) != 2 or len({item.author for item in intents}) != 2:
            raise ValueError(f"{case_id} intents require distinct IDs and authors")
        if any(item.status not in {IntentStatus.DRAFT, IntentStatus.ACCEPTED} for item in intents):
            raise ValueError(f"{case_id} intents must be draft or accepted at capture")
        if any(item.created_at > captured_at for item in intents):
            raise ValueError(f"{case_id} intent timestamp is after capture")
        parsed.append((case, intents))
    return data, parsed


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _serialized(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


class _PublishedFile:
    def __init__(self, path: Path, descriptor: int):
        self.path = path
        self.descriptor = descriptor
        metadata = os.fstat(descriptor)
        self.identity = (metadata.st_dev, metadata.st_ino)

    def matches(self) -> bool:
        try:
            metadata = self.path.stat(follow_symlinks=False)
            return (metadata.st_dev, metadata.st_ino) == self.identity
        except FileNotFoundError:
            return False

    def remove_if_owned(self) -> None:
        if self.matches():
            self.path.unlink()

    def close(self) -> None:
        os.close(self.descriptor)


def _write_exclusive(path: Path, value: dict[str, Any]) -> _PublishedFile:
    """Publish a complete private JSON file without replacing an existing artifact."""
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".backbone-study-", delete=False
        ) as stream:
            temporary = Path(stream.name)
            os.chmod(temporary, 0o600)
            stream.write(_serialized(value).decode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(temporary, os.O_RDONLY)
        os.link(temporary, path)
        return _PublishedFile(path, descriptor)
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _publish_validated(
    output: Path,
    value: dict[str, Any],
    inputs: tuple[tuple[Path, str], ...],
    input_error: str,
    output_error: str,
) -> _PublishedFile:
    published = _write_exclusive(output, value)
    try:
        if any(_digest(path) != expected for path, expected in inputs):
            raise ValueError(input_error)
        if output.read_bytes() != _serialized(value) or not published.matches():
            raise ValueError(output_error)
    except BaseException:
        published.remove_if_owned()
        published.close()
        raise
    return published


def capture(
    repo: Path,
    project: str,
    sampling: str,
    dataset: Path,
    predictions: Path,
    ledger_branch: str | None = None,
) -> dict[str, Any]:
    """Capture all currently unassigned pre-work intent pairs and freeze warnings."""
    conductor = Conductor(repo, ledger_branch=ledger_branch)
    root = conductor.code_store.root
    if not dataset.is_absolute() or not predictions.is_absolute():
        raise ValueError("dataset and predictions must use absolute private paths")
    if dataset.resolve() == predictions.resolve():
        raise ValueError("dataset and predictions must be different files")
    if any(path.resolve().is_relative_to(root) for path in (dataset, predictions)):
        raise ValueError("study files must be outside the public code repository")
    _nonempty(project, "project")
    _nonempty(sampling, "sampling")
    if dataset.exists() or predictions.exists():
        raise FileExistsError("study outputs already exist; capture never overwrites evidence")
    if conductor.code_store._git("status", "--porcelain", "--untracked-files=all").stdout:
        raise ValueError("code worktree must be clean before capturing a baseline")
    base_sha = conductor.code_store._git("rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
    state = conductor.store.read()
    assigned = {task.intent_id for task in state.tasks.values()}
    eligible = sorted(
        (
            intent
            for intent in state.intents.values()
            if intent.status in {IntentStatus.DRAFT, IntentStatus.ACCEPTED}
            and intent.id not in assigned
        ),
        key=lambda intent: intent.id,
    )
    captured_at = datetime.now(UTC)
    cases = [
        {
            "id": f"pair-{left.id}-{right.id}",
            "project": project,
            "base_sha": base_sha,
            "captured_at": captured_at.isoformat(),
            "intents": [left.model_dump(mode="json"), right.model_dump(mode="json")],
        }
        for left, right in combinations(eligible, 2)
        if left.author != right.author
    ]
    if not cases:
        raise ValueError("no eligible unassigned intent pairs with distinct authors")
    if any(intent.created_at > captured_at for intent in eligible):
        raise ValueError("an intent timestamp is after capture time")

    def require_unchanged_snapshot() -> None:
        if (
            conductor.code_store._git("rev-parse", "--verify", "HEAD^{commit}").stdout.strip()
            != base_sha
        ):
            raise ValueError("code HEAD changed during capture")
        if conductor.code_store._git("status", "--porcelain", "--untracked-files=all").stdout:
            raise ValueError("code worktree changed during capture")
        if conductor.store.read().version != state.version:
            raise ValueError("Backbone state changed during capture")

    require_unchanged_snapshot()
    data = {"schema_version": 1, "sampling": sampling, "cases": cases}
    dataset_publication = _write_exclusive(dataset, data)
    predictions_publication = None
    try:
        frozen, predictions_publication = _freeze_and_identity(dataset, predictions)
        if _digest(dataset) != frozen["dataset_sha256"] or not dataset_publication.matches():
            raise ValueError("dataset changed after deterministic predictions were frozen")
        prediction_bytes = predictions.read_bytes()
        if prediction_bytes != _serialized(frozen) or not predictions_publication.matches():
            raise ValueError("deterministic predictions changed during capture")
        require_unchanged_snapshot()
        return {
            "case_count": len(cases),
            "base_sha": base_sha,
            "backbone_version": state.version,
            "dataset_sha256": frozen["dataset_sha256"],
            "predictions_sha256": hashlib.sha256(prediction_bytes).hexdigest(),
        }
    except BaseException:
        if predictions_publication is not None:
            predictions_publication.remove_if_owned()
        dataset_publication.remove_if_owned()
        raise
    finally:
        if predictions_publication is not None:
            predictions_publication.close()
        dataset_publication.close()


def freeze(dataset: Path, output: Path) -> dict[str, Any]:
    frozen, published = _freeze_and_identity(dataset, output)
    published.close()
    return frozen


def _freeze_and_identity(dataset: Path, output: Path) -> tuple[dict[str, Any], _PublishedFile]:
    dataset_sha = _digest(dataset)
    data, cases = _dataset(dataset)
    if _digest(dataset) != dataset_sha:
        raise ValueError("dataset changed while deterministic predictions were preparing")
    detector = hashlib.sha256()
    for name in ("conflicts.py", "models.py"):
        detector.update((ROOT / "src" / "backbone_conductor" / name).read_bytes())
    detector.update(Path(__file__).read_bytes())
    frozen = {
        "schema_version": 1,
        "dataset_sha256": dataset_sha,
        "detector_sha256": detector.hexdigest(),
        "frozen_at": datetime.now(UTC).isoformat(),
        "sampling": data["sampling"],
        "cases": [],
    }
    for case, intents in cases:
        state = BackboneState(intents={intent.id: intent for intent in intents})
        findings = [
            {
                "id": item.id,
                "rule": item.rule,
                "severity": item.severity.value,
                "parties": sorted(item.parties),
                "evidence": item.evidence,
            }
            for item in detect_conflicts(state)
            if not item.resolved
        ]
        frozen["cases"].append({"id": case["id"], "findings": findings})
    if _digest(dataset) != dataset_sha:
        raise ValueError("dataset changed while deterministic predictions were freezing")
    published = _publish_validated(
        output,
        frozen,
        ((dataset, dataset_sha),),
        "dataset changed while deterministic predictions were publishing",
        "deterministic predictions changed during publication",
    )
    return frozen, published


def _validated_predictions(
    dataset: Path,
    data: dict[str, Any],
    cases: list[tuple[dict[str, Any], list[Intent]]],
    predictions: Path,
) -> tuple[dict[str, Any], dict[str, bool]]:
    frozen = _read(predictions)
    if (
        set(frozen)
        != {"schema_version", "dataset_sha256", "detector_sha256", "frozen_at", "sampling", "cases"}
        or frozen["schema_version"] != 1
    ):
        raise ValueError("predictions have an invalid schema")
    if frozen["dataset_sha256"] != _digest(dataset) or frozen["sampling"] != data["sampling"]:
        raise ValueError("predictions do not match the frozen dataset")
    _timestamp(frozen["frozen_at"], "frozen_at")
    if not isinstance(frozen["detector_sha256"], str) or not SHA256.fullmatch(
        frozen["detector_sha256"]
    ):
        raise ValueError("predictions require a detector SHA-256")
    if not isinstance(frozen["cases"], list) or len(frozen["cases"]) != len(cases):
        raise ValueError("predictions must cover every dataset case")
    predicted: dict[str, bool] = {}
    for (case, intents), item in zip(cases, frozen["cases"], strict=True):
        if not isinstance(item, dict) or set(item) != {"id", "findings"}:
            raise ValueError("prediction case has an invalid schema")
        if item["id"] != case["id"] or not isinstance(item["findings"], list):
            raise ValueError("prediction cases must match dataset order and IDs")
        for finding in item["findings"]:
            if not isinstance(finding, dict) or set(finding) != {
                "id",
                "rule",
                "severity",
                "parties",
                "evidence",
            }:
                raise ValueError(f"{case['id']}: finding has an invalid schema")
            if (
                not isinstance(finding["id"], str)
                or not finding["id"]
                or not isinstance(finding["rule"], str)
                or not finding["rule"]
                or finding["severity"] not in {"advisory", "blocking", "critical"}
                or not isinstance(finding["parties"], list)
                or set(finding["parties"]) != {intent.id for intent in intents}
                or not isinstance(finding["evidence"], dict)
            ):
                raise ValueError(f"{case['id']}: finding does not describe this pair")
        predicted[case["id"]] = bool(item["findings"])
    return frozen, predicted


def _semantic_context(
    case: dict[str, Any], intents: list[Intent], rule_case: dict[str, Any]
) -> dict[str, Any]:
    return {
        "base_sha": case["base_sha"],
        "intents": [intent.model_dump(mode="json", include=ADVICE_FIELDS) for intent in intents],
        "accepted_decisions": [],
        "deterministic_conflicts": [
            {key: item[key] for key in ("id", "rule", "severity", "evidence")}
            for item in rule_case["findings"]
        ],
    }


def freeze_semantic(
    dataset: Path,
    predictions: Path,
    output: Path,
    dsh_home: Path,
    model: str,
    provider: str = "deepseek-official",
) -> dict[str, Any]:
    """Freeze optional model advice against the same pre-work pairs and rule predictions."""
    if not all(path.is_absolute() for path in (dataset, predictions, output, dsh_home)):
        raise ValueError("semantic study paths and DSH home must be absolute")
    if len({path.resolve() for path in (dataset, predictions, output)}) != 3:
        raise ValueError("semantic output must differ from dataset and predictions")
    if output.resolve().is_relative_to(ROOT) or dsh_home.resolve().is_relative_to(ROOT):
        raise ValueError("semantic output and DSH home must be outside the public repository")
    if output.exists():
        raise FileExistsError(
            "semantic predictions already exist; freeze never overwrites evidence"
        )
    dataset_sha = _digest(dataset)
    predictions_sha = _digest(predictions)
    data, cases = _dataset(dataset)
    deterministic, _predicted = _validated_predictions(dataset, data, cases, predictions)
    if _digest(dataset) != dataset_sha or _digest(predictions) != predictions_sha:
        raise ValueError("study inputs changed while semantic advice was preparing")
    started_at = datetime.now(UTC)
    if started_at < _timestamp(deterministic["frozen_at"], "deterministic frozen_at") or any(
        started_at < _timestamp(case["captured_at"], "captured_at") for case, _intents in cases
    ):
        raise ValueError("semantic advice cannot run before its inputs were captured and frozen")
    reviewer = DSHReviewer(dsh_home, model, provider)
    implementation = hashlib.sha256()
    for path in (ROOT / "src" / "backbone_conductor" / "runtime.py", Path(__file__)):
        implementation.update(path.read_bytes())
    frozen = {
        "schema_version": 1,
        "dataset_sha256": dataset_sha,
        "predictions_sha256": predictions_sha,
        "implementation_sha256": implementation.hexdigest(),
        "model": model,
        "provider": provider,
        "cases": [],
    }
    for (case, intents), rule_case in zip(cases, deterministic["cases"], strict=True):
        context = _semantic_context(case, intents, rule_case)
        context_bytes = json.dumps(context, sort_keys=True, ensure_ascii=False).encode()
        if len(context_bytes) > 1_000_000:
            raise ValueError(f"{case['id']}: semantic context exceeds 1 MB")
        advice = reviewer.advise_conflict(context)
        fields = {key: advice[key] for key in SemanticConflictAdvice.model_fields if key in advice}
        validated = SemanticConflictAdvice.model_validate(fields).model_dump()
        runtime = advice.get("runtime")
        if not isinstance(runtime, dict) or runtime.get("finish_reason") != "completed":
            raise ValueError(f"{case['id']}: model advice did not complete")
        frozen["cases"].append(
            {
                "id": case["id"],
                "context_sha256": hashlib.sha256(context_bytes).hexdigest(),
                "advice": {**validated, "runtime": runtime},
            }
        )
    if (
        _digest(dataset) != frozen["dataset_sha256"]
        or _digest(predictions) != frozen["predictions_sha256"]
    ):
        raise ValueError("study inputs changed while semantic advice was running")
    frozen["frozen_at"] = datetime.now(UTC).isoformat()
    published = _publish_validated(
        output,
        frozen,
        ((dataset, dataset_sha), (predictions, predictions_sha)),
        "study inputs changed while semantic advice was publishing",
        "semantic predictions changed during publication",
    )
    published.close()
    return frozen


def _review_file(
    path: Path,
    dataset_sha: str,
    predictions_sha: str,
    authors: dict[str, set[str]],
    packet_cases: dict[str, dict[str, Any]],
    semantic_sha: str | None = None,
) -> tuple[str, dict[str, bool]]:
    data = _read(path)
    schema_version = data.get("schema_version")
    expected = {"schema_version", "dataset_sha256", "predictions_sha256", "reviewer", "cases"}
    if semantic_sha is not None:
        expected.add("semantic_predictions_sha256")
    if schema_version == 2:
        expected.add("label_definition")
    if set(data) != expected or type(schema_version) is not int or schema_version not in {1, 2}:
        raise ValueError(f"{path}: review file has an invalid schema")
    if schema_version == 2 and data["label_definition"] != BLIND_LABEL_DEFINITION:
        raise ValueError(f"{path}: review label definition has changed")
    if (
        data["dataset_sha256"] != dataset_sha
        or data["predictions_sha256"] != predictions_sha
        or (semantic_sha is not None and data["semantic_predictions_sha256"] != semantic_sha)
        or not isinstance(data["cases"], list)
    ):
        raise ValueError(f"{path}: review does not match frozen dataset and predictions")
    reviewer = _nonempty(data["reviewer"], f"{path} reviewer")
    labels: dict[str, bool] = {}
    seen: set[str] = set()
    for item in data["cases"]:
        if not isinstance(item, dict):
            raise ValueError(f"{path}: each review case must be an object")
        case_id = _nonempty(item.get("id"), f"{path} case id")
        if case_id not in authors or case_id in seen:
            raise ValueError(f"{path}: unknown or duplicate case {case_id}")
        seen.add(case_id)
        if schema_version == 1:
            if set(item) != {"id", "conflict", "rationale"}:
                raise ValueError(f"{path}: each review case needs id, conflict, rationale")
        else:
            expected_case = packet_cases[case_id]
            if set(item) != set(expected_case) or any(
                item[key] != expected_case[key]
                for key in expected_case
                if key not in {"conflict", "rationale"}
            ):
                raise ValueError(f"{case_id}: review packet does not match frozen dataset")
        if reviewer in authors[case_id]:
            raise ValueError(f"{case_id}: reviewer must differ from intent authors")
        if schema_version == 2 and item["conflict"] is None:
            if item["rationale"] != "":
                raise ValueError(f"{case_id}: unlabeled review must have empty rationale")
            continue
        if type(item["conflict"]) is not bool:
            raise ValueError(f"{case_id}: conflict label must be boolean")
        _nonempty(item["rationale"], f"{case_id} rationale")
        labels[case_id] = item["conflict"]
    return reviewer, labels


def _validated_semantic(
    path: Path,
    dataset_sha: str,
    predictions_sha: str,
    cases: list[tuple[dict[str, Any], list[Intent]]],
    deterministic: dict[str, Any],
) -> dict[str, str]:
    data = _read(path)
    if (
        set(data)
        != {
            "schema_version",
            "dataset_sha256",
            "predictions_sha256",
            "implementation_sha256",
            "frozen_at",
            "model",
            "provider",
            "cases",
        }
        or data["schema_version"] != 1
    ):
        raise ValueError("semantic predictions have an invalid schema")
    if data["dataset_sha256"] != dataset_sha or data["predictions_sha256"] != predictions_sha:
        raise ValueError("semantic predictions do not match frozen dataset and rules")
    frozen_at = _timestamp(data["frozen_at"], "semantic frozen_at")
    if frozen_at < _timestamp(deterministic["frozen_at"], "deterministic frozen_at") or any(
        frozen_at < _timestamp(case["captured_at"], "captured_at") for case, _intents in cases
    ):
        raise ValueError("semantic advice claims a freeze before its inputs existed")
    if not isinstance(data["implementation_sha256"], str) or not SHA256.fullmatch(
        data["implementation_sha256"]
    ):
        raise ValueError("semantic predictions require an implementation SHA-256")
    _nonempty(data["model"], "semantic model")
    _nonempty(data["provider"], "semantic provider")
    if not isinstance(data["cases"], list) or len(data["cases"]) != len(cases):
        raise ValueError("semantic predictions must cover every dataset case")
    verdicts = {}
    for (case, intents), rule_case, item in zip(
        cases, deterministic["cases"], data["cases"], strict=True
    ):
        if not isinstance(item, dict) or set(item) != {"id", "context_sha256", "advice"}:
            raise ValueError("semantic prediction case has an invalid schema")
        if item["id"] != case["id"]:
            raise ValueError("semantic prediction cases must match dataset order and IDs")
        if not isinstance(item["context_sha256"], str) or not SHA256.fullmatch(
            item["context_sha256"]
        ):
            raise ValueError(f"{case['id']}: semantic context requires a SHA-256")
        expected_context_sha = hashlib.sha256(
            json.dumps(
                _semantic_context(case, intents, rule_case), sort_keys=True, ensure_ascii=False
            ).encode()
        ).hexdigest()
        if item["context_sha256"] != expected_context_sha:
            raise ValueError(f"{case['id']}: semantic context does not match frozen inputs")
        advice = item["advice"]
        if not isinstance(advice, dict) or set(advice) != {
            *SemanticConflictAdvice.model_fields,
            "runtime",
        }:
            raise ValueError(f"{case['id']}: semantic advice has an invalid schema")
        SemanticConflictAdvice.model_validate(
            {key: advice[key] for key in SemanticConflictAdvice.model_fields}
        )
        if (
            not isinstance(advice["runtime"], dict)
            or advice["runtime"].get("finish_reason") != "completed"
        ):
            raise ValueError(f"{case['id']}: semantic advice did not complete")
        verdicts[case["id"]] = advice["verdict"]
    return verdicts


def _blind_review_case(case: dict[str, Any], intents: list[Intent]) -> dict[str, Any]:
    """Expose frozen plans without rule findings, model advice, or labels."""
    return {
        "id": case["id"],
        "project": case["project"],
        "base_sha": case["base_sha"],
        "captured_at": case["captured_at"],
        "intents": [
            intent.model_dump(mode="json", include=BLIND_REVIEW_FIELDS) for intent in intents
        ],
        "conflict": None,
        "rationale": "",
    }


def prepare_review(
    dataset: Path,
    predictions: Path,
    output: Path,
    reviewer: str,
    semantic_predictions: Path | None = None,
) -> dict[str, Any]:
    """Create a private, prediction-blind review file ready for human labels."""
    paths = (dataset, predictions, output)
    if semantic_predictions is not None:
        paths += (semantic_predictions,)
    if not all(path.is_absolute() for path in paths):
        raise ValueError("review paths must be absolute")
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("review output must differ from study inputs")
    if output.resolve().is_relative_to(ROOT):
        raise ValueError("review file must be outside the public repository")
    if output.exists():
        raise FileExistsError("review file already exists; preparation never overwrites labels")
    reviewer = _nonempty(reviewer, "reviewer")
    dataset_sha, predictions_sha = _digest(dataset), _digest(predictions)
    data, cases = _dataset(dataset)
    frozen, _predicted = _validated_predictions(dataset, data, cases, predictions)
    semantic_sha = _digest(semantic_predictions) if semantic_predictions is not None else None
    if semantic_predictions is not None:
        _validated_semantic(semantic_predictions, dataset_sha, predictions_sha, cases, frozen)
    for case, intents in cases:
        if reviewer in {intent.author for intent in intents}:
            raise ValueError(f"{case['id']}: reviewer must differ from intent authors")
    if (
        _digest(dataset) != dataset_sha
        or _digest(predictions) != predictions_sha
        or (semantic_predictions is not None and _digest(semantic_predictions) != semantic_sha)
    ):
        raise ValueError("study inputs changed while preparing blind review")
    packet = {
        "schema_version": 2,
        "dataset_sha256": dataset_sha,
        "predictions_sha256": predictions_sha,
        "reviewer": reviewer,
        "label_definition": BLIND_LABEL_DEFINITION,
        "cases": [_blind_review_case(case, intents) for case, intents in cases],
    }
    if semantic_sha is not None:
        packet["semantic_predictions_sha256"] = semantic_sha
    inputs = ((dataset, dataset_sha), (predictions, predictions_sha))
    if semantic_predictions is not None:
        inputs += ((semantic_predictions, semantic_sha),)
    published = _publish_validated(
        output,
        packet,
        inputs,
        "study inputs changed while blind review was publishing",
        "blind review packet changed during publication",
    )
    published.close()
    return packet


def _metrics(counts: dict[str, int]) -> dict[str, Any]:
    precision = (
        counts["tp"] / (counts["tp"] + counts["fp"]) if counts["tp"] + counts["fp"] else None
    )
    recall = counts["tp"] / (counts["tp"] + counts["fn"]) if counts["tp"] + counts["fn"] else None
    return {
        "counts": counts,
        "precision": precision,
        "recall": recall,
        "f1": (
            (2 * precision * recall / (precision + recall) if precision + recall else 0.0)
            if precision is not None and recall is not None
            else None
        ),
    }


def _outcome(warning: bool, observed: bool) -> str:
    return "tp" if warning and observed else "fp" if warning else "fn" if observed else "tn"


def score(
    dataset: Path,
    predictions: Path,
    first_review: Path,
    second_review: Path,
    adjudications: Path | None = None,
    semantic_predictions: Path | None = None,
) -> dict[str, Any]:
    dataset_sha = _digest(dataset)
    prediction_sha = _digest(predictions)
    data, cases = _dataset(dataset)
    frozen, predicted = _validated_predictions(dataset, data, cases, predictions)
    if _digest(dataset) != dataset_sha or _digest(predictions) != prediction_sha:
        raise ValueError("study inputs changed while scoring was preparing")
    authors = {case["id"]: {intent.author for intent in intents} for case, intents in cases}
    packet_cases = {case["id"]: _blind_review_case(case, intents) for case, intents in cases}
    semantic_sha = _digest(semantic_predictions) if semantic_predictions is not None else None
    semantic_verdicts = (
        _validated_semantic(semantic_predictions, dataset_sha, prediction_sha, cases, frozen)
        if semantic_predictions is not None
        else None
    )
    first_review_sha = _digest(first_review)
    second_review_sha = _digest(second_review)
    adjudications_sha = _digest(adjudications) if adjudications is not None else None
    reviewer_a, labels_a = _review_file(
        first_review, dataset_sha, prediction_sha, authors, packet_cases, semantic_sha
    )
    reviewer_b, labels_b = _review_file(
        second_review, dataset_sha, prediction_sha, authors, packet_cases, semantic_sha
    )
    if reviewer_a == reviewer_b:
        raise ValueError("two distinct reviewers are required")
    if adjudications is not None:
        adjudicator, adjudicated = _review_file(
            adjudications, dataset_sha, prediction_sha, authors, packet_cases, semantic_sha
        )
        if adjudicator in {reviewer_a, reviewer_b}:
            raise ValueError("adjudicator must differ from both reviewers")
    else:
        adjudicated = {}

    counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    semantic_counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    combined_counts = {"tp": 0, "fp": 0, "fn": 0, "tn": 0}
    resolved_abstentions = 0
    rows = []
    for case, _intents in cases:
        case_id = case["id"]
        semantic_verdict = semantic_verdicts[case_id] if semantic_verdicts is not None else None
        values = [labels[case_id] for labels in (labels_a, labels_b) if case_id in labels]
        if len(values) < 2:
            if case_id in adjudicated:
                raise ValueError(f"{case_id}: adjudication requires two disagreeing reviews")
            if not values:
                row = {"id": case_id, "status": "unlabeled", "prediction": predicted[case_id]}
                if semantic_verdict is not None:
                    row["semantic_verdict"] = semantic_verdict
                rows.append(row)
                continue
            row = {"id": case_id, "status": "awaiting_review", "prediction": predicted[case_id]}
            if semantic_verdict is not None:
                row["semantic_verdict"] = semantic_verdict
            rows.append(row)
            continue
        if values[0] == values[1]:
            if case_id in adjudicated:
                raise ValueError(f"{case_id}: agreeing reviews need no adjudication")
            observed, source = values[0], "consensus"
        elif case_id in adjudicated:
            observed = adjudicated[case_id]
            source = "adjudicated"
        else:
            row = {"id": case_id, "status": "disputed", "prediction": predicted[case_id]}
            if semantic_verdict is not None:
                row["semantic_verdict"] = semantic_verdict
            rows.append(row)
            continue
        warning = predicted[case_id]
        outcome = _outcome(warning, observed)
        counts[outcome] += 1
        row = {
            "id": case_id,
            "project": case["project"],
            "status": source,
            "prediction": warning,
            "label": observed,
            "outcome": outcome,
        }
        if semantic_verdict is not None:
            semantic_warning = semantic_verdict == "conflict"
            combined_warning = warning or semantic_warning
            semantic_outcome = _outcome(semantic_warning, observed)
            combined_outcome = _outcome(combined_warning, observed)
            semantic_counts[semantic_outcome] += 1
            combined_counts[combined_outcome] += 1
            resolved_abstentions += semantic_verdict == "uncertain"
            row.update(
                semantic_verdict=semantic_verdict,
                semantic_outcome=semantic_outcome,
                combined_prediction=combined_warning,
                combined_outcome=combined_outcome,
            )
        rows.append(row)
    resolved = sum(counts.values())
    metrics = _metrics(counts)
    both_labeled = labels_a.keys() & labels_b.keys()
    agreed = sum(labels_a[case_id] == labels_b[case_id] for case_id in both_labeled)
    labeling = {
        "first_labeled_count": len(labels_a),
        "second_labeled_count": len(labels_b),
        "both_labeled_count": len(both_labeled),
        "agreed_count": agreed,
        "disagreed_count": len(both_labeled) - agreed,
        "adjudicated_count": len(adjudicated),
        "unresolved_disagreement_count": len(both_labeled) - agreed - len(adjudicated),
        "observed_agreement": agreed / len(both_labeled) if both_labeled else None,
    }
    project_counts: dict[str, dict[str, Any]] = {}
    for (case, _intents), row in zip(cases, rows, strict=True):
        project = case["project"]
        summary = project_counts.setdefault(
            project,
            {
                "sample_count": 0,
                "resolved_count": 0,
                "deterministic": {"tp": 0, "fp": 0, "fn": 0, "tn": 0},
                "semantic": {"tp": 0, "fp": 0, "fn": 0, "tn": 0},
                "combined": {"tp": 0, "fp": 0, "fn": 0, "tn": 0},
            },
        )
        summary["sample_count"] += 1
        if "outcome" in row:
            summary["resolved_count"] += 1
            summary["deterministic"][row["outcome"]] += 1
            if semantic_verdicts is not None:
                summary["semantic"][row["semantic_outcome"]] += 1
                summary["combined"][row["combined_outcome"]] += 1
    project_summaries = {}
    for project, summary in sorted(project_counts.items()):
        project_report = {
            "sample_count": summary["sample_count"],
            "resolved_count": summary["resolved_count"],
            "deterministic": _metrics(summary["deterministic"]),
        }
        if semantic_verdicts is not None:
            project_report["semantic"] = _metrics(summary["semantic"])
            project_report["combined"] = _metrics(summary["combined"])
        project_summaries[project] = project_report
    report = {
        "status": "complete_sample" if resolved == len(cases) else "incomplete",
        "dataset_sha256": dataset_sha,
        "predictions_sha256": prediction_sha,
        "first_review_sha256": first_review_sha,
        "second_review_sha256": second_review_sha,
        "detector_sha256": frozen["detector_sha256"],
        "sampling": data["sampling"],
        "projects": sorted({case["project"] for case, _intents in cases}),
        "resolved_projects": sorted({row["project"] for row in rows if "project" in row}),
        "sample_count": len(cases),
        "resolved_count": resolved,
        "labeling": labeling,
        "project_summaries": project_summaries,
        **metrics,
        "cases": rows,
        "limitation": LIMITATION,
    }
    if semantic_verdicts is not None:
        report["semantic_predictions_sha256"] = semantic_sha
        report["semantic"] = {
            **_metrics(semantic_counts),
            "resolved_abstentions": resolved_abstentions,
            "total_abstentions": sum(value == "uncertain" for value in semantic_verdicts.values()),
            "uncertain_policy": "uncertain counts as no alert; positive labels become false negatives",
        }
        report["combined"] = _metrics(combined_counts)
        report["limitation"] += (
            " Semantic advice sees rule findings and intent plans, so its arm is not an "
            "independent model-only detector. The prospective dataset omits accepted decisions. "
            "Model calls may incur provider cost, and external evidence "
            "is required to prove advice was frozen before outcomes and hidden from reviewers."
        )
    if adjudications is not None:
        report["adjudications_sha256"] = adjudications_sha
    if (
        _digest(dataset) != dataset_sha
        or _digest(predictions) != prediction_sha
        or (semantic_predictions is not None and _digest(semantic_predictions) != semantic_sha)
    ):
        raise ValueError("study inputs changed during scoring")
    if (
        _digest(first_review) != first_review_sha
        or _digest(second_review) != second_review_sha
        or (adjudications is not None and _digest(adjudications) != adjudications_sha)
    ):
        raise ValueError("review labels changed during scoring")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    capture_command = actions.add_parser(
        "capture",
        help="Capture all unassigned intent pairs from a clean Git checkout and freeze them",
    )
    capture_command.add_argument("--repo", type=Path, required=True)
    capture_command.add_argument("--ledger-branch")
    capture_command.add_argument("--project", required=True)
    capture_command.add_argument("--sampling", required=True)
    capture_command.add_argument("--dataset", type=Path, required=True)
    capture_command.add_argument("--predictions", type=Path, required=True)
    freeze_command = actions.add_parser("freeze", help="Freeze predictions before human labeling")
    freeze_command.add_argument("--dataset", type=Path, required=True)
    freeze_command.add_argument("--output", type=Path, required=True)
    semantic_command = actions.add_parser(
        "freeze-semantic", help="Freeze optional DSH advice against pre-work pairs"
    )
    semantic_command.add_argument("--dataset", type=Path, required=True)
    semantic_command.add_argument("--predictions", type=Path, required=True)
    semantic_command.add_argument("--output", type=Path, required=True)
    semantic_command.add_argument("--dsh-home", type=Path, required=True)
    semantic_command.add_argument("--model", required=True)
    semantic_command.add_argument("--provider", default="deepseek-official")
    review_command = actions.add_parser(
        "prepare-review", help="Create a prediction-blind file for an independent reviewer"
    )
    review_command.add_argument("--dataset", type=Path, required=True)
    review_command.add_argument("--predictions", type=Path, required=True)
    review_command.add_argument("--semantic-predictions", type=Path)
    review_command.add_argument("--reviewer", required=True)
    review_command.add_argument("--output", type=Path, required=True)
    score_command = actions.add_parser("score", help="Score a frozen sample against reviews")
    score_command.add_argument("--dataset", type=Path, required=True)
    score_command.add_argument("--predictions", type=Path, required=True)
    score_command.add_argument("--semantic-predictions", type=Path)
    score_command.add_argument("--review", action="append", type=Path, required=True)
    score_command.add_argument("--adjudications", type=Path)
    score_command.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "capture":
            result = capture(
                args.repo,
                args.project,
                args.sampling,
                args.dataset,
                args.predictions,
                args.ledger_branch,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        if args.action == "freeze":
            frozen = freeze(args.dataset, args.output)
            print(f"Frozen {len(frozen['cases'])} cases to {args.output}")
            print(f"Dataset SHA-256: {frozen['dataset_sha256']}")
            return 0
        if args.action == "freeze-semantic":
            frozen = freeze_semantic(
                args.dataset,
                args.predictions,
                args.output,
                args.dsh_home,
                args.model,
                args.provider,
            )
            print(f"Frozen {len(frozen['cases'])} semantic cases to {args.output}")
            print(f"Semantic predictions SHA-256: {_digest(args.output)}")
            return 0
        if args.action == "prepare-review":
            packet = prepare_review(
                args.dataset,
                args.predictions,
                args.output,
                args.reviewer,
                args.semantic_predictions,
            )
            print(
                json.dumps(
                    {
                        "reviewer": packet["reviewer"],
                        "case_count": len(packet["cases"]),
                        "output": str(args.output),
                        "packet_sha256": _digest(args.output),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if len(args.review) != 2:
            raise ValueError("score requires exactly two --review files")
        report = score(
            args.dataset,
            args.predictions,
            args.review[0],
            args.review[1],
            args.adjudications,
            args.semantic_predictions,
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        parser.error(f"invalid prospective evaluation: {exc}")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"Prospective sample: {report['resolved_count']}/{report['sample_count']} resolved")
        labeling = report["labeling"]
        agreement = (
            "n/a"
            if labeling["observed_agreement"] is None
            else f"{labeling['observed_agreement']:.1%}"
        )
        print(
            f"Double-labeled cases: {labeling['both_labeled_count']} | "
            f"agreement {agreement} | "
            f"unresolved disputes {labeling['unresolved_disagreement_count']}"
        )
        label = "Rules: " if "semantic" in report else ""
        print(
            f"{label}TP {report['counts']['tp']} | FP {report['counts']['fp']} | FN {report['counts']['fn']} | TN {report['counts']['tn']}"
        )
        precision = "n/a" if report["precision"] is None else f"{report['precision']:.1%}"
        recall = "n/a" if report["recall"] is None else f"{report['recall']:.1%}"
        print(f"Precision {precision} | Recall {recall}")
        if "semantic" in report:
            for name in ("semantic", "combined"):
                arm = report[name]
                arm_precision = "n/a" if arm["precision"] is None else f"{arm['precision']:.1%}"
                arm_recall = "n/a" if arm["recall"] is None else f"{arm['recall']:.1%}"
                print(f"{name}: precision {arm_precision} | recall {arm_recall} | {arm['counts']}")
            print(f"Semantic abstentions (resolved): {report['semantic']['resolved_abstentions']}")
        print(report["limitation"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
