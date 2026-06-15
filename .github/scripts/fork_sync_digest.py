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

import fnmatch
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from _github import make_headers, request, require_token

# Lazy-init so the module is importable without GH_TOKEN (render_digest
# imports MANAGED_COMMIT_PATTERNS from here, and runs in a step without
# the token env). Populated by main() before any gh() call.
HEADERS: dict = {}

# Ahead-commit subjects that the sync pipeline itself produces, so an
# overlay of these on top of upstream is expected — not "diverged" in
# the alarming sense. Two sources:
#   1. distribute_forward_port.py writes forward-port.yml via the
#      Contents API, which lands as a commit on the default (series)
#      branch. The workflow file MUST live on the branch where
#      pull_request_target events fire, so this commit is structural.
#   2. merge-upstream produces a "Merge branch 'OCA:<b>' into <b>"
#      commit any time the fork is non-empty (i.e. carries #1). Also
#      structural — not a sign of human error on a series branch.
# Anything outside these patterns is a real divergence (someone pushed
# work to a series branch directly) and stays flagged.
MANAGED_COMMIT_PATTERNS = (
    re.compile(
        r"^chore\(ci\): (created|updated) forward-port\.yml "
        r"from ledoent/\.github distributor"
    ),
    re.compile(r"^Merge branch '[^']+' into "),
)


def _is_managed_commit(subject: str) -> bool:
    return any(p.match(subject) for p in MANAGED_COMMIT_PATTERNS)


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
        "ahead_commits": [],
        "diverged": False,
        "managed_overlay": False,
    }
    # Post-sync divergence check. merge-upstream silently produces a
    # merge commit (instead of fast-forwarding) when the fork has local
    # commits the upstream doesn't have — returning success either way.
    # Without this check, accidental pushes to a series-named branch on
    # the fork accumulate undetected.
    #
    # But: forks with install_forward_port carry a managed chore commit
    # on every series branch (the distributor's forward-port.yml write),
    # and merge-upstream then layers a "Merge branch 'OCA:<b>'" commit
    # on top each time upstream advances. Both are structural — not
    # human error. Classify them as `managed_overlay` and reserve
    # `diverged` for genuinely unexpected ahead-commits.
    if upstream_org and not skipped and status < 400:
        ahead, behind, subjects = _compare_with_upstream(
            repo, upstream_org, branch
        )
        result["ahead_by"] = ahead
        result["behind_by"] = behind
        result["ahead_commits"] = subjects
        if ahead and ahead > 0:
            unmanaged = [s for s in subjects if not _is_managed_commit(s)]
            if unmanaged:
                result["diverged"] = True
            else:
                result["managed_overlay"] = True
    return result


def _compare_with_upstream(
    fork_repo: str, upstream_org: str, branch: str
) -> tuple[int | None, int | None, list[str]]:
    """Return (ahead_by, behind_by, ahead_commit_subjects).

    `ahead_commit_subjects` is the first line of each commit the fork
    has that upstream doesn't — used to classify the divergence as
    managed (structural overlay) vs. real (someone pushed work to a
    series branch). Returns (None, None, []) on any API error so a
    flaky compare doesn't mask a successful sync.
    """
    fork_owner = fork_repo.split("/", 1)[0]
    repo_name = fork_repo.split("/", 1)[1]
    path = (
        f"/repos/{upstream_org}/{repo_name}/compare/"
        f"{upstream_org}:{branch}...{fork_owner}:{branch}"
    )
    status, body = gh("GET", path)
    if status != 200:
        return None, None, []
    subjects = [
        (c.get("commit", {}).get("message") or "").split("\n", 1)[0]
        for c in body.get("commits", [])
    ]
    return body.get("ahead_by"), body.get("behind_by"), subjects


