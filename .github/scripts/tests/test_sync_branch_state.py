"""Cover the response → state mapping in sync_branch.

The function is a thin wrapper around merge-upstream, but the
422/404 → "skipped" branch is load-bearing: get it wrong and
forks without a 19.0 branch yet flood the digest with false alarms.
"""

import fork_sync_digest


def _patch_gh(monkeypatch, returns):
    """Make gh(method, path, body) return a fixed (status, body)."""
    monkeypatch.setattr(fork_sync_digest, "gh", lambda *a, **kw: returns)


def test_fast_forward_is_success(monkeypatch):
    _patch_gh(monkeypatch, (200, {"merge_type": "fast-forward"}))
    r = fork_sync_digest.sync_branch("ledoent/x", "18.0")
    assert r["status"] == 200
    assert r["merge_type"] == "fast-forward"
    assert r["skipped"] is False


def test_no_change_is_success(monkeypatch):
    _patch_gh(monkeypatch, (200, {"merge_type": "none"}))
    r = fork_sync_digest.sync_branch("ledoent/x", "18.0")
    assert r["skipped"] is False
    assert r["merge_type"] == "none"


def test_422_branch_not_on_fork_is_skipped(monkeypatch):
    _patch_gh(monkeypatch, (422, {"message": "Branch does not exist"}))
    r = fork_sync_digest.sync_branch("ledoent/x", "20.0")
    assert r["status"] == 422
    assert r["skipped"] is True


def test_404_branch_not_on_upstream_is_skipped(monkeypatch):
    _patch_gh(monkeypatch, (404, {"message": "Branch not found"}))
    r = fork_sync_digest.sync_branch("ledoent/x", "20.0")
    assert r["status"] == 404
    assert r["skipped"] is True


def test_real_403_is_not_skipped(monkeypatch):
    # The first-run PAT-scope error class. MUST flag as failure.
    _patch_gh(monkeypatch, (403, {"message": "Resource not accessible by personal access token"}))
    r = fork_sync_digest.sync_branch("ledoent/x", "18.0")
    assert r["skipped"] is False
    assert r["status"] == 403


def test_conflict_409_is_not_skipped(monkeypatch):
    # Fork has diverged from upstream — real failure, action required.
    _patch_gh(monkeypatch, (409, {"message": "Merge conflict"}))
    r = fork_sync_digest.sync_branch("ledoent/x", "18.0")
    assert r["skipped"] is False
    assert r["status"] == 409


def _patch_gh_sequence(monkeypatch, *returns):
    """Make gh() return each tuple in turn.

    On a fork with an upstream_org the compare call comes FIRST (the
    mirror guard), and merge-upstream only runs if the branch is not
    ahead. Order the tuples accordingly.
    """
    it = iter(returns)
    monkeypatch.setattr(fork_sync_digest, "gh", lambda *a, **kw: next(it))


def _record_gh(monkeypatch, *returns):
    """Same, but also records (method, path) of every call made."""
    calls = []
    it = iter(returns)

    def fake_gh(method, path, body=None):
        calls.append((method, path))
        return next(it)

    monkeypatch.setattr(fork_sync_digest, "gh", fake_gh)
    return calls


def _commits(*subjects):
    return [{"commit": {"message": s}} for s in subjects]


def _compare(ahead, *subjects, behind=0):
    return (200, {
        "ahead_by": ahead,
        "behind_by": behind,
        "commits": _commits(*subjects),
    })


def test_diverged_branch_is_flagged_when_upstream_org_provided(monkeypatch):
    # A stray human commit on a series branch. Must be flagged AND must
    # not be merged — merging would bury it under a merge commit.
    calls = _record_gh(
        monkeypatch,
        _compare(30, "[FIX] some real human commit that shouldn't be here"),
    )
    r = fork_sync_digest.sync_branch("ledoent/social", "18.0", upstream_org="OCA")
    assert r["diverged"] is True
    assert r["managed_overlay"] is False
    assert r["ahead_by"] == 30
    assert not any(m == "POST" for m, _ in calls), "must not merge a diverged branch"


