#!/usr/bin/env python3
"""Collect fork-sync + upstream-MIG data for the daily digest.

Single responsibility: **collect**. No rendering, no email assembly —
those live in render_digest.py and the workflow's email step.

Reads `.github/forks.yml`, then for each fork:
  1. POSTs /repos/{repo}/merge-upstream for every branch in `branches:`.
  2. (If `upstream_org` is set) queries upstream for `[MIG]` PRs that
     merged in the last 24h on `upstream_track`.

Writes three JSON files in CWD which render_digest.py consumes:
  - forks-parsed.json        the parsed forks.yml (also consumed by
                             distribute_forward_port.py)
  - sync-results.json        list of per-branch sync outcomes
  - mig-buckets.json         {"OCA/repo@track": [pr, ...]}

Auth: `GH_TOKEN` — fine-grained PAT, resource owner `ledoent` org,
Contents:write + Workflows:write + public_repo.
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _github import make_headers, request, require_token

HEADERS = make_headers(require_token(), user_agent="ledoent-fork-digest/2.0")


def gh(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    return request(method, path, headers=HEADERS, body=body)


def load_forks(path: str | Path = ".github/forks.yml") -> list[dict]:
    """Parse forks.yml — block-style entries only.

    The format is restricted enough that we don't pull in PyYAML; one
    `- repo:` line per entry, followed by indented `key: value` lines
    until the next entry or a blank line. Lists are written as
    `[item, item]`. Comments stripped.
    """
    text = Path(path).read_text()
    out: list[dict] = []
    cur: dict | None = None

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            if cur:
                out.append(cur)
                cur = None
            continue

        m_new = re.match(r"^  - repo:\s*(.+?)\s*$", line)
        if m_new:
            if cur:
                out.append(cur)
            cur = {"repo": m_new.group(1).strip('"')}
            continue

        m_kv = re.match(r"^    ([a-z_]+):\s*(.+?)\s*$", line)
        if m_kv and cur is not None:
            k, v = m_kv.group(1), m_kv.group(2)
            if v.startswith("[") and v.endswith("]"):
                cur[k] = [s.strip().strip('"') for s in v[1:-1].split(",") if s.strip()]
            elif v == "null":
                cur[k] = None
            elif v in ("true", "false"):
                cur[k] = v == "true"
            else:
                cur[k] = v.strip('"')

    if cur:
        out.append(cur)
    return out


def sync_branch(repo: str, branch: str) -> dict:
    status, body = gh("POST", f"/repos/{repo}/merge-upstream", {"branch": branch})
    msg = body.get("message", "")
    # "Branch n/a" responses we expect on forks that haven't cut a 20.0
    # (or any future release) yet. Two API shapes for the same condition:
    #   422 + "does not exist" — branch isn't on the fork at all
    #   404 + "Branch not found" — branch isn't on the upstream either
    # Both are "skipped", not "failed".
    skipped = (status == 422 and "does not exist" in msg.lower()) or (
        status == 404 and "branch not found" in msg.lower()
    )
    return {
        "repo": repo,
        "branch": branch,
        "status": status,
        "message": msg,
        "merge_type": body.get("merge_type"),
        "skipped": skipped,
    }


def list_recent_mig_prs(
    org: str, repo_name: str, track: str, since: datetime
) -> list[dict]:
    q = (
        f"repo:{org}/{repo_name} is:pr is:merged base:{track} "
        f"merged:>={since.strftime('%Y-%m-%dT%H:%M:%SZ')} "
        f"in:title [MIG]"
    )
    status, body = gh("GET", f"/search/issues?q={urllib.parse.quote(q)}&per_page=20")
    if status != 200:
        return []
    return [
        {
            "title": item["title"],
            "url": item["html_url"],
            "number": item["number"],
            "merged_at": item.get("closed_at"),
            "user": item.get("user", {}).get("login"),
        }
        for item in body.get("items", [])
    ]


def main() -> int:
    forks = load_forks()
    print(f"Loaded {len(forks)} forks from .github/forks.yml", file=sys.stderr)
    Path("forks-parsed.json").write_text(json.dumps(forks, indent=2))

    sync_results: list[dict] = []
    for f in forks:
        for branch in f.get("branches", []):
            res = sync_branch(f["repo"], branch)
            sync_results.append(res)
            tag = "SKIP" if res["skipped"] else (
                res.get("merge_type") or str(res["status"])
            )
            print(f"  {res['repo']}@{branch:>10}  -> {tag}", file=sys.stderr)
    Path("sync-results.json").write_text(json.dumps(sync_results, indent=2))

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    mig_buckets: dict[str, list[dict]] = {}
    seen_upstream: set[tuple[str, str]] = set()
    for f in forks:
        if not f.get("upstream_org") or not f.get("upstream_track"):
            continue
        repo_name = f["repo"].split("/", 1)[1]
        key = (f["upstream_org"], repo_name)
        if key in seen_upstream:
            continue
        seen_upstream.add(key)
        prs = list_recent_mig_prs(
            f["upstream_org"], repo_name, f["upstream_track"], since
        )
        if prs:
            mig_buckets[f"{f['upstream_org']}/{repo_name}@{f['upstream_track']}"] = prs
    Path("mig-buckets.json").write_text(json.dumps(mig_buckets, indent=2))

    print(
        f"Wrote sync-results.json ({len(sync_results)} entries) + "
        f"mig-buckets.json ({sum(len(v) for v in mig_buckets.values())} PRs)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
