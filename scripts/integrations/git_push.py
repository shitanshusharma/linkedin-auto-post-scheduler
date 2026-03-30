"""Commit and push JSON changes from Actions (or opt-in locally)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any
from urllib import error, parse, request

from core.constants import GIT_AUTOMATION


def _github_token() -> str:
    return os.environ.get("GH_TOKEN", "").strip() or os.environ.get("GITHUB_TOKEN", "").strip()


def _automation_branch() -> str:
    return os.environ.get("AUTOMATION_BRANCH", "").strip() or GIT_AUTOMATION.DEFAULT_AUTOMATION_BRANCH


def _base_branch() -> str:
    return os.environ.get("AUTOMATION_BASE_BRANCH", "").strip() or GIT_AUTOMATION.DEFAULT_BASE_BRANCH


def _github_api_request(
    *,
    token: str,
    url: str,
    method: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")

    req = request.Request(url=url, data=body, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "linkedin-auto-post-scheduler")
    if body is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with request.urlopen(req, timeout=30) as resp:  # noqa: S310 - GitHub API endpoint
            raw = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"GitHub API failed ({method} {url}): {exc.code} {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"GitHub API network error ({method} {url}): {exc}") from exc

    if not raw:
        return None
    return json.loads(raw)


def _create_or_reuse_pull_request(*, repo: str, token: str, head: str, base: str, title: str) -> str:
    if "/" not in repo:
        raise RuntimeError(f"Invalid GITHUB_REPOSITORY value: {repo}")
    owner, _ = repo.split("/", 1)

    query = parse.urlencode({"state": "open", "head": f"{owner}:{head}", "base": base})
    list_url = f"https://api.github.com/repos/{repo}/pulls?{query}"
    existing = _github_api_request(token=token, url=list_url, method="GET")
    if isinstance(existing, list) and existing:
        html_url = existing[0].get("html_url")
        if isinstance(html_url, str) and html_url:
            return html_url

    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    run_url = ""
    if run_id:
        server_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com").strip()
        run_url = f"{server_url}/{repo}/actions/runs/{run_id}"

    body = "Automated update from scheduler workflow."
    if run_url:
        body += f"\n\nWorkflow run: {run_url}"

    create_url = f"https://api.github.com/repos/{repo}/pulls"
    created = _github_api_request(
        token=token,
        url=create_url,
        method="POST",
        payload={"title": title, "head": head, "base": base, "body": body},
    )
    if isinstance(created, dict):
        html_url = created.get("html_url")
        if isinstance(html_url, str) and html_url:
            return html_url
    raise RuntimeError("Pull request creation did not return an HTML URL")


def _push_head_to_branch(repo_root: Path, branch: str) -> None:
    refspec = f"HEAD:refs/heads/{branch}"
    try:
        subprocess.run(["git", "push", "origin", refspec], cwd=repo_root, check=True)
        return
    except subprocess.CalledProcessError:
        # One retry for non-fast-forward races on the shared automation branch.
        subprocess.run(["git", "fetch", "origin", branch], cwd=repo_root, check=True)
        subprocess.run(["git", "rebase", f"origin/{branch}"], cwd=repo_root, check=True)
        subprocess.run(["git", "push", "origin", refspec], cwd=repo_root, check=True)


def commit_and_push(repo_root: Path, paths: list[str], message: str) -> bool:
    """
    Stage paths, commit if dirty, push to origin.
    Returns True if a commit was created and push attempted.
    """
    in_actions = os.environ.get("GITHUB_ACTIONS") == "true"
    if in_actions:
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

    if in_actions:
        branch = _automation_branch()
        base_branch = _base_branch()
        repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
        token = _github_token()
        if not repo or not token:
            raise RuntimeError("GITHUB_REPOSITORY and GITHUB_TOKEN/GH_TOKEN are required in Actions")

        _push_head_to_branch(repo_root, branch)
        pr_url = _create_or_reuse_pull_request(
            repo=repo,
            token=token,
            head=branch,
            base=base_branch,
            title=message,
        )
        print(f"Opened or reused PR: {pr_url}", flush=True)
    else:
        subprocess.run(["git", "push", "origin", "HEAD"], cwd=repo_root, check=True)

    return True


def should_auto_push() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true" or os.environ.get("GIT_PUSH") == "1"

