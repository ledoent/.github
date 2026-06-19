#!/usr/bin/env python3
"""Flag test-requirements.txt git+https dependency pins that are now removable.

The OCA dep-chain-in-flight pattern pins an *unmerged* dependency PR via a
``odoo-addon-<mod> @ git+https://.../refs/pull/<N>/head`` line in
``test-requirements.txt``. Once that dependency's OCA wheel is published to
PyPI, the pin is stale: it must be stripped or the PR's "Detect unreleased
dependencies" check stays red forever. Nothing told us when that happened (a
stale ``mis_builder`` pin sat on OCA/l10n-usa#180 after its wheel shipped), so
this nightly job closes the loop.

For every open PR authored by us (across the org's forks + the upstream OCA
repos we contribute to), it reads the head branch's ``test-requirements.txt``,
extracts each git+https pin, and checks whether ``odoo-addon-<mod>`` has a
release on PyPI for the PR's series (``18.0`` / ``19.0`` taken from the branch
name). Matches are "removable". Writes ``stale-pins.json`` plus an HTML digest
(``pins-digest.html`` / ``pins-digest.subject``) for the email step.

Env:
  GH_TOKEN            token that can search/read PRs across ledoent/* + OCA.
  PIN_CHECK_AUTHOR    GitHub login to scan (default: dnplkndll).
"""

import base64
import html
import json
import os
import re
import subprocess
import sys
import urllib.request

AUTHOR = os.environ.get("PIN_CHECK_AUTHOR", "dnplkndll")
PIN_RE = re.compile(r"odoo-addon-([a-z0-9_]+)\s*@\s*git\+https", re.IGNORECASE)
SERIES_RE = re.compile(r"^(\d+\.\d+)")


def gh(*args):
    return subprocess.run(["gh", *args], capture_output=True, text=True)


def our_open_prs():
    """URLs of every open PR authored by AUTHOR, org-wide."""
    r = gh(
        "search", "prs",
        "--author", AUTHOR, "--state", "open", "--limit", "300",
        "--json", "url",
    )
    if r.returncode != 0:
        print("pr search failed:", r.stderr.strip(), file=sys.stderr)
        return []
    return [p["url"] for p in json.loads(r.stdout or "[]")]


def pr_head(url):
    r = gh(
        "pr", "view", url,
        "--json", "number,title,headRefName,headRepository,repository,url",
    )
    if r.returncode != 0:
        return None
    return json.loads(r.stdout)


def fetch_test_requirements(repo, ref):
    """test-requirements.txt content from a repo branch, or '' if absent."""
    r = gh(
        "api", f"repos/{repo}/contents/test-requirements.txt",
        "-X", "GET", "-f", f"ref={ref}", "--jq", ".content",
    )
    if r.returncode != 0:
        return ""
    try:
        return base64.b64decode(r.stdout).decode()
    except Exception:  # noqa: BLE001 - malformed/binary content is just "no pins"
        return ""


_pypi_cache = {}


def pypi_has_series(pkg, series):
    key = (pkg, series)
    if key in _pypi_cache:
        return _pypi_cache[key]
    ok = False
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{pkg}/json", timeout=20
        ) as resp:
            data = json.load(resp)
        ok = any(v.startswith(series) for v in data.get("releases", {}))
    except Exception:  # noqa: BLE001 - a PyPI hiccup must not crash the digest
        ok = False
    _pypi_cache[key] = ok
    return ok


def collect_stale():
    stale = []
    for url in our_open_prs():
        pr = pr_head(url)
        if not pr:
            continue
        head_repo = (pr.get("headRepository") or {}).get("nameWithOwner")
        ref = pr.get("headRefName") or ""
        if not head_repo:
            continue
        series_match = SERIES_RE.match(ref)
        if not series_match:
            continue  # can't tell the series -> can't check the right wheel
        series = series_match.group(1)
        content = fetch_test_requirements(head_repo, ref)
        if not content:
            continue
        for pin in PIN_RE.finditer(content):
            module = pin.group(1)
            pkg = "odoo-addon-" + module.replace("_", "-")
            if pypi_has_series(pkg, series):
                stale.append(
                    {
                        "repo": pr["repository"]["nameWithOwner"],
                        "number": pr["number"],
                        "title": pr["title"],
                        "url": pr["url"],
                        "module": module,
                        "pkg": pkg,
                        "series": series,
                    }
                )
    return stale


def render(stale):
    n = len(stale)
    subject = (
        f"[ledoent] {n} stale dependency pin(s) to remove"
        if n
        else "[ledoent] no stale dependency pins"
    )
    if n:
        rows = "".join(
            "<tr>"
            f"<td><a href='{html.escape(s['url'])}'>"
            f"{html.escape(s['repo'])}#{s['number']}</a></td>"
            f"<td>{html.escape(s['title'])}</td>"
            f"<td><code>{html.escape(s['module'])}</code></td>"
            f"<td>{s['series']} wheel on PyPI &mdash; strip the pin</td>"
            "</tr>"
            for s in stale
        )
        body = (
            f"<h2>Stale dependency pins ({n})</h2>"
            "<p>These open PRs pin a <code>git+https</code> dependency whose OCA "
            "wheel is now published on PyPI. Strip the pin line from "
            "<code>test-requirements.txt</code> so the &ldquo;Detect unreleased "
            "dependencies&rdquo; check can go green.</p>"
            "<table border='1' cellpadding='6' cellspacing='0'>"
            "<tr><th>PR</th><th>Title</th><th>Module</th><th>Action</th></tr>"
            f"{rows}</table>"
        )
    else:
        body = (
            "<h2>No stale dependency pins</h2>"
            "<p>Every open-PR pin still points at an unreleased dependency.</p>"
        )
    with open("stale-pins.json", "w") as f:
        json.dump(stale, f, indent=2)
    with open("pins-digest.subject", "w") as f:
        f.write(subject)
    with open("pins-digest.html", "w") as f:
        f.write(body)
    print(f"stale pins: {n}")


if __name__ == "__main__":
    render(collect_stale())
