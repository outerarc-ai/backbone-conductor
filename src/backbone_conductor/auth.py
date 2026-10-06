"""Opt-in bearer authentication for the HTTP transport.

Credentials live outside the Git ledger. The file contains only SHA-256 digests
of randomly generated, high-entropy bearer tokens, never plaintext tokens.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock, Timeout

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.@-]{0,199}\Z")


def _private_credential_directory(path: Path) -> None:
    metadata = path.stat()
    if not stat.S_ISDIR(metadata.st_mode) or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise ValueError("HTTP credential directory must be private (no group/other access)")
    uid = os.geteuid() if hasattr(os, "geteuid") else None
    if uid not in (None, 0) and metadata.st_uid != uid:
        raise ValueError("HTTP credential directory must belong to this user")


@dataclass(frozen=True)
class Principal:
    name: str
    role: str


class TokenAuth:
    """Verify tokens against a private, strictly validated credential file."""

    def __init__(self, path: str | Path, repo: str | Path) -> None:
        self.location = Path(path).expanduser().absolute()
        self.repository = Path(repo).expanduser().resolve()
        self.check_available()

    def _load(self) -> tuple[tuple[str, Principal], ...]:
        try:
            if self.location.is_symlink():
                raise ValueError("HTTP credential file must be a regular file")
            resolved = self.location.resolve(strict=True)
            if resolved.is_relative_to(self.repository):
                raise ValueError("HTTP credential file must be outside the repository")
            _private_credential_directory(self.location.parent)
            descriptor = os.open(
                self.location,
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("HTTP credential file must be a regular file")
                if metadata.st_nlink != 1:
                    raise ValueError("HTTP credential file must not have hard links")
                uid = os.geteuid() if hasattr(os, "geteuid") else None
                if uid not in (None, 0) and metadata.st_uid != uid:
                    raise ValueError("HTTP credential file must belong to this user")
                if metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                    raise ValueError(
                        "HTTP credential file must be accessible only to its owner (0600)"
                    )
                if metadata.st_size > 1_048_576:
                    raise ValueError("HTTP credential file is too large")
                content = stream.read(1_048_577)
                if len(content) > 1_048_576:
                    raise ValueError("HTTP credential file is too large")
            config = json.loads(content)
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"Invalid HTTP credential file: {exc}") from exc
        if not isinstance(config, dict) or set(config) != {"tokens"}:
            raise ValueError("HTTP credential file must contain only a tokens array")
        entries = config["tokens"]
        if not isinstance(entries, list) or not entries:
            raise ValueError("HTTP credential file needs at least one token")
        tokens: list[tuple[str, Principal]] = []
        seen: set[str] = set()
        seen_names: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != {"name", "role", "sha256"}:
                raise ValueError("Each token needs name, role and sha256 only")
            name, role, digest = entry["name"], entry["role"], entry["sha256"]
            if not isinstance(name, str) or not _NAME.fullmatch(name):
                raise ValueError("Invalid HTTP token principal name")
            if name in seen_names:
                raise ValueError("HTTP token principal names must be unique")
            if not isinstance(role, str) or role not in {"admin", "member", "reviewer"}:
                raise ValueError("HTTP token role must be admin, member or reviewer")
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or digest in seen:
                raise ValueError("HTTP token sha256 must be unique lowercase hex")
            seen.add(digest)
            seen_names.add(name)
            tokens.append((digest, Principal(name, role)))
        if not any(principal.role == "admin" for _, principal in tokens):
            raise ValueError("HTTP credential file needs an admin token")
        return tuple(tokens)

    def check_available(self) -> None:
        """Validate the current file without retaining a stale token generation."""
        self._load()

    def authenticate(self, authorization: str | None) -> Principal | None:
        tokens = self._load()
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization[7:]
        if len(token) < 32 or len(token) > 512 or any(character.isspace() for character in token):
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        matched = None
        for expected, principal in tokens:
            if secrets.compare_digest(expected, digest):
                matched = principal
        return matched


def _issue(
    admin: str, members: list[str], reviewers: list[str] | None = None
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    principals = [
        (admin, "admin"),
        *((member, "member") for member in members),
        *((reviewer, "reviewer") for reviewer in reviewers or []),
    ]
    names = [name for name, _ in principals]
    if any(not _NAME.fullmatch(name) for name in names) or len(set(names)) != len(names):
        raise ValueError("Token principal names must be unique and use safe characters")
    issued = [
        {"name": name, "role": role, "token": secrets.token_urlsafe(48)}
        for name, role in principals
    ]
    digests = [
        {
            "name": item["name"],
            "role": item["role"],
            "sha256": hashlib.sha256(item["token"].encode("utf-8")).hexdigest(),
        }
        for item in issued
    ]
    return issued, digests


def _write_digests(descriptor: int, digests: list[dict[str, str]]) -> None:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({"tokens": digests}, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _new_credential(principal: Principal) -> tuple[dict[str, str], dict[str, str]]:
    issued = {
        "name": principal.name,
        "role": principal.role,
        "token": secrets.token_urlsafe(48),
    }
    digest = {
        "name": principal.name,
        "role": principal.role,
        "sha256": hashlib.sha256(issued["token"].encode("utf-8")).hexdigest(),
    }
    return issued, digest


def _matches_created_file(path: Path, descriptor: int) -> bool:
    try:
        current = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return False
    original = os.fstat(descriptor)
    return (current.st_dev, current.st_ino) == (original.st_dev, original.st_ino)


def create_token_file(
    path: str | Path,
    repo: str | Path,
    admin: str,
    members: list[str],
    reviewers: list[str] | None = None,
) -> list[dict[str, str]]:
    """Create a private digest file and return one-time plaintext credentials."""
    repository = Path(repo).expanduser().resolve()
    location = Path(path).expanduser().absolute()
    if location.is_symlink():
        raise ValueError("HTTP credential file must not be a symlink")
    location = location.resolve()
    if location.is_relative_to(repository):
        raise ValueError("HTTP credential file must be outside the repository")
    _private_credential_directory(location.parent)
    issued, digests = _issue(admin, members, reviewers)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{location.name}.create-", dir=location.parent
    )
    temporary = Path(temporary_name)
    published = False
    try:
        _write_digests(os.dup(descriptor), digests)
        TokenAuth(temporary, repository)
        expected = (json.dumps({"tokens": digests}, indent=2) + "\n").encode("utf-8")
        os.lseek(descriptor, 0, os.SEEK_SET)
        if (
            not _matches_created_file(temporary, descriptor)
            or os.read(descriptor, len(expected) + 1) != expected
        ):
            raise ValueError("HTTP credential temporary file changed during creation")
        os.link(temporary, location)
        published = True
        if not _matches_created_file(temporary, descriptor):
            raise ValueError("HTTP credential temporary file changed during creation")
        temporary.unlink()
        TokenAuth(location, repository)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if (
            not _matches_created_file(location, descriptor)
            or os.read(descriptor, len(expected) + 1) != expected
            or not _matches_created_file(location, descriptor)
        ):
            raise ValueError("HTTP credential file changed during creation")
    except BaseException:
        if published and _matches_created_file(location, descriptor):
            location.unlink()
        raise
    finally:
        try:
            if _matches_created_file(temporary, descriptor):
                temporary.unlink()
        finally:
            os.close(descriptor)
    return issued


def _rotate_file(
    path: str | Path,
    repo: str | Path,
    issue: Callable[
        [tuple[tuple[str, Principal], ...]],
        tuple[list[dict[str, str]], list[dict[str, str]]],
    ],
) -> list[dict[str, str]]:
    """Serialize cooperating writers and atomically publish one token generation."""
    repository = Path(repo).expanduser().resolve()
    location = Path(path).expanduser().absolute()
    if location.resolve().is_relative_to(repository):
        raise ValueError("HTTP credential file must be outside the repository")
    _private_credential_directory(location.parent)
    try:
        with FileLock(str(location) + ".lock", timeout=10, mode=0o600, preserve_lock_file=True):
            entries = TokenAuth(location, repository)._load()
            previous = location.stat(follow_symlinks=False)
            issued, digests = issue(entries)
            descriptor, temporary = tempfile.mkstemp(
                prefix=f".{location.name}.rotate-", dir=location.parent
            )
            try:
                _write_digests(descriptor, digests)
                TokenAuth(temporary, repository)
                current = location.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size) != (
                    previous.st_dev,
                    previous.st_ino,
                    previous.st_mtime_ns,
                    previous.st_size,
                ):
                    raise ValueError("HTTP credential file changed during rotation; retry")
                os.replace(temporary, location)
            finally:
                Path(temporary).unlink(missing_ok=True)
            return issued
    except Timeout as exc:
        raise ValueError("Timed out waiting for HTTP credential rotation") from exc


def rotate_token_file(
    path: str | Path,
    repo: str | Path,
    admin: str,
    members: list[str],
    reviewers: list[str] | None = None,
) -> list[dict[str, str]]:
    """Replace all principals and tokens, revoking the old generation."""
    return _rotate_file(path, repo, lambda _entries: _issue(admin, members, reviewers))


def rotate_principal_token(path: str | Path, repo: str | Path, name: str) -> dict[str, str]:
    """Rotate one existing principal while preserving every other credential."""

    def issue(entries: tuple[tuple[str, Principal], ...]):
        target = next((principal for _, principal in entries if principal.name == name), None)
        if target is None:
            raise ValueError("HTTP principal does not exist")
        issued, replacement = _new_credential(target)
        digests = [
            {
                "name": principal.name,
                "role": principal.role,
                "sha256": replacement["sha256"] if principal.name == name else digest,
            }
            for digest, principal in entries
        ]
        return [issued], digests

    return _rotate_file(path, repo, issue)[0]


def add_principal_token(path: str | Path, repo: str | Path, name: str, role: str) -> dict[str, str]:
    """Issue one new principal without revoking existing credentials."""
    if not _NAME.fullmatch(name):
        raise ValueError("Invalid HTTP token principal name")
    if role not in {"admin", "member", "reviewer"}:
        raise ValueError("HTTP token role must be admin, member or reviewer")

    def issue(entries: tuple[tuple[str, Principal], ...]):
        if any(principal.name == name for _, principal in entries):
            raise ValueError("HTTP principal already exists")
        issued, replacement = _new_credential(Principal(name, role))
        digests = [
            {"name": principal.name, "role": principal.role, "sha256": digest}
            for digest, principal in entries
        ]
        digests.append(replacement)
        return [issued], digests

    return _rotate_file(path, repo, issue)[0]


def revoke_principal_token(path: str | Path, repo: str | Path, name: str) -> dict[str, str]:
    """Remove one principal while keeping at least one administrator."""

    def issue(entries: tuple[tuple[str, Principal], ...]):
        target = next((principal for _, principal in entries if principal.name == name), None)
        if target is None:
            raise ValueError("HTTP principal does not exist")
        if (
            target.role == "admin"
            and sum(principal.role == "admin" for _, principal in entries) == 1
        ):
            raise ValueError("Cannot revoke the last HTTP administrator")
        digests = [
            {"name": principal.name, "role": principal.role, "sha256": digest}
            for digest, principal in entries
            if principal.name != name
        ]
        return [{"name": target.name, "role": target.role}], digests

    return _rotate_file(path, repo, issue)[0]
