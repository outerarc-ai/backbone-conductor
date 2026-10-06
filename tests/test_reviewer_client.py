"""Keep reviewer credentials local and require TLS away from loopback."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from backbone_conductor.audit_history import verify_all_history_pages
from backbone_conductor.cli import _format_inspection, main
from backbone_conductor.reviewer_client import (
    _private_token,
    _validated_audit_report,
    _validated_full_patch,
    _validated_history_report,
    _validated_snapshot_report,
)


def test_reviewer_cli_requires_https_for_non_loopback(tmp_path: Path, capsys) -> None:
    token = tmp_path / "reviewer.token"
    token.write_text("r" * 48 + "\n", encoding="ascii")
    token.chmod(0o600)
    assert (
        main(
            [
                "reviewer",
                "--url",
                "http://coordinator.example.org",
                "--token-file",
                str(token),
                "whoami",
            ]
        )
        == 1
    )
    assert "requires HTTPS" in json.loads(capsys.readouterr().err)["error"]


def test_reviewer_token_file_rejects_shared_or_linked_credentials(tmp_path: Path) -> None:
    token = tmp_path / "reviewer.token"
    token.write_text("r" * 48 + "\n", encoding="ascii")
    token.chmod(0o600)
    assert _private_token(str(token)) == "r" * 48

    token.chmod(0o644)
    with pytest.raises(ValueError, match="mode 0600"):
        _private_token(str(token))
    token.chmod(0o600)

    symlink = tmp_path / "linked.token"
    symlink.symlink_to(token)
    with pytest.raises(ValueError, match="symlink"):
        _private_token(str(symlink))

    hardlink = tmp_path / "hardlinked.token"
    os.link(token, hardlink)
    with pytest.raises(ValueError, match="single-link"):
        _private_token(str(token))


def test_remote_audit_report_rejects_inconsistent_signature_counts() -> None:
    report = {
        "checked": 1,
        "limit": 1,
        "total_metadata_commits": 2,
        "truncated": True,
        "valid": 0,
        "unsigned": 1,
        "invalid": 0,
        "all_inspected_signed_and_valid": False,
        "commits": [{"commit": "a" * 40, "signature": "unsigned"}],
    }
    assert _validated_audit_report(report, 1) == report
    for bad in (
        {**report, "all_inspected_signed_and_valid": True},
        {**report, "valid": 1},
        {**report, "commits": [{"commit": "a" * 40, "signature": {}}]},
    ):
        with pytest.raises(ValueError, match="invalid audit report"):
            _validated_audit_report(bad, 1)


def test_remote_snapshot_report_rejects_inconsistent_result() -> None:
    report = {
        "version": "a" * 40,
        "git_parent_count": 1,
        "view_count": 2,
        "missing_views": [],
        "extra_views": [],
        "changed_views": [],
        "parent_version": "b" * 40,
        "expected_parent_version": "b" * 40,
        "merged_parent_version": None,
        "expected_merged_parent_version": None,
        "parent_links_ok": True,
        "ok": True,
    }
    assert _validated_snapshot_report(report) == report
    drifted = {**report, "changed_views": ["BACKBONE.md"], "ok": False}
    assert _validated_snapshot_report(drifted) == drifted
    for bad in (
        {**report, "version": "not-a-sha"},
        {**report, "view_count": -1},
        {**report, "changed_views": ["BACKBONE.md"], "ok": True},
        {**report, "expected_parent_version": "c" * 40},
        {key: value for key, value in report.items() if key != "parent_version"},
    ):
        with pytest.raises(ValueError, match="invalid snapshot report"):
            _validated_snapshot_report(bad)


def test_remote_history_report_rejects_inconsistent_counts() -> None:
    report = {
        "head": "a" * 40,
        "limit": 2,
        "offset": 0,
        "checked": 2,
        "total_metadata_commits": 2,
        "truncated": False,
        "next_offset": None,
        "invalid": 0,
        "page_ok": True,
        "ok": True,
        "commits": [
            {
                "commit": "b" * 40,
                "ok": True,
                "missing_views": [],
                "extra_views": [],
                "changed_views": [],
                "parent_links_ok": True,
            },
            {
                "commit": "c" * 40,
                "ok": True,
                "missing_views": [],
                "extra_views": [],
                "changed_views": [],
                "parent_links_ok": True,
            },
        ],
    }
    assert _validated_history_report(report, 2) == report
    for bad in (
        {**report, "invalid": 1},
        {**report, "truncated": True},
        {**report, "next_offset": 2},
        {**report, "offset": 1},
        {**report, "page_ok": False},
        {**report, "commits": [report["commits"][0]]},
        {**report, "commits": [{**report["commits"][0], "ok": False}, report["commits"][1]]},
    ):
        with pytest.raises(ValueError, match="invalid history report"):
            _validated_history_report(bad, 2)

    partial = {
        **report,
        "limit": 1,
        "checked": 1,
        "total_metadata_commits": 2,
        "truncated": True,
        "next_offset": 1,
        "ok": False,
        "commits": report["commits"][:1],
    }
    assert _validated_history_report(partial, 1) == partial
    final_page = {
        **report,
        "limit": 1,
        "offset": 1,
        "checked": 1,
        "commits": report["commits"][1:],
        "ok": False,
    }
    assert _validated_history_report(final_page, 1, 1, report["head"]) == final_page
    with pytest.raises(ValueError, match="invalid history report"):
        _validated_history_report(final_page, 1, 1, "d" * 40)
    combined = verify_all_history_pages(lambda offset, _head: (partial, final_page)[offset], 1)
    assert combined["ok"]
    assert combined["pages"] == 2
    assert combined["checked"] == 2
    repeated = {**final_page, "commits": partial["commits"]}
    with pytest.raises(ValueError, match="repeated commits"):
        verify_all_history_pages(lambda offset, _head: (partial, repeated)[offset], 1)


def test_remote_full_patch_rejects_truncation_or_hash_mismatch() -> None:
    patch = "diff --git a/a b/a\n+é\n"
    packet = {
        "git": {
            "base_sha": "a" * 40,
            "target_sha": "b" * 40,
            "integrated_into_target": False,
        },
        "diff": {
            "patch": patch,
            "truncated": False,
            "sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "changed_paths": ["a"],
        },
    }
    assert _validated_full_patch(packet) == packet
    for bad in (
        {"diff": {**packet["diff"], "patch": patch[:-1]}},
        {"diff": {**packet["diff"], "truncated": True}},
        {"diff": {**packet["diff"], "sha256": "0" * 64}},
        {"diff": {**packet["diff"], "patch": "x" * 1_000_001}},
    ):
        with pytest.raises(ValueError, match="invalid full review patch"):
            _validated_full_patch(bad)

    integrated = {
        **packet,
        "git": {**packet["git"], "integrated_into_target": True},
        "target_diff": {
            **packet["diff"],
            "base_sha": "a" * 40,
            "target_sha": "b" * 40,
        },
    }
    assert _validated_full_patch(integrated) == integrated
    for bad in (
        {**integrated, "target_diff": None},
        {
            **integrated,
            "target_diff": {**integrated["target_diff"], "patch": patch[:-1]},
        },
        {
            **integrated,
            "target_diff": {**integrated["target_diff"], "target_sha": "c" * 40},
        },
        {**packet, "target_diff": integrated["target_diff"]},
    ):
        with pytest.raises(ValueError, match="invalid full review patch"):
            _validated_full_patch(bad)


def _inspection_packet(patch: str) -> dict:
    return {
        "version": "v1",
        "current_version": "v1",
        "inspection_kind": "current",
        "current_task_status": "submitted",
        "approval": None,
        "task": {
            "id": "task-1",
            "status": "submitted",
            "member_id": "alice",
            "base_ref": "main",
            "spec": "Implement the export",
            "constraints": ["Keep the API stable"],
            "forbidden_paths": ["secrets/"],
            "artifact": {
                "branch": "feature/export",
                "summary": "Added export function",
                "changed_paths": ["a"],
            },
        },
        "intent": {
            "id": "intent-1",
            "status": "in_progress",
            "author": "owner",
            "problem": "Need an export\nfunction",
            "proposed_outcome": "Export records",
            "affected_paths": ["a"],
            "affected_symbols": ["export"],
            "constraints": ["Preserve input order"],
        },
        "git": {
            "base_sha": "a" * 40,
            "artifact_sha": "b" * 40,
            "target_sha": "c" * 40,
            "current_target_sha": "c" * 40,
            "branch_unchanged": True,
            "integrated_into_target": False,
            "net_changed_paths": [],
            "divergent_paths": [],
        },
        "decision_delta": {"new_decisions": ["decision-2"], "withdrawn_decisions": []},
        "accepted_decisions": [
            {
                "id": "decision-2",
                "decision_type": "api_design",
                "summary": "Keep old API",
                "rationale": "Clients depend on it",
            }
        ],
        "blocking_conflicts": [
            {
                "id": "conflict-1",
                "severity": "blocking",
                "conflict_type": "decision_conflict",
                "rule": "incompatible_api",
                "parties": ["intent-1", "decision-2"],
                "evidence": {"reason": "API mismatch"},
            }
        ],
        "diff": {
            "patch": patch,
            "truncated": False,
            "sha256": hashlib.sha256(patch.encode()).hexdigest(),
            "changed_paths": ["a"],
        },
        "target_diff": None,
    }


def test_text_inspection_shows_context_patch_and_truncation() -> None:
    patch = "diff --git a/a b/a\n+one\n+two\n"
    digest = hashlib.sha256(patch.encode()).hexdigest()
    packet = _inspection_packet(patch)
    rendered = _format_inspection(packet)
    assert "Problem:\n  Need an export\n  function" in rendered
    assert "Task constraints: Keep the API stable" in rendered
    assert "New accepted decisions since fork: decision-2" in rendered
    assert "decision-2 [api_design]" in rendered
    assert "Blocking conflicts at inspection (1):" in rendered
    assert 'Evidence: {"reason": "API mismatch"}' in rendered
    assert "+one\n+two" in rendered
    assert f"Full patch SHA-256: {digest}" in rendered
    assert "Integrated target diff: pending Git merge." in rendered
    assert "TRUNCATED PREVIEW" not in rendered

    preview = {**packet, "diff": {**packet["diff"], "patch": patch[:10], "truncated": True}}
    rendered = _format_inspection(preview)
    assert "TRUNCATED PREVIEW" in rendered
    assert "Review the complete patch with --full" in rendered

    integrated = {
        **packet,
        "git": {**packet["git"], "integrated_into_target": True},
        "target_diff": {
            **packet["diff"],
            "base_sha": "a" * 40,
            "target_sha": "c" * 40,
        },
    }
    assert "Integrated target paths versus base" in _format_inspection(integrated)
    with pytest.raises(ValueError, match="SHA-256"):
        _format_inspection({**packet, "diff": {**packet["diff"], "patch": patch[:-1]}})
    with pytest.raises(ValueError, match="not bound"):
        _format_inspection(
            {
                **integrated,
                "target_diff": {**integrated["target_diff"], "changed_paths": ["other"]},
            }
        )
    with pytest.raises(ValueError, match="response is invalid"):
        _format_inspection({**packet, "intent": None})

    approved = {
        **integrated,
        "current_version": "v2",
        "inspection_kind": "approval",
        "current_task_status": "merged",
        "git": {**integrated["git"], "current_target_sha": "d" * 40},
        "approval": {
            "reviewed_version": "v1",
            "target_sha": "c" * 40,
            "decision": {
                "id": "review-1",
                "status": "accepted",
                "author": "reviewer",
                "rationale": "Checked the final tree",
            },
        },
    }
    rendered = _format_inspection(approved)
    assert "Task: task-1 (now merged)" in rendered
    assert "Reviewed target commit: " + "c" * 40 in rendered
    assert "Current target branch commit: " + "d" * 40 in rendered
    assert "Approval decision: review-1 (now accepted) by reviewer" in rendered
    with pytest.raises(ValueError, match="approval inspection"):
        _format_inspection({**approved, "approval": {**approved["approval"], "target_sha": "x"}})


def test_text_inspection_escapes_terminal_controls_in_patch() -> None:
    patch = "diff --git a/a b/a\n+\x1b[31m\u202eevil\u2028\n"
    packet = _inspection_packet(patch)
    rendered = _format_inspection(packet)
    assert "\\x1b[31m\\u202eevil\\u2028" in rendered
    assert "\x1b" not in rendered
    assert "\u202e" not in rendered
    altered = {**packet, "intent": {**packet["intent"], "problem": "Bad \x1b[31m title"}}
    rendered = _format_inspection(altered)
    assert "Bad \\x1b[31m title" in rendered
    assert "\x1b" not in rendered
