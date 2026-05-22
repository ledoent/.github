#!/usr/bin/env python3
"""Render the daily digest email from collected JSON artifacts.

Reads sync-results.json, mig-buckets.json, and (optionally)
forward-port-distribution.json from CWD. Writes digest.html,
digest.subject, and digest.exit which the workflow's email step
consumes.

Splitting render from collection means the distributor's outcomes
land in the email body — previously they only appeared in the
artifact tarball, so a 14/14 failure (like our first run) sent a
"looks fine" digest. With this split, both the per-branch sync and
the forward-port distribution show up in the inbox.
"""

from __future__ import annotations

import html
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _load(path: str, default):
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else default


def render(
    sync_results: list[dict],
    mig_buckets: dict[str, list[dict]],
    distribution: list[dict],
) -> tuple[str, str, str]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    sync_fail = [
        r for r in sync_results if r["status"] >= 400 and not r["skipped"]
    ]
    sync_updated = [
        r for r in sync_results if r.get("merge_type") in ("fast-forward", "merge")
    ]
    sync_nochange = [
        r for r in sync_results
        if r.get("merge_type") == "none" and r["status"] < 400
    ]
    sync_skipped = [r for r in sync_results if r["skipped"]]
    # Divergence = fork branch has commits upstream doesn't. merge-upstream
    # returns success and produces a merge commit; the digest needs to
    # surface this separately so it gets fixed (force-reset the branch)
    # instead of accumulating forever.
    sync_diverged = [
        r for r in sync_results
        if r.get("diverged") and not r["skipped"]
    ]

    dist_fail = [r for r in distribution if r["action"] == "failed"]
    dist_created = [r for r in distribution if r["action"] == "created"]
    dist_updated = [r for r in distribution if r["action"] == "updated"]
    dist_unchanged = [r for r in distribution if r["action"] == "unchanged"]

    lines: list[str] = [
        '<html><body style="font-family: -apple-system, sans-serif; '
        'max-width: 800px;">',
        f"<h2>Ledoent fork digest — {today}</h2>",
        f"<p><b>Sync:</b> {len(sync_updated)} updated · "
        f"{len(sync_nochange)} already current · "
        f"{len(sync_skipped)} skipped (branch n/a) · "
        f'<span style="color:{"#c00" if sync_fail else "#0a0"}">'
        f"{len(sync_fail)} failed</span>"
        + (
            f' · <span style="color:#c80">{len(sync_diverged)} diverged</span>'
            if sync_diverged else ""
        )
        + "</p>",
    ]
    if distribution:
        lines.append(
            f"<p><b>Forward-port distribution:</b> "
            f"{len(dist_created)} created · {len(dist_updated)} updated · "
            f"{len(dist_unchanged)} unchanged · "
            f'<span style="color:{"#c00" if dist_fail else "#0a0"}">'
            f"{len(dist_fail)} failed</span></p>"
        )

    if sync_fail:
        lines.append('<h3 style="color:#c00">⚠️ Sync failures</h3><ul>')
        for r in sync_fail:
            lines.append(
                f"<li><code>{html.escape(r['repo'])}</code> "
                f"({html.escape(r['branch'])}): "
                f"HTTP {r['status']} — {html.escape(r['message'])}</li>"
            )
        lines.append("</ul>")

    if sync_diverged:
        lines.append(
            '<h3 style="color:#c80">⚠️ Diverged from upstream</h3>'
            "<p>These fork branches have commits the upstream doesn't — "
            "merge-upstream silently produced a merge commit instead of "
            "fast-forwarding. Reset the branch to upstream HEAD "
            "(after backing it up).</p><ul>"
        )
        for r in sync_diverged:
            ahead = r.get("ahead_by") or 0
            behind = r.get("behind_by") or 0
            lines.append(
                f"<li><code>{html.escape(r['repo'])}</code> "
                f"({html.escape(r['branch'])}): "
                f"<b>{ahead} ahead</b>, {behind} behind</li>"
            )
        lines.append("</ul>")

    if dist_fail:
        lines.append('<h3 style="color:#c00">⚠️ Distributor failures</h3><ul>')
        for r in dist_fail:
            lines.append(
                f"<li><code>{html.escape(r['repo'])}</code>: "
                f"{html.escape(r['detail'])}</li>"
            )
        lines.append("</ul>")

    if sync_updated:
        lines.append("<h3>Synced</h3><ul>")
        for r in sync_updated:
            lines.append(
                f"<li><code>{html.escape(r['repo'])}</code> "
                f"({html.escape(r['branch'])}): "
                f"{html.escape(r.get('merge_type') or '')}</li>"
            )
        lines.append("</ul>")

    total_migs = sum(len(v) for v in mig_buckets.values())
    if total_migs:
        lines.append(
            f"<h3>Upstream [MIG] PRs merged in last 24h ({total_migs})</h3>"
        )
        for repo, items in sorted(mig_buckets.items()):
            if not items:
                continue
            lines.append(f"<h4>{html.escape(repo)}</h4><ul>")
            for it in items:
                lines.append(
                    f'<li><a href="{html.escape(it["url"])}">'
                    f"#{it['number']}</a> "
                    f"<b>{html.escape(it['title'])}</b> — "
                    f"@{html.escape(it.get('user') or '')} "
                    f"<small>{html.escape(it.get('merged_at') or '')}</small>"
                    "</li>"
                )
            lines.append("</ul>")
    else:
        lines.append("<p><i>No upstream [MIG] PRs merged in the last 24h.</i></p>")

    lines.append(
        '<hr><p style="color:#888;font-size:11px">'
        "Generated by ledoent/.github/.github/workflows/fork-sync-and-digest.yml "
        "— edit <code>.github/forks.yml</code> to add/remove tracked forks.</p>"
    )
    lines.append("</body></html>")

    subject_parts = [f"Ledoent digest {today}"]
    if sync_fail:
        subject_parts.append(f"⚠️ {len(sync_fail)} sync fail")
    if dist_fail:
        subject_parts.append(f"⚠️ {len(dist_fail)} dist fail")
    if sync_diverged:
        subject_parts.append(f"⚠️ {len(sync_diverged)} diverged")
    if total_migs:
        subject_parts.append(f"{total_migs} MIG")
    subject = " — ".join(subject_parts)

    fail_count = len(sync_fail) + len(dist_fail)
    return subject, "\n".join(lines), str(fail_count)


def main() -> int:
    sync_results = _load("sync-results.json", [])
    mig_buckets = _load("mig-buckets.json", {})
    distribution = _load("forward-port-distribution.json", [])

    subject, body_html, exit_marker = render(sync_results, mig_buckets, distribution)
    Path("digest.html").write_text(body_html)
    Path("digest.subject").write_text(subject)
    Path("digest.exit").write_text(exit_marker)

    print(f"Wrote digest.html ({len(body_html)} bytes), subject: {subject}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
