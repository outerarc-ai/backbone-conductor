"""Shared safety checks for remote role-specific HTTP clients."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import httpx

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")


def server_url(value: str, label: str) -> str:
    parts = urlsplit(value)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"{label} URL has an invalid port") from exc
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.path not in {"", "/"}
        or parts.query
        or parts.fragment
        or port == 0
    ):
        raise ValueError(f"{label} URL must be an HTTP(S) server origin")
    if parts.scheme == "http" and parts.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError(f"Remote {label.lower()} access requires HTTPS outside loopback")
    return value.rstrip("/")


def identifier(value: str, label: str) -> str:
    if not _ID.fullmatch(value):
        raise ValueError(f"{label} command needs a valid Backbone identifier")
    return value


def request_json(
    client: httpx.Client,
    method: str,
    path: str,
    label: str,
    data: dict | None = None,
    params: dict | None = None,
):
    try:
        response = client.request(method, path, json=data, params=params)
    except httpx.RequestError as exc:
        raise ValueError(f"{label} connection failed ({type(exc).__name__})") from exc
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", "Request failed")
        except (ValueError, AttributeError):
            detail = "Request failed"
        raise ValueError(
            f"{label} request failed (HTTP {response.status_code}): {str(detail)[:300]}"
        )
    if not 200 <= response.status_code < 300:
        raise ValueError(f"{label} request failed (HTTP {response.status_code})")
    try:
        return response.json()
    except ValueError as exc:
        raise ValueError(f"{label} server returned invalid JSON") from exc
