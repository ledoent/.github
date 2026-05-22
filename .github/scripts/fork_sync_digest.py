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
        # Blank-line check uses the ORIGINAL line, before comment
        # stripping. Otherwise comment-only lines inside an entry
        # (e.g. an indented `# Default branch is …` between two keys)
        # look identical to a true blank separator and silently
        # truncate the entry. Comment lines are a no-op; only truly
        # empty/whitespace-only lines terminate an entry.
        if not raw.strip():
            if cur:
                out.append(cur)
                cur = None
            continue
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue  # comment-only line — keep accumulating into cur

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


def sync_branch(repo: str, branch: str, upstream_org: str | None = None) -> dict:
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
    result = {
        "repo": repo,
        "branch": branch,
        "status": status,
        "message": msg,
        "merge_type": body.get("merge_type"),
        "skipped": skipped,
        "ahead_by": None,
        "behind_by": None,
        "diverged": False,
    }
    # Post-sync divergence check. merge-upstream silently produces a
    # merge commit (instead of fast-forwarding) when the fork has local
    # commits the upstream doesn't have — returning success either way.
    # Without this check, accidental pushes to a series-named branch on
    # the fork (e.g. someone "Merge pull request"-ing a feature branch
    # into 18.0) accumulate undetected and the daily sync keeps adding
    # merge commits on top. Compare against upstream to surface drift.
    if upstream_org and not skipped and status < 400:
        ahead, behind = _compare_with_upstream(repo, upstream_org, branch)
        result["ahead_by"] = ahead
        result["behind_by"] = behind
        result["diverged"] = bool(ahead and ahead > 0)
    return result


def _compare_with_upstream(
    fork_repo: str, upstream_org: str, branch: str
) -> tuple[int | None, int | None]:
    """Return (ahead_by, behind_by) for fork branch vs upstream branch.

    ahead_by > 0 means the fork has commits the upstream doesn't —
    almost always a mistake on a series-named tracking branch.
    Returns (None, None) on any API error so a flaky compare doesn't
    mask a successful sync.
    """
    fork_owner = fork_repo.split("/", 1)[0]
    repo_name = fork_repo.split("/", 1)[1]
    path = (
        f"/repos/{upstream_org}/{repo_name}/compare/"
        f"{upstream_org}:{branch}...{fork_owner}:{branch}"
    )
    status, body = gh("GET", path)
    if status != 200:
        return None, None
    return body.get("ahead_by"), body.get("behind_by")


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
            res = sync_branch(f["repo"], branch, upstream_org=f.get("upstream_org"))
            sync_results.append(res)
            tag = "SKIP" if res["skipped"] else (
                res.get("merge_type") or str(res["status"])
            )
            if res.get("diverged"):
                tag = f"{tag} DIVERGED+{res['ahead_by']}"
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
