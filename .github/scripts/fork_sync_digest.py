#!/usr/bin/env python3
"""Sync ledoent forks with upstream + emit a daily digest.

Two responsibilities:
  1. For each fork in .github/forks.yml, POST /repos/{repo}/merge-upstream
     so the base branch tracks upstream. Failures collected into the digest.
  2. For each fork whose `upstream_org` is set, query upstream for `[MIG]`
     PRs closed in the last 24h on the configured track branch — these are
     the "interesting developments" the human wants to know about.

Outputs `digest.html` (plus `digest.subject` and `digest.exit`) in CWD,
which the workflow consumes verbatim.

Auth: `GH_TOKEN` env var, a PAT scoped to ledoent/* with Contents:write
(for merge-upstream) and public_repo (for upstream PR listing).
"""

from __future__ import annotations

import html
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Use stdlib for YAML to avoid a pip step. forks.yml stays narrowly
# enough formatted that this hand-rolled parser is sufficient.
import re


GH = "https://api.github.com"
TOKEN = os.environ["GH_TOKEN"]
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "ledoent-fork-digest/1.0",
}


def gh(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    url = path if path.startswith("http") else f"{GH}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=HEADERS)
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


def load_forks() -> list[dict]:
    text = Path(".github/forks.yml").read_text()
    out: list[dict] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].rstrip()
        if not line.lstrip().startswith("- {"):
            continue
        inner = line.strip().lstrip("- {").rstrip("}")
        entry: dict = {}
        for pair in re.split(r",\s+(?=[a-z_]+:)", inner):
            k, _, v = pair.partition(":")
            k = k.strip()
            v = v.strip().strip('"')
            if v == "null":
                v = None
            entry[k] = v
        out.append(entry)
    return out


def sync_one(repo: str, base: str) -> dict:
    status, body = gh("POST", f"/repos/{repo}/merge-upstream", {"branch": base})
    return {
        "repo": repo,
        "base": base,
        "status": status,
        "message": body.get("message", ""),
        "merge_type": body.get("merge_type"),
        "base_branch": body.get("base_branch"),
    }


def list_recent_mig_prs(org: str, repo_name: str, track: str, since: datetime) -> list[dict]:
    """Closed-and-merged PRs in the last 24h with [MIG] in the title."""
    # Use search API — it lets us filter by title + merged-in-window in one call.
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
    fail = [r for r in sync_results if r["status"] >= 400 or r.get("merge_type") == "none" and r["status"] >= 400]
    updated = [r for r in sync_results if r.get("merge_type") in ("fast-forward", "merge")]
    nochange = [r for r in sync_results if r.get("merge_type") == "none" and r["status"] < 400]

    lines: list[str] = [
        "<html><body style=\"font-family: -apple-system, sans-serif; max-width: 800px;\">",
        f"<h2>Ledoent fork digest — {today}</h2>",
        f"<p><b>Sync:</b> {len(updated)} updated · {len(nochange)} already current · "
        f"<span style=\"color:{'#c00' if fail else '#0a0'}\">{len(fail)} failed</span></p>",
    ]

    if fail:
        lines.append("<h3 style=\"color:#c00\">⚠️ Sync failures</h3><ul>")
        for r in fail:
            lines.append(
                f"<li><code>{html.escape(r['repo'])}</code> ({html.escape(r['base'])}): "
                f"HTTP {r['status']} — {html.escape(r['message'])}</li>"
            )
        lines.append("</ul>")

    if updated:
        lines.append("<h3>Synced</h3><ul>")
        for r in updated:
            lines.append(
                f"<li><code>{html.escape(r['repo'])}</code> ({html.escape(r['base'])}): "
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
                    f"<li><a href=\"{html.escape(it['url'])}\">#{it['number']}</a> "
                    f"<b>{html.escape(it['title'])}</b> — "
                    f"@{html.escape(it.get('user') or '')} "
                    f"<small>{html.escape(it.get('merged_at') or '')}</small></li>"
                )
            lines.append("</ul>")
    else:
        lines.append("<p><i>No upstream [MIG] PRs merged in the last 24h.</i></p>")

    lines.append("<hr><p style=\"color:#888;font-size:11px\">"
                 "Generated by ledoent/.github/.github/workflows/fork-sync-and-digest.yml — "
                 "edit <code>.github/forks.yml</code> to add/remove tracked forks.</p>")
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

    sync_results: list[dict] = []
    for f in forks:
        res = sync_one(f["repo"], f["base"])
        sync_results.append(res)
        print(f"  sync {res['repo']}@{res['base']} -> {res['status']} {res.get('merge_type','-')}",
              file=sys.stderr)

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    mig_buckets: dict[str, list[dict]] = {}
    for f in forks:
        if not f.get("upstream_org") or not f.get("upstream_track"):
            continue
        # repo name without the org prefix
        repo_name = f["repo"].split("/", 1)[1]
        prs = list_recent_mig_prs(f["upstream_org"], repo_name, f["upstream_track"], since)
        if prs:
            mig_buckets[f"{f['upstream_org']}/{repo_name}@{f['upstream_track']}"] = prs

    subject, body_html = render(sync_results, mig_buckets)
    Path("digest.html").write_text(body_html)
    Path("digest.subject").write_text(subject)

    fail_count = sum(1 for r in sync_results if r["status"] >= 400)
    Path("digest.exit").write_text(str(fail_count))
    # We don't propagate fail_count as a non-zero exit: the email step
    # should always run and report failures. The workflow can branch on
    # digest.exit if it wants a separate alert path later.
    return 0


if __name__ == "__main__":
    sys.exit(main())
