"""Commit guard: scan the staged changes for secrets and private data before every commit.

Blocks (exit 1): values copied from .env, token and private-key shapes, personal emails, local user paths, and
files that must stay private (.env, eval answer keys and audit files, the eval truth, per-instance run files,
hand-check sheets). Prints the kind and file:line only, never the matching value.

    python3 scripts/precommit_scan.py        # scans `git diff --cached`; installed as .githooks/pre-commit
"""

from __future__ import annotations

import fnmatch
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PRIVATE_PATHS = (".env", "data/keys/*", "data/audit/*", "data/deals.json", "results/handcheck/*",
                 "results/*_runs.json", "results/archive/*/*_runs.json", "*.sqlite", "*.db")
TOKEN_SHAPES = {
    "API or bot token": re.compile(r"xox[abprs]-[0-9A-Za-z-]{10,}|xapp-\d-[0-9A-Za-z-]{10,}|sk-[A-Za-z0-9_-]{20,}"
                                   r"|pat-[a-z]{2}\d-[0-9a-f-]{20,}|AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{30,}"),
    "private key": re.compile(r"BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY"),
    "local user path": re.compile(r"/Users/[A-Za-z0-9._-]+/|C:\\Users\\[A-Za-z0-9._-]+\\"),
}
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[a-z]{2,})")
OK_EMAIL_DOMAINS = ("example", "anthropic.com", "users.noreply.github.com", "noreply.github.com")


def env_secrets(env_file: Path) -> list:
    """Secret-looking values from .env (length 8 or more, key name suggests a credential)."""
    out = []
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                v = v.split("#")[0].strip()
                if len(v) >= 8 and re.search(r"KEY|TOKEN|SECRET|PASSWORD", k, re.I):
                    out.append(v)
    return out


def forbidden_paths(paths: list) -> list:
    return [p for p in paths if any(fnmatch.fnmatch(p, pat) for pat in PRIVATE_PATHS) and p != ".env.example"]


def scan_line(line: str, secrets: list) -> list:
    hits = [kind for kind, rx in TOKEN_SHAPES.items() if rx.search(line)]
    if any(s in line for s in secrets):
        hits.append("value copied from .env")
    for m in EMAIL.finditer(line):
        if not m.group(1).lower().endswith(OK_EMAIL_DOMAINS):
            hits.append("personal email address")
            break
    return hits


def parse_added_lines(diff: str) -> list:
    """[(path, line_number, text)] for every added line in a unified diff."""
    out, path, n = [], None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ b/"):
            path = raw[6:]
        elif raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            n = int(m.group(1)) - 1 if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            n += 1
            out.append((path, n, raw[1:]))
        elif not raw.startswith("-"):
            n += 1
    return out


def findings(paths: list, diff: str, secrets: list) -> list:
    out = [f"{p}: private file must not be committed" for p in forbidden_paths(paths)]
    for path, n, text in parse_added_lines(diff):
        for kind in scan_line(text, secrets):
            out.append(f"{path}:{n}: {kind}")
    return out


def main() -> int:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout

    paths = git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines()
    found = findings(paths, git("diff", "--cached", "-U0", "--no-color"), env_secrets(ROOT / ".env"))
    email = git("config", "user.email").strip()
    if email and not email.lower().endswith(("noreply.github.com", "users.noreply.github.com")):
        print("note: commits will carry your git author email; GitHub's noreply address keeps it private")
    if found:
        print("commit blocked by precommit_scan:\n  " + "\n  ".join(found))
        return 1
    print(f"precommit_scan: {len(paths)} staged file(s) clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