def test_ahead_branch_is_never_merged(monkeypatch):
    # THE regression this guard exists for. merge-upstream on a branch
    # that is ahead cannot fast-forward, so it mints a merge commit and
    # returns 200 — leaving the branch ahead forever, so the next run
    # mints another. 14 series branches accumulated ~140 of these before
    # anyone noticed. The guard must refuse to call merge-upstream at all.
    calls = _record_gh(monkeypatch, _compare(1, "Merge branch 'OCA:18.0' into 18.0"))
    r = fork_sync_digest.sync_branch("ledoent/web", "18.0", upstream_org="OCA")
    assert calls == [("GET", "/repos/OCA/web/compare/OCA:18.0...ledoent:18.0")]
    assert r["merge_type"] is None
    assert "refusing to merge" in r["message"]


def test_merge_commit_is_no_longer_whitelisted(monkeypatch):
    # The old MANAGED_COMMIT_PATTERNS matched "Merge branch '...' into",
    # so the tool's own merge commits were classified as expected and the
    # drift stayed invisible. A merge commit on a mirror branch is always
    # a bug and must now surface as divergence.
    _patch_gh_sequence(monkeypatch, _compare(2, "Merge branch 'OCA:18.0' into 18.0"))
    r = fork_sync_digest.sync_branch("ledoent/web", "18.0", upstream_org="OCA")
    assert r["diverged"] is True
    assert r["managed_overlay"] is False


def test_clean_fast_forward_is_not_diverged(monkeypatch):
    _patch_gh_sequence(
        monkeypatch,
        _compare(0),
        (200, {"merge_type": "fast-forward"}),
    )
    r = fork_sync_digest.sync_branch("ledoent/x", "18.0", upstream_org="OCA")
    assert r["diverged"] is False
    assert r["managed_overlay"] is False
    assert r["merge_type"] == "fast-forward"
    assert r["ahead_by"] == 0


def test_behind_only_branch_still_fast_forwards(monkeypatch):
    # Stale but pristine (a strict ancestor of upstream) — the common
    # case. Must still sync.
    _patch_gh_sequence(
        monkeypatch,
        _compare(0, behind=4),
        (200, {"merge_type": "fast-forward"}),
    )
    r = fork_sync_digest.sync_branch("ledoent/OpenUpgrade", "19.0", upstream_org="OCA")
    assert r["merge_type"] == "fast-forward"
    assert r["diverged"] is False
    assert r["behind_by"] == 4


def test_only_distributor_chore_is_managed_not_diverged(monkeypatch):
    # The distributor's forward-port.yml commit. Named as managed so the
    # digest can word it accurately ("the distributor did this, not a
    # human") — but it still leaves the branch a non-mirror, so it is
    # still not merged.
    calls = _record_gh(
        monkeypatch,
        _compare(
            1,
            "chore(ci): created forward-port.yml from ledoent/.github distributor",
        ),
    )
    r = fork_sync_digest.sync_branch("ledoent/web", "18.0", upstream_org="OCA")
    assert r["diverged"] is False
    assert r["managed_overlay"] is True
    assert r["ahead_by"] == 1
    assert not any(m == "POST" for m, _ in calls)


def test_updated_chore_subject_is_also_managed(monkeypatch):
    # When the distributor template content changes, the chore commit
    # message switches from "created" to "updated". Both shapes must
    # be recognised.
    _patch_gh_sequence(
        monkeypatch,
        _compare(
            1,
            "chore(ci): updated forward-port.yml from ledoent/.github distributor",
        ),
    )
    r = fork_sync_digest.sync_branch("ledoent/web", "18.0", upstream_org="OCA")
    assert r["managed_overlay"] is True
    assert r["diverged"] is False


def test_no_upstream_org_skips_divergence_check(monkeypatch):
    # Non-OCA forks (upstream_org: null) are NOT mirrors — sentry/master
    # carries real customizations — so they get no guard and no compare
    # call. merge-upstream is the correct primitive there.
    calls = _record_gh(monkeypatch, (200, {"merge_type": "merge"}))
    r = fork_sync_digest.sync_branch("ledoent/sentry", "master")
    assert calls == [("POST", "/repos/ledoent/sentry/merge-upstream")]
    assert r["diverged"] is False
    assert r["ahead_by"] is None
    assert r["merge_type"] == "merge"


