"""Validate and combine bounded, HEAD-pinned metadata history pages."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def validate_history_page(
    report: object, limit: int, offset: int = 0, expected_head: str | None = None
) -> dict[str, Any]:
    """Reject malformed or internally inconsistent coordinator reports."""
    error = ValueError("Invalid history page")
    if type(limit) is not int or not 1 <= limit <= 1000 or type(offset) is not int or offset < 0:
        raise error
    if not isinstance(report, dict):
        raise error
    required = {
        "head",
        "limit",
        "offset",
        "checked",
        "total_metadata_commits",
        "truncated",
        "next_offset",
        "invalid",
        "page_ok",
        "ok",
        "commits",
    }
    if not required <= report.keys():
        raise error
    head = report["head"]
    if not isinstance(head, str) or _COMMIT.fullmatch(head) is None:
        raise error
    if expected_head is not None and head != expected_head:
        raise error
    for key in ("limit", "offset", "checked", "total_metadata_commits", "invalid"):
        if type(report[key]) is not int or report[key] < 0:
            raise error
    total = report["total_metadata_commits"]
    checked = report["checked"]
    end = offset + checked
    truncated = total > end
    next_offset = report["next_offset"]
    if (
        total == 0
        or offset >= total
        or report["limit"] != limit
        or report["offset"] != offset
        or checked != min(limit, total - offset)
        or type(report["truncated"]) is not bool
        or report["truncated"] != truncated
        or (type(next_offset) is not int if truncated else next_offset is not None)
        or next_offset != (end if truncated else None)
        or type(report["page_ok"]) is not bool
        or type(report["ok"]) is not bool
        or not isinstance(report["commits"], list)
        or len(report["commits"]) != checked
    ):
        raise error
    failures = 0
    seen: set[str] = set()
    for entry in report["commits"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("commit"), str)
            or _COMMIT.fullmatch(entry["commit"]) is None
            or entry["commit"] in seen
            or type(entry.get("ok")) is not bool
        ):
            raise error
        seen.add(entry["commit"])
        if "error" in entry:
            if entry["ok"] or not isinstance(entry["error"], str):
                raise error
        else:
            for key in ("missing_views", "extra_views", "changed_views"):
                value = entry.get(key)
                if not isinstance(value, list) or any(not isinstance(path, str) for path in value):
                    raise error
            links_ok = entry.get("parent_links_ok")
            if type(links_ok) is not bool or entry["ok"] != (
                not any(entry[key] for key in ("missing_views", "extra_views", "changed_views"))
                and links_ok
            ):
                raise error
        failures += not entry["ok"]
    if (
        report["invalid"] != failures
        or report["page_ok"] != (failures == 0)
        or report["ok"] != (failures == 0 and offset == 0 and not truncated)
    ):
        raise error
    return report


def verify_all_history_pages(
    fetch: Callable[[int, str | None], dict[str, Any]], limit: int
) -> dict[str, Any]:
    """Inspect every page while retaining only failures in the final report."""
    offset = 0
    head: str | None = None
    total: int | None = None
    failures: list[dict[str, Any]] = []
    seen: set[str] = set()
    pages = 0
    while True:
        page = validate_history_page(fetch(offset, head), limit, offset, head)
        if total is not None and page["total_metadata_commits"] != total:
            raise ValueError("Metadata history changed between pages; restart verification")
        commits = {entry["commit"] for entry in page["commits"]}
        if commits & seen:
            raise ValueError("Metadata history repeated commits between pages")
        seen.update(commits)
        head = page["head"]
        total = page["total_metadata_commits"]
        failures.extend(entry for entry in page["commits"] if not entry["ok"])
        pages += 1
        if not page["truncated"]:
            return {
                "head": head,
                "page_size": limit,
                "checked": total,
                "total_metadata_commits": total,
                "pages": pages,
                "invalid": len(failures),
                "ok": not failures,
                "invalid_commits": failures,
            }
        offset = page["next_offset"]
