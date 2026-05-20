#!/usr/bin/env python3
"""Sync ledoent forks with upstream + emit a daily digest.

Two responsibilities:
  1. For each fork in .github/forks.yml, POST /repos/{repo}/merge-upstream
     for every branch listed in `branches:`. Failures collected into the
     digest. Branches that don't exist on the fork are skipped quietly.
  2. For each fork whose `upstream_org` is set, query upstream for `[MIG]`
     PRs closed in the last 24h on the configured `upstream_track`.

Outputs `digest.html`, `digest.subject`, `digest.exit` in CWD which the
workflow consumes verbatim. Also writes `forks-parsed.json` so the
forward-port-distributor workflow can read the same fork list without
re-parsing the YAML.

Auth: `GH_TOKEN` env var — a PAT scoped to ledoent/* with Contents:write
(for merge-upstream) and public_repo (for upstream PR listing).
"""

from __future__ import annotations

import html
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


def load_forks() -> list[dict]:
    """Parse .github/forks.yml — block-style entries only.

    The format is restricted enough that we don't pull in PyYAML; one
    `- repo:` line per entry, followed by indented `key: value` lines
    until the next entry or a blank line. Lists are written as
    `[item, item]`. Comments stripped.
    """
    text = Path(".github/forks.yml").read_text()
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
                # ["18.0", "19.0"] → list of strings
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
    skipped = (
        (status == 422 and "does not exist" in msg.lower())
        or (status == 404 and "branch not found" in msg.lower())
    )
    return {
        "repo": repo,
        "branch": branch,
        "status": status,
        "message": msg,
        "merge_type": body.get("merge_type"),
        "skipped": skipped,
    }


def list_recent_mig_prs(org: str, repo_name: str, track: str, since: datetime) -> list[dict]:
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


def render(sync_results: list[dict], mig_buckets: dict[str, list[dict]]) -> tuple[str, str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fail = [r for r in sync_results if r["status"] >= 400 and not r["skipped"]]
    updated = [r for r in sync_results if r.get("merge_type") in ("fast-forward", "merge")]
    nochange = [r for r in sync_results if r.get("merge_type") == "none" and r["status"] < 400]
    skipped = [r for r in sync_results if r["skipped"]]

    lines: list[str] = [
        '<html><body style="font-family: -apple-system, sans-serif; max-width: 800px;">',
        f"<h2>Ledoent fork digest — {today}</h2>",
        f"<p><b>Sync:</b> {len(updated)} updated · {len(nochange)} already current · "
        f"{len(skipped)} skipped (branch n/a) · "
        f'<span style="color:{"#c00" if fail else "#0a0"}">{len(fail)} failed</span></p>',
    ]

    if fail:
        lines.append('<h3 style="color:#c00">⚠️ Sync failures</h3><ul>')
        for r in fail:
            lines.append(
                f"<li><code>{html.escape(r['repo'])}</code> "
                f"({html.escape(r['branch'])}): "
                f"HTTP {r['status']} — {html.escape(r['message'])}</li>"
            )
        lines.append("</ul>")

    if updated:
        lines.append("<h3>Synced</h3><ul>")
        for r in updated:
            lines.append(
                f"<li><code>{html.escape(r['repo'])}</code> "
                f"({html.escape(r['branch'])}): "
                f"{html.escape(r.get('merge_type') or '')}</li>"
            )
        lines.append("</ul>")

    total_migs = sum(len(v) for v in mig_buckets.values())
    if total_migs:
        lines.append(f"<h3>Upstream [MIG] PRs merged in last 24h ({total_migs})</h3>")
        for repo, items in sorted(mig_buckets.items()):
            if not items:
                continue
            lines.append(f"<h4>{html.escape(repo)}</h4><ul>")
            for it in items:
                lines.append(
                    f'<li><a href="{html.escape(it["url"])}">#{it["number"]}</a> '
                    f"<b>{html.escape(it['title'])}</b> — "
                    f"@{html.escape(it.get('user') or '')} "
                    f"<small>{html.escape(it.get('merged_at') or '')}</small></li>"
                )
            lines.append("</ul>")
    else:
        lines.append("<p><i>No upstream [MIG] PRs merged in the last 24h.</i></p>")

    lines.append(
        '<hr><p style="color:#888;font-size:11px">'
        "Generated by ledoent/.github/.github/workflows/fork-sync-and-digest.yml — "
        "edit <code>.github/forks.yml</code> to add/remove tracked forks.</p>"
    )
    lines.append("</body></html>")

    subject_parts = [f"Ledoent digest {today}"]
    if fail:
        subject_parts.append(f"⚠️ {len(fail)} sync fail")
    if total_migs:
        subject_parts.append(f"{total_migs} MIG")
    subject = " — ".join(subject_parts)

    return subject, "\n".join(lines)


def main() -> int:
    forks = load_forks()
    print(f"Loaded {len(forks)} forks from .github/forks.yml", file=sys.stderr)

    # Stash the parsed list for downstream workflows (forward-port distributor).
    Path("forks-parsed.json").write_text(json.dumps(forks, indent=2))

    sync_results: list[dict] = []
    for f in forks:
        for branch in f.get("branches", []):
            res = sync_branch(f["repo"], branch)
            sync_results.append(res)
            tag = "SKIP" if res["skipped"] else (res.get("merge_type") or str(res["status"]))
            print(f"  {res['repo']}@{branch:>10}  -> {tag}", file=sys.stderr)

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
        prs = list_recent_mig_prs(f["upstream_org"], repo_name, f["upstream_track"], since)
        if prs:
            mig_buckets[f"{f['upstream_org']}/{repo_name}@{f['upstream_track']}"] = prs

    subject, body_html = render(sync_results, mig_buckets)
    Path("digest.html").write_text(body_html)
    Path("digest.subject").write_text(subject)

    fail_count = sum(1 for r in sync_results if r["status"] >= 400 and not r["skipped"])
    Path("digest.exit").write_text(str(fail_count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
