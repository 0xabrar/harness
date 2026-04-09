from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_task_worktree import integrate_commit  # noqa: E402


def git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed\nstdout={completed.stdout}\nstderr={completed.stderr}"
        )
    return completed


def setup_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "app.txt").write_text("base\n", encoding="utf-8")
    git(repo, "add", "app.txt")
    git(repo, "commit", "-m", "base")
    return repo


class TaskWorktreeIntegrationTests(unittest.TestCase):
    def test_integrate_commit_classifies_clean_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup_repo(Path(tmp))
            git(repo, "checkout", "-b", "feature")
            (repo / "app.txt").write_text("feature\n", encoding="utf-8")
            git(repo, "commit", "-am", "feature change")
            commit = git(repo, "rev-parse", "HEAD").stdout.strip()

            git(repo, "checkout", "main")
            result = integrate_commit(repo=repo, commit=commit)

            self.assertEqual(result.outcome, "applied")
            self.assertEqual(result.integrated_commit, git(repo, "rev-parse", "HEAD").stdout.strip())
            self.assertEqual((repo / "app.txt").read_text(encoding="utf-8"), "feature\n")

    def test_integrate_commit_classifies_conflict_and_cleans_up(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup_repo(Path(tmp))
            git(repo, "checkout", "-b", "feature")
            (repo / "app.txt").write_text("feature\n", encoding="utf-8")
            git(repo, "commit", "-am", "feature change")
            commit = git(repo, "rev-parse", "HEAD").stdout.strip()

            git(repo, "checkout", "main")
            (repo / "app.txt").write_text("mainline\n", encoding="utf-8")
            git(repo, "commit", "-am", "mainline change")
            main_head = git(repo, "rev-parse", "HEAD").stdout.strip()

            result = integrate_commit(repo=repo, commit=commit)

            self.assertEqual(result.outcome, "conflict")
            self.assertEqual(result.integrated_commit, "")
            self.assertEqual(git(repo, "rev-parse", "HEAD").stdout.strip(), main_head)
            self.assertEqual(git(repo, "status", "--short").stdout.strip(), "")

    def test_integrate_commit_classifies_already_applied(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup_repo(Path(tmp))
            git(repo, "checkout", "-b", "feature")
            (repo / "app.txt").write_text("shared\n", encoding="utf-8")
            git(repo, "commit", "-am", "feature change")
            commit = git(repo, "rev-parse", "HEAD").stdout.strip()

            git(repo, "checkout", "main")
            (repo / "app.txt").write_text("shared\n", encoding="utf-8")
            git(repo, "commit", "-am", "same patch on main")
            main_head = git(repo, "rev-parse", "HEAD").stdout.strip()

            result = integrate_commit(repo=repo, commit=commit)

            self.assertEqual(result.outcome, "already_applied")
            self.assertEqual(result.integrated_commit, "")
            self.assertEqual(git(repo, "rev-parse", "HEAD").stdout.strip(), main_head)
            self.assertEqual(git(repo, "status", "--short").stdout.strip(), "")

    def test_integrate_commit_classifies_fatal_git_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = setup_repo(Path(tmp))

            result = integrate_commit(repo=repo, commit="deadbeef")

            self.assertEqual(result.outcome, "fatal")
            self.assertEqual(result.integrated_commit, "")
            self.assertIn("bad revision", result.detail)


if __name__ == "__main__":
    unittest.main()
