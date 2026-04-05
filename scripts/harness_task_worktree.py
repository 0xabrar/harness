#!/usr/bin/env python3
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from harness_artifacts import HarnessError


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    if check and completed.returncode != 0:
        raise HarnessError(completed.stderr.strip() or f"git {' '.join(args)} failed")
    return completed


def git_head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def task_branch_name(task_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(task_id)).strip("-") or "task"
    return f"harness/{safe}"


def task_worktree_path(repo: Path, task_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(task_id)).strip("-") or "task"
    return repo / ".harness-worktrees" / safe


def prepare_task_worktree(
    *,
    repo: Path,
    task_id: str,
    branch_name: str = "",
    worktree_path: str = "",
    base_commit: str = "",
) -> dict[str, str]:
    branch = branch_name or task_branch_name(task_id)
    worktree = Path(worktree_path) if worktree_path else task_worktree_path(repo, task_id)
    base = base_commit or git_head(repo)
    worktree.parent.mkdir(parents=True, exist_ok=True)

    if worktree.exists() and not (worktree / ".git").exists():
        shutil.rmtree(worktree)

    if not worktree.exists():
        branch_exists = bool(_git(repo, "branch", "--list", branch).stdout.strip())
        if branch_exists:
            _git(repo, "worktree", "add", str(worktree), branch)
        else:
            _git(repo, "worktree", "add", "-b", branch, str(worktree), base)

    return {
        "branch_name": branch,
        "worktree_path": str(worktree),
        "base_commit": base,
    }


def reset_task_worktree(*, repo: Path, worktree_path: str, base_commit: str) -> None:
    worktree = Path(worktree_path)
    if not worktree.exists():
        return
    _git(worktree, "reset", "--hard", base_commit)
    _git(worktree, "clean", "-fd")


def remove_task_worktree(*, repo: Path, branch_name: str = "", worktree_path: str = "") -> None:
    if worktree_path:
        worktree = Path(worktree_path)
        if worktree.exists():
            _git(repo, "worktree", "remove", "--force", str(worktree), check=False)
            if worktree.exists():
                shutil.rmtree(worktree, ignore_errors=True)
    if branch_name:
        _git(repo, "branch", "-D", branch_name, check=False)


def cherry_pick_commit(*, repo: Path, commit: str) -> str:
    completed = _git(repo, "cherry-pick", commit, check=False)
    if completed.returncode != 0:
        _git(repo, "cherry-pick", "--abort", check=False)
        raise HarnessError(completed.stderr.strip() or f"Failed to cherry-pick commit {commit}")
    return git_head(repo)
