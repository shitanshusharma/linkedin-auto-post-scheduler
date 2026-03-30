import os
from pathlib import Path


def repo_root() -> Path:
    """Root of the checkout (GitHub Actions) or repo when running locally."""
    ws = os.environ.get("GITHUB_WORKSPACE")
    if ws:
        return Path(ws)
    return Path(__file__).resolve().parents[2]

