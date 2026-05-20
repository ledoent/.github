"""Minimal GitHub REST helper shared by the org-control scripts.

Kept dependency-free (stdlib only) because the workflow runs without
pip and adding a setup-python step for one HTTP call isn't worth it.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


GH = "https://api.github.com"
_USER_AGENT_DEFAULT = "ledoent-org-control/1.0"


def require_token(var: str = "GH_TOKEN") -> str:
    """Read a token from env or exit with a useful pointer.

    Cryptic `KeyError: 'GH_TOKEN'` tracebacks waste a workflow run when
    the secret name is mis-spelled. Print the actionable message and
    exit non-zero instead.
    """
    token = os.environ.get(var)
    if not token:
        sys.stderr.write(
            f"error: {var} is not set in the environment.\n"
            f"In the workflow, ensure the step has\n"
            f"    env:\n"
            f"      {var}: ${{{{ secrets.LEDOENT_FORK_SYNC_TOKEN }}}}\n"
            f"and that the secret exists at the repo or org level.\n"
        )
        sys.exit(2)
    return token


def make_headers(token: str, user_agent: str = _USER_AGENT_DEFAULT) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": user_agent,
    }


def request(
    method: str,
    path: str,
    *,
    headers: dict,
    body: dict | None = None,
    timeout: int = 30,
) -> tuple[int, dict]:
    """Single HTTP call returning (status, parsed_json_or_error_dict)."""
    url = path if path.startswith("http") else f"{GH}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if body:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read().decode()
            return r.status, (json.loads(payload) if payload else {})
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode())
        except Exception:
            payload = {"message": str(e)}
        return e.code, payload
