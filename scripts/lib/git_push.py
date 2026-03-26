"""Commit and push JSON changes from Actions (or opt-in locally)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path


def commit_and_push(repo_root: Path, paths: list[str], message: str) -> bool:
    """
    Stage paths, commit if dirty, push to origin.
    Returns True if a commit was created and push attempted.
    """
    subprocess.run(
        ["git", "config", "user.name", "github-actions"],
        cwd=repo_root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "github-actions@users.noreply.github.com"],
        cwd=repo_root,
        check=True,
    )
    for p in paths:
        subprocess.run(["git", "add", p], cwd=repo_root, check=True)

    st = subprocess.run(["git", "diff", "--staged", "--quiet"], cwd=repo_root)
    if st.returncode == 0:
        return False

    subprocess.run(["git", "commit", "-m", message], cwd=repo_root, check=True)
    subprocess.run(["git", "push", "origin", "HEAD"], cwd=repo_root, check=True)
    return True


def should_auto_push() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("GIT_PUSH") == "1"
