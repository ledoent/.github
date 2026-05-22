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
    """Make gh() return each tuple in turn (merge-upstream, then compare)."""
    it = iter(returns)
    monkeypatch.setattr(fork_sync_digest, "gh", lambda *a, **kw: next(it))


def test_diverged_branch_is_flagged_when_upstream_org_provided(monkeypatch):
    # merge-upstream succeeds with a merge commit; compare reports 30
    # local commits ahead. This is the case that motivated the check —
    # ledoent/social:18.0 in May 2026 silently accumulated drift because
    # merge-upstream returns success either way.
    _patch_gh_sequence(
        monkeypatch,
        (200, {"merge_type": "merge"}),
        (200, {"ahead_by": 30, "behind_by": 0}),
    )
    r = fork_sync_digest.sync_branch("ledoent/social", "18.0", upstream_org="OCA")
    assert r["diverged"] is True
    assert r["ahead_by"] == 30
    assert r["behind_by"] == 0


def test_clean_fast_forward_is_not_diverged(monkeypatch):
    _patch_gh_sequence(
        monkeypatch,
        (200, {"merge_type": "fast-forward"}),
        (200, {"ahead_by": 0, "behind_by": 0}),
    )
    r = fork_sync_digest.sync_branch("ledoent/x", "18.0", upstream_org="OCA")
    assert r["diverged"] is False
    assert r["ahead_by"] == 0


def test_no_upstream_org_skips_divergence_check(monkeypatch):
    # Non-OCA forks (upstream_org: null) get no compare call. Verified by
    # patching gh to fail loudly if called more than once.
    calls = []

    def fake_gh(method, path, body=None):
        calls.append((method, path))
        return 200, {"merge_type": "fast-forward"}

    monkeypatch.setattr(fork_sync_digest, "gh", fake_gh)
    r = fork_sync_digest.sync_branch("ledoent/x", "master")
    assert len(calls) == 1
    assert r["diverged"] is False
    assert r["ahead_by"] is None


def test_compare_failure_does_not_mask_successful_sync(monkeypatch):
    # If the compare endpoint flakes, the sync result still records the
    # merge-upstream success. ahead_by stays None so the digest knows we
    # don't actually know — better than a false "clean" classification.
    _patch_gh_sequence(
        monkeypatch,
        (200, {"merge_type": "fast-forward"}),
        (500, {"message": "Internal Server Error"}),
    )
    r = fork_sync_digest.sync_branch("ledoent/x", "18.0", upstream_org="OCA")
    assert r["status"] == 200
    assert r["merge_type"] == "fast-forward"
    assert r["diverged"] is False
    assert r["ahead_by"] is None


def test_failed_sync_skips_compare(monkeypatch):
    # No point burning a compare call if merge-upstream itself failed —
    # the failure already triggers a digest alarm.
    calls = []

    def fake_gh(method, path, body=None):
        calls.append((method, path))
        return 403, {"message": "Resource not accessible by personal access token"}

    monkeypatch.setattr(fork_sync_digest, "gh", fake_gh)
    r = fork_sync_digest.sync_branch("ledoent/x", "18.0", upstream_org="OCA")
    assert len(calls) == 1
    assert r["status"] == 403
    assert r["diverged"] is False