def check_ledoent_branch(repo: str, upstream_org: str | None, upstream_track: str | None) -> dict | None:
    """Test if the ledoent branch of the fork rebases/aggregates cleanly."""
    status, body = gh("GET", f"/repos/{repo}/branches/ledoent")
    if status != 200:
        return None  # No ledoent branch exists on this fork, skip

    print(f"  Testing ledoent branch for {repo}...", file=sys.stderr)
    base_branch = upstream_track or "18.0"
    token = require_token()
    repo_name = repo.split("/", 1)[1]
    fork_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    upstream_org_name = upstream_org or "OCA"
    upstream_url = f"https://github.com/{upstream_org_name}/{repo_name}.git"

    check_status = "error"
    error_msg = ""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        repo_dir = tmp_path / "repo"

        try:
            # 1. Clone the fork's ledoent branch
            subprocess.run(
                ["git", "clone", "--branch", "ledoent", "--single-branch", fork_url, "repo"],
                cwd=tmpdir, check=True, capture_output=True
            )
            # 2. Add upstream remote
            subprocess.run(
                ["git", "remote", "add", "upstream", upstream_url],
                cwd=repo_dir, check=True, capture_output=True
            )
            # 3. Fetch target branch
            subprocess.run(
                ["git", "fetch", "upstream", base_branch],
                cwd=repo_dir, check=True, capture_output=True
            )
            # 4. Configure dummy identity for merging/rebasing
            subprocess.run(["git", "config", "user.name", "github-actions[bot]"], cwd=repo_dir, check=True)
            subprocess.run(["git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com"], cwd=repo_dir, check=True)

            repos_yaml = repo_dir / "repos.yaml"
            if repos_yaml.exists():
                # Test gitaggregate
                res = subprocess.run(
                    ["gitaggregate", "-c", "repos.yaml"],
                    cwd=repo_dir, capture_output=True
                )
                if res.returncode != 0:
                    check_status = "conflict"
                    error_msg = f"gitaggregate failed:\n{res.stderr.decode()}"
                else:
                    check_status = "clean"
            else:
                # Test rebase
                res = subprocess.run(
                    ["git", "rebase", f"upstream/{base_branch}"],
                    cwd=repo_dir, capture_output=True
                )
                if res.returncode != 0:
                    subprocess.run(["git", "rebase", "--abort"], cwd=repo_dir, capture_output=True)
                    check_status = "conflict"
                    error_msg = f"git rebase upstream/{base_branch} failed with conflicts:\n{res.stderr.decode()}"
                else:
                    check_status = "clean"
        except subprocess.CalledProcessError as e:
            check_status = "error"
            error_msg = f"Git command failed: {e.stderr.decode()}"
        except FileNotFoundError:
            check_status = "error"
            error_msg = "gitaggregate executable not found on runner."
        except Exception as e:
            check_status = "error"
            error_msg = f"Unexpected check error: {str(e)}"

    return {
        "repo": repo,
        "branch": "ledoent",
        "check_type": "rebase_check",
        "status": 200 if check_status == "clean" else (409 if check_status == "conflict" else 500),
        "check_status": check_status,
        "message": error_msg or "Clean",
        "skipped": False,
    }


def _list_matching_branches(repo: str, globs: list[str]) -> list[str]:
    """Fork branch names matching any fnmatch glob in `globs` (paginated)."""
    out: list[str] = []
    page = 1
    while True:
        status, body = gh(
            "GET", f"/repos/{repo}/branches?per_page=100&page={page}"
        )
        if status != 200 or not isinstance(body, list) or not body:
            break
        names = [b["name"] for b in body]
        out.extend(
            n for n in names if any(fnmatch.fnmatch(n, g) for g in globs)
        )
        if len(names) < 100:
            break
        page += 1
    return sorted(set(out))


def _rebase_one(
    repo_dir: Path, repo: str, branch: str, base_branch: str, base_ref: str
) -> dict:
    """Rebase one fetched feature branch onto base_ref; force-push if clean."""

    def row(status: int, rebase_status: str, behind, message: str) -> dict:
        return {
            "repo": repo,
            "branch": branch,
            "check_type": "feature_rebase",
            "status": status,
            "rebase_status": rebase_status,
            "behind": behind,
            "message": message,
            "skipped": False,
        }

    def git(*args, **kw):
        return subprocess.run(
            ["git", *args], cwd=repo_dir, capture_output=True, **kw
        )

    fetched = git("fetch", "origin", branch)
    if fetched.returncode != 0:
        return row(500, "error", None, f"fetch failed: {fetched.stderr.decode()[:200]}")

    cnt = git("rev-list", "--count", f"origin/{branch}..{base_ref}")
    behind = int(cnt.stdout.decode().strip() or "0") if cnt.returncode == 0 else None
    if behind == 0:
        return row(200, "already-current", 0, "up to date with base")

    git("checkout", "--detach", f"origin/{branch}", check=True)
    git("clean", "-fdq")
    mergebase = git("merge-base", base_ref, f"origin/{branch}").stdout.decode().strip()
    # Content-preserving rebase: hooks off (nothing new to lint — the branch
    # content is unchanged, only re-parented) and a no-op editor so an empty/
    # message step can't block in the headless runner.
    env = {**os.environ, "GIT_EDITOR": "true"}
    reb = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "rebase", "--onto", base_ref, mergebase],
        cwd=repo_dir, capture_output=True, env=env,
    )
    if reb.returncode != 0:
        files = git("diff", "--name-only", "--diff-filter=U").stdout.decode().split()
        subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "rebase", "--abort"],
            cwd=repo_dir, capture_output=True, env=env,
        )
        return row(409, "conflict", behind, "conflict in: " + ", ".join(files[:8]))

    # PAT-authed push ⇒ re-triggers the fork's tests/pre-commit (a GITHUB_TOKEN
    # push would not). --force-with-lease: the clone fetched origin/<branch>
    # moments ago, so this guards against a racing push in the same window.
    pushed = git("push", "--force-with-lease", "origin", f"HEAD:refs/heads/{branch}")
    if pushed.returncode != 0:
        return row(500, "error", behind, f"push failed: {pushed.stderr.decode()[:200]}")
    return row(200, "rebased", behind, f"rebased onto {base_branch} (+{behind})")


