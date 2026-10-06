"""Read a bearer token from an owner-only local file."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_private_token(path: str | Path, label: str) -> str:
    location = Path(path).expanduser().absolute()
    if location.is_symlink():
        raise ValueError(f"{label} token file must be a regular file, not a symlink")
    descriptor = os.open(
        location,
        os.O_RDONLY
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ValueError(f"{label} token file must be a single-link regular file")
        if metadata.st_uid != os.getuid() or metadata.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ValueError(f"{label} token file must belong to this user and be mode 0600")
        raw = stream.read(1025)
    if len(raw) > 1024:
        raise ValueError(f"{label} token file is too large")
    try:
        token = raw.decode("ascii").rstrip("\r\n")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{label} token must be ASCII") from exc
    if not 32 <= len(token) <= 512 or any(character.isspace() for character in token):
        raise ValueError(f"{label} token must be a single 32–512 character value")
    return token
