"""Request-scoped attribution for commits made through authenticated HTTP."""

from __future__ import annotations

from contextvars import ContextVar, Token

_HTTP_ACTOR: ContextVar[tuple[str, str] | None] = ContextVar("backbone_http_actor", default=None)


def bind_http_actor(name: str, role: str) -> Token[tuple[str, str] | None]:
    """Bind the already authenticated request principal until its response completes."""
    return _HTTP_ACTOR.set((name, role))


def reset_http_actor(token: Token[tuple[str, str] | None]) -> None:
    _HTTP_ACTOR.reset(token)


def attributed_message(message: str) -> str:
    """Append a Git trailer without claiming that Git itself verified the token."""
    actor = _HTTP_ACTOR.get()
    if actor is None:
        return message.rstrip("\n") + "\n"
    name, role = actor
    return (
        message.rstrip("\n") + f"\n\nBackbone-HTTP-Principal: {name}\nBackbone-HTTP-Role: {role}\n"
    )