def rebase_feature_branches(repo: str, globs: list[str], base_branch: str) -> list[dict]:
    """Rebase fork feature branches matching `globs` onto the fork's freshly
    synced `base_branch`, force-pushing clean rebases and reporting conflicts.

    Runs AFTER the merge-upstream sync in the same fork iteration, so
    `origin/<base_branch>` already reflects upstream. Whole-fork failures
    (clone, etc.) collapse to a single error row so the digest still renders.
    """
    branches = _list_matching_branches(repo, globs)
    if not branches:
        return []
    print(
        f"  Rebasing {len(branches)} feature branch(es) on {repo} onto {base_branch}...",
        file=sys.stderr,
    )
    token = require_token()
    fork_url = f"https://x-access-token:{token}@github.com/{repo}.git"
    results: list[dict] = []
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_dir = Path(tmpdir) / "repo"
            clone = subprocess.run(
                ["git", "clone", "--branch", base_branch, "--single-branch", fork_url, "repo"],
                cwd=tmpdir, capture_output=True,
            )
            if clone.returncode != 0:
                return [{
                    "repo": repo, "branch": "(clone)", "check_type": "feature_rebase",
                    "status": 500, "rebase_status": "error", "behind": None,
                    "message": f"clone failed: {clone.stderr.decode()[:300]}",
                    "skipped": False,
                }]
            subprocess.run(
                ["git", "config", "user.name", "github-actions[bot]"],
                cwd=repo_dir, check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email",
                 "41898282+github-actions[bot]@users.noreply.github.com"],
                cwd=repo_dir, check=True, capture_output=True,
            )
            base_ref = f"origin/{base_branch}"
            for b in branches:
                results.append(_rebase_one(repo_dir, repo, b, base_branch, base_ref))
    except Exception as e:  # noqa: BLE001 — never let one fork crash the digest
        results.append({
            "repo": repo, "branch": "(rebase)", "check_type": "feature_rebase",
            "status": 500, "rebase_status": "error", "behind": None,
            "message": f"unexpected error: {e}", "skipped": False,
        })
    return results


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
    global HEADERS
    HEADERS = make_headers(require_token(), user_agent="ledoent-fork-digest/2.0")
    forks = load_forks()
    print(f"Loaded {len(forks)} forks from .github/forks.yml", file=sys.stderr)
    Path("forks-parsed.json").write_text(json.dumps(forks, indent=2))

    sync_results: list[dict] = []
    rebase_results: list[dict] = []
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

        # Check ledoent branch if it exists on the fork
        ledoent_res = check_ledoent_branch(f["repo"], f.get("upstream_org"), f.get("upstream_track"))
        if ledoent_res:
            sync_results.append(ledoent_res)
            print(
                f"  {ledoent_res['repo']}@ledoent  -> CHECK {ledoent_res['check_status'].upper()}",
                file=sys.stderr,
            )

        # Rebase opted-in feature branches onto the freshly-synced base.
        globs = f.get("rebase_globs")
        if globs:
            base = f.get("upstream_track") or "19.0"
            for r in rebase_feature_branches(f["repo"], globs, base):
                rebase_results.append(r)
                print(
                    f"  {r['repo']}@{r['branch']}  -> REBASE {r['rebase_status'].upper()}",
                    file=sys.stderr,
                )
    Path("sync-results.json").write_text(json.dumps(sync_results, indent=2))
    Path("rebase-results.json").write_text(json.dumps(rebase_results, indent=2))

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
