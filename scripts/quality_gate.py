"""Run repository quality checks used by pre-commit and CI."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    name: str
    command: list[str]
    cwd: Path


def run_check(check: Check) -> bool:
    print(f"[quality] {check.name}...", flush=True)
    result = subprocess.run(check.command, cwd=check.cwd)
    if result.returncode == 0:
        print(f"[quality] PASS: {check.name}", flush=True)
        return True
    print(f"[quality] FAIL: {check.name}", file=sys.stderr)
    return False


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    worker = root / "worker"
    npm_bin = "npm.cmd" if os.name == "nt" else "npm"

    checks = [
        Check(
            name="Python compile check",
            command=[sys.executable, "-m", "compileall", "scripts"],
            cwd=root,
        ),
        Check(
            name="Worker TypeScript check",
            command=[npm_bin, "exec", "tsc", "--", "--noEmit"],
            cwd=worker,
        ),
    ]

    failed = False
    for check in checks:
        if not run_check(check):
            failed = True

    if failed:
        print("[quality] One or more checks failed.", file=sys.stderr)
        return 1
    print("[quality] All checks passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

