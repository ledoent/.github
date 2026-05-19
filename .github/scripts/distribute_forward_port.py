#!/usr/bin/env python3
"""Push the forward-port workflow file to every opted-in fork.

Reads `forks-parsed.json` (written by fork_sync_digest.py — the
distributor runs after the digest job in the same workflow run, so the
file is present) and for each entry with `install_forward_port: true`,
writes `.github/workflows/forward-port.yml` on the fork's default branch
via the GitHub Contents API.

Idempotent: if the file already exists with the same content, the API
returns 200 with no change. If content differs, this updates it.

Auth: same `GH_TOKEN` PAT — needs `Contents: write` on each target fork.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

GH = "https://api.github.com"
TOKEN = os.environ["GH_TOKEN"]
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "ledoent-fp-distributor/1.0",
}

TEMPLATE = Path(".github/templates/forward-port.yml")
DEST_PATH = ".github/workflows/forward-port.yml"


def gh(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        f"{GH}{path}", data=data, method=method, headers=HEADERS
    )
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            payload = r.read().decode()
            return r.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = {"message": str(e)}
        return e.code, payload


def push_workflow(repo: str, content_bytes: bytes) -> tuple[str, str]:
    """Return (action, detail) — action ∈ {'created','updated','unchanged','failed'}."""
    # Look up existing file (and its sha, needed for update).
    status, existing = gh("GET", f"/repos/{repo}/contents/{DEST_PATH}")
    if status == 200 and existing.get("type") == "file":
        current = base64.b64decode(existing["content"]).decode()
        if current == content_bytes.decode():
            return ("unchanged", existing["sha"][:7])
        sha = existing["sha"]
        op = "updated"
    elif status == 404:
        sha = None
        op = "created"
    else:
        return ("failed", f"GET {status}: {existing.get('message','')}")

    body = {
        "message": (
            f"chore(ci): {op} forward-port.yml from ledoent/.github distributor"
        ),
        "content": base64.b64encode(content_bytes).decode(),
    }
    if sha:
        body["sha"] = sha

    status, payload = gh("PUT", f"/repos/{repo}/contents/{DEST_PATH}", body)
    if status not in (200, 201):
        return ("failed", f"PUT {status}: {payload.get('message','')}")
    return (op, payload.get("commit", {}).get("sha", "")[:7])


def main() -> int:
    forks = json.loads(Path("forks-parsed.json").read_text())
    opted_in = [f for f in forks if f.get("install_forward_port")]
    template = TEMPLATE.read_bytes()
    print(f"Distributing forward-port.yml to {len(opted_in)} forks", file=sys.stderr)

    results: list[dict] = []
    for f in opted_in:
        action, detail = push_workflow(f["repo"], template)
        results.append({"repo": f["repo"], "action": action, "detail": detail})
        print(f"  {f['repo']:<40} {action:<10} {detail}", file=sys.stderr)

    Path("forward-port-distribution.json").write_text(json.dumps(results, indent=2))
    fail = sum(1 for r in results if r["action"] == "failed")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
