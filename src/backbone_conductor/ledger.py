"""Optional metadata-only Git branch in a hidden linked worktree."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from filelock import Timeout

from .models import TaskStatus
from .storage import GitStore, StorageError

LEDGER_BRANCH = "backbone"


def ledger_path(source: GitStore) -> Path:
    common = source._git("rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return Path(common).resolve() / "backbone-ledger"


def open_ledger(source: GitStore, branch: str) -> GitStore:
    if branch != LEDGER_BRANCH:
        raise StorageError(f"Only the {LEDGER_BRANCH!r} ledger branch is supported")
    path = ledger_path(source)
    if not path.is_dir():
        raise StorageError(
            "Backbone ledger worktree is missing; run ledger create or ledger attach"
        )
    ledger = GitStore(path)
    current = ledger._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if current.returncode or current.stdout.strip() != branch:
        raise StorageError("Ledger worktree is not on the backbone branch")
    if ledger.git_dir == source.git_dir:
        raise StorageError("Ledger and source must use distinct Git worktrees")
    return ledger


def _ensure_new_branch(source: GitStore) -> Path:
    path = ledger_path(source)
    if path.exists():
        raise StorageError(f"Ledger worktree already exists: {path}")
    if (
        source._git(
            "show-ref", "--verify", "--quiet", f"refs/heads/{LEDGER_BRANCH}", check=False
        ).returncode
        == 0
    ):
        raise StorageError("Backbone branch already exists; use ledger attach")
    remote_branches = source._git(
        "for-each-ref", "--format=%(refname)", "refs/remotes"
    ).stdout.splitlines()
    if any(ref.endswith(f"/{LEDGER_BRANCH}") for ref in remote_branches):
        raise StorageError("A remote Backbone branch is available; use ledger attach")
    return path


def create_ledger(repo: str | Path) -> dict[str, Any]:
    """Create a metadata-only orphan branch without altering the source checkout."""
    source = GitStore(repo)
    path = _ensure_new_branch(source)
    if (source.path / "state.json").exists():
        raise StorageError("Existing inline Backbone state needs an explicit migration")
    if source._head() is None:
        raise StorageError("Commit source code before creating a separate Backbone branch")
    source_head = source._head()
    source._git("worktree", "add", "--detach", str(path), source_head)
    try:
        ledger = GitStore(path)
        ledger._git("switch", "--orphan", LEDGER_BRANCH)
        state = ledger.init()
        if source._head() != source_head:
            raise StorageError("Source branch changed during ledger creation")
        return {"branch": LEDGER_BRANCH, "worktree": str(path), "version": state.version}
    except BaseException:
        source._git("worktree", "remove", "--force", str(path), check=False)
        raise


def migrate_ledger(repo: str | Path) -> dict[str, Any]:
    """Move a quiescent inline ledger to a metadata-only branch preserving ancestry."""
    source = GitStore(repo)
    try:
        with source._lock:
            path = _ensure_new_branch(source)
            state = source._read()
            active = [
                task.id
                for task in state.tasks.values()
                if task.status not in {TaskStatus.MERGED, TaskStatus.CANCELLED}
            ]
            if active:
                raise StorageError(
                    "Finish or cancel active tasks before migration: " + ", ".join(active)
                )
            if source._git("status", "--porcelain=v1", "--untracked-files=all").stdout:
                raise StorageError("Migration requires a clean source worktree and index")
            current = source._git("symbolic-ref", "--quiet", "--short", "HEAD", check=False)
            if current.returncode:
                raise StorageError("Migration requires a checked-out source branch")
            source_branch = current.stdout.strip()
            source_head = source._head()
            if source_head is None:
                raise StorageError("Migration requires a source commit")
            original_views = {
                f".backbone/{name}": content for name, content in source._render(state).items()
            }
            tracked = set(
                path
                for path in source._git("ls-files", "-z", "--", ".backbone").stdout.split("\x00")
                if path
            )
            if tracked != set(original_views):
                raise StorageError("Inline metadata has unexpected tracked files; inspect it first")
            migrated = state.model_copy(deep=True)
            migrated.parent_version = state.version
            migrated.merged_parent_version = None
            migrated.version = None
            views = {f".backbone/{name}": value for name, value in source._render(migrated).items()}
            with tempfile.TemporaryDirectory(
                prefix="backbone-migrate-", dir=source.git_dir
            ) as temp:
                index_env = {"GIT_INDEX_FILE": str(Path(temp) / "index")}
                source._git("read-tree", "--empty", env=index_env)
                for name, content in views.items():
                    blob = source._git("hash-object", "-w", "--stdin", input=content).stdout.strip()
                    source._git(
                        "update-index",
                        "--add",
                        "--cacheinfo",
                        f"100644,{blob},{name}",
                        env=index_env,
                    )
                tree = source._git("write-tree", env=index_env).stdout.strip()
            branch_commit = source._git(
                "commit-tree",
                *source._signing_args(),
                tree,
                "-p",
                source_head,
                input="backbone: migrate inline ledger\n",
            ).stdout.strip()
            if (
                source._head() != source_head
                or source._git("status", "--porcelain=v1", "--untracked-files=all").stdout
            ):
                raise StorageError("Source checkout changed during migration; retry")
            source._git("update-ref", f"refs/heads/{LEDGER_BRANCH}", branch_commit, "")
            try:
                source._git("rm", "-r", "--", ".backbone")
                source._git(
                    "commit",
                    "--only",
                    "-m",
                    "backbone: remove migrated inline ledger",
                    "--",
                    ".backbone",
                )
            except BaseException:
                if source._head() == source_head:
                    source._git(
                        "restore",
                        "--source=HEAD",
                        "--staged",
                        "--worktree",
                        "--",
                        ".backbone",
                        check=False,
                    )
                    source._git(
                        "update-ref",
                        "-d",
                        f"refs/heads/{LEDGER_BRANCH}",
                        branch_commit,
                        check=False,
                    )
                raise
            source_after = source._head()
            for directory in sorted(
                (item for item in source.path.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                source.path.rmdir()
            except OSError:
                pass
            try:
                source._git("worktree", "add", str(path), LEDGER_BRANCH)
                ledger = open_ledger(source, LEDGER_BRANCH)
                verified = ledger.read()
            except BaseException as exc:
                raise StorageError(
                    "Migration commits were created but ledger worktree attachment failed; "
                    "run ledger attach to repair"
                ) from exc
            return {
                "branch": LEDGER_BRANCH,
                "worktree": str(path),
                "version": verified.version,
                "previous_version": state.version,
                "source_branch": source_branch,
                "source_before": source_head,
                "source_after": source_after,
            }
    except Timeout as exc:
        raise StorageError("Timed out waiting for the Backbone repository lock") from exc


def attach_ledger(repo: str | Path, remote: str = "origin") -> dict[str, Any]:
    """Attach the existing local or remote metadata branch to this clone."""
    source = GitStore(repo)
    path = ledger_path(source)
    if path.exists():
        raise StorageError(f"Ledger worktree already exists: {path}")
    if source._git(
        "show-ref", "--verify", "--quiet", f"refs/heads/{LEDGER_BRANCH}", check=False
    ).returncode:
        remotes = source._git("remote").stdout.splitlines()
        if remote.startswith("-") or remote not in remotes:
            raise StorageError(f"Unknown Git remote: {remote!r}")
        source._git(
            "fetch",
            "--no-tags",
            remote,
            f"refs/heads/{LEDGER_BRANCH}:refs/heads/{LEDGER_BRANCH}",
            timeout=120,
        )
    tracked = source._git("ls-tree", "-r", "--name-only", LEDGER_BRANCH).stdout.splitlines()
    if not tracked or any(not item.startswith(".backbone/") for item in tracked):
        raise StorageError("Backbone branch must contain metadata files only")
    source._git("worktree", "add", str(path), LEDGER_BRANCH)
    try:
        ledger = open_ledger(source, LEDGER_BRANCH)
        state = ledger.read()
        return {"branch": LEDGER_BRANCH, "worktree": str(path), "version": state.version}
    except BaseException:
        source._git("worktree", "remove", "--force", str(path), check=False)
        raise