def test_compare_failure_does_not_block_the_sync(monkeypatch):
    # If the compare endpoint flakes we cannot prove the branch is ahead.
    # Fall through to merge-upstream rather than stall the sync, and
    # leave ahead_by None so the digest knows we don't actually know.
    _patch_gh_sequence(
        monkeypatch,
        (500, {"message": "Internal Server Error"}),
        (200, {"merge_type": "fast-forward"}),
    )
    r = fork_sync_digest.sync_branch("ledoent/x", "18.0", upstream_org="OCA")
    assert r["status"] == 200
    assert r["merge_type"] == "fast-forward"
    assert r["diverged"] is False
    assert r["managed_overlay"] is False
    assert r["ahead_by"] is None


def test_failed_sync_is_still_reported(monkeypatch):
    # The PAT-scope error class. Compare says clean, merge-upstream then
    # fails — the failure must survive into the result.
    _patch_gh_sequence(
        monkeypatch,
        _compare(0),
        (403, {"message": "Resource not accessible by personal access token"}),
    )
    r = fork_sync_digest.sync_branch("ledoent/x", "18.0", upstream_org="OCA")
    assert r["status"] == 403
    assert r["skipped"] is False
    assert r["diverged"] is False


def test_check_ledoent_branch_non_existent(monkeypatch):
    # 404 response from GitHub API means no ledoent branch exists
    monkeypatch.setattr(fork_sync_digest, "gh", lambda method, path: (404, {}))
    r = fork_sync_digest.check_ledoent_branch("ledoent/x", "OCA", "18.0")
    assert r is None


def test_check_ledoent_branch_rebase_clean(monkeypatch):
    # ledoent branch exists (200 response)
    monkeypatch.setattr(fork_sync_digest, "gh", lambda method, path: (200, {"name": "ledoent"}))
    monkeypatch.setattr(fork_sync_digest, "require_token", lambda: "mock_token")

    # Mock subprocess.run to simulate clean rebase without repos.yaml
    import subprocess
    from pathlib import Path

    def fake_run(cmd, *args, **kwargs):
        class MockCompletedProcess:
            returncode = 0
            stdout = b""
            stderr = b""
        return MockCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)
    # Mock Path.exists to always return False for repos.yaml
    original_exists = Path.exists
    def fake_exists(self):
        if self.name == "repos.yaml":
            return False
        return original_exists(self)
    monkeypatch.setattr(Path, "exists", fake_exists)

    r = fork_sync_digest.check_ledoent_branch("ledoent/x", "OCA", "18.0")
    assert r["check_status"] == "clean"
    assert r["status"] == 200
    assert r["branch"] == "ledoent"


def test_check_ledoent_branch_gitaggregate_fail(monkeypatch):
    # ledoent branch exists
    monkeypatch.setattr(fork_sync_digest, "gh", lambda method, path: (200, {"name": "ledoent"}))
    monkeypatch.setattr(fork_sync_digest, "require_token", lambda: "mock_token")

    import subprocess
    from pathlib import Path

    def fake_run(cmd, *args, **kwargs):
        class MockCompletedProcess:
            # gitaggregate fails
            returncode = 1 if "gitaggregate" in cmd else 0
            stdout = b""
            stderr = b"Conflict in merging branches"
        return MockCompletedProcess()

    monkeypatch.setattr(subprocess, "run", fake_run)

    # Mock Path.exists to return True for repos.yaml
    original_exists = Path.exists
    def fake_exists(self):
        if self.name == "repos.yaml":
            return True
        return original_exists(self)
    monkeypatch.setattr(Path, "exists", fake_exists)

    r = fork_sync_digest.check_ledoent_branch("ledoent/x", "OCA", "18.0")
    assert r["check_status"] == "conflict"
    assert r["status"] == 409
    assert "gitaggregate failed" in r["message"]

