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
