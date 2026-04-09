#!/usr/bin/env python3
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from harness_artifacts import HarnessError


IntegrationOutcome = Literal["applied", "conflict", "already_applied", "fatal"]


@dataclass(frozen=True)
class IntegrationResult:
    outcome: IntegrationOutcome
    integrated_commit: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def detail(self) -> str:
        parts = [self.stderr.strip(), self.stdout.strip()]
        return "\n".join(part for part in parts if part)


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


def _has_cherry_pick_in_progress(repo: Path) -> bool:
    return _git(repo, "rev-parse", "-q", "--verify", "CHERRY_PICK_HEAD", check=False).returncode == 0


def _cleanup_failed_cherry_pick(repo: Path) -> None:
    if _has_cherry_pick_in_progress(repo):
        _git(repo, "cherry-pick", "--abort", check=False)


def _classify_integration_failure(completed: subprocess.CompletedProcess[str]) -> IntegrationOutcome:
    output = "\n".join((completed.stderr, completed.stdout)).lower()
    if "previous cherry-pick is now empty" in output or "nothing to commit, working tree clean" in output:
        return "already_applied"
    if "could not apply" in output or "after resolving the conflicts" in output or "conflict (" in output:
        return "conflict"
    return "fatal"


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


def integrate_commit(*, repo: Path, commit: str) -> IntegrationResult:
    completed = _git(repo, "cherry-pick", commit, check=False)
    if completed.returncode == 0:
        return IntegrationResult(
            outcome="applied",
            integrated_commit=git_head(repo),
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    outcome = _classify_integration_failure(completed)
    _cleanup_failed_cherry_pick(repo)
    return IntegrationResult(
        outcome=outcome,
        integrated_commit="",
        returncode=completed.returncode,
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def cherry_pick_commit(*, repo: Path, commit: str) -> str:
    result = integrate_commit(repo=repo, commit=commit)
    if result.outcome == "applied":
        return result.integrated_commit
    raise HarnessError(result.detail or f"Failed to cherry-pick commit {commit}")
