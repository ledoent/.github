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


def _commits(*subjects):
    return [{"commit": {"message": s}} for s in subjects]


def test_diverged_branch_is_flagged_when_upstream_org_provided(monkeypatch):
    # merge-upstream succeeds with a merge commit; compare reports 30
    # local commits ahead, one of which isn't a managed overlay. This
    # is the case that motivated the check — ledoent/social:18.0 in
    # May 2026 silently accumulated drift because merge-upstream
    # returns success either way.
    _patch_gh_sequence(
        monkeypatch,
        (200, {"merge_type": "merge"}),
        (200, {
            "ahead_by": 30,
            "behind_by": 0,
            "commits": _commits(
                "chore(ci): created forward-port.yml from ledoent/.github distributor",
                "Merge branch 'OCA:18.0' into 18.0",
                "[FIX] some real human commit that shouldn't be here",
            ),
        }),
    )
    r = fork_sync_digest.sync_branch("ledoent/social", "18.0", upstream_org="OCA")
    assert r["diverged"] is True
    assert r["managed_overlay"] is False
    assert r["ahead_by"] == 30
    assert r["behind_by"] == 0


def test_clean_fast_forward_is_not_diverged(monkeypatch):
    _patch_gh_sequence(
        monkeypatch,
        (200, {"merge_type": "fast-forward"}),
        (200, {"ahead_by": 0, "behind_by": 0, "commits": []}),
    )
    r = fork_sync_digest.sync_branch("ledoent/x", "18.0", upstream_org="OCA")
    assert r["diverged"] is False
    assert r["managed_overlay"] is False
    assert r["ahead_by"] == 0


def test_only_distributor_chore_is_managed_not_diverged(monkeypatch):
    # The steady state for install_forward_port forks: 1 ahead by the
    # chore commit alone. Must NOT trip the diverged alarm.
    _patch_gh_sequence(
        monkeypatch,
        (200, {"merge_type": "none"}),
        (200, {
            "ahead_by": 1,
            "behind_by": 0,
            "commits": _commits(
                "chore(ci): created forward-port.yml from ledoent/.github distributor",
            ),
        }),
    )
    r = fork_sync_digest.sync_branch("ledoent/web", "18.0", upstream_org="OCA")
    assert r["diverged"] is False
    assert r["managed_overlay"] is True
    assert r["ahead_by"] == 1


def test_chore_plus_sync_merge_is_managed_not_diverged(monkeypatch):
    # After an upstream advance, the chore + a "Merge branch 'OCA:...'"
    # land together. Still structural, still not divergence.
    _patch_gh_sequence(
        monkeypatch,
        (200, {"merge_type": "merge"}),
        (200, {
            "ahead_by": 2,
            "behind_by": 0,
            "commits": _commits(
                "chore(ci): created forward-port.yml from ledoent/.github distributor",
                "Merge branch 'OCA:18.0' into 18.0",
            ),
        }),
    )
    r = fork_sync_digest.sync_branch("ledoent/web", "18.0", upstream_org="OCA")
    assert r["diverged"] is False
    assert r["managed_overlay"] is True
    assert r["ahead_by"] == 2


def test_updated_chore_subject_is_also_managed(monkeypatch):
    # When the distributor template content changes, the chore commit
    # message switches from "created" to "updated". Both shapes must
    # be recognised.
    _patch_gh_sequence(
        monkeypatch,
        (200, {"merge_type": "none"}),
        (200, {
            "ahead_by": 1,
            "behind_by": 0,
            "commits": _commits(
                "chore(ci): updated forward-port.yml from ledoent/.github distributor",
            ),
        }),
    )
    r = fork_sync_digest.sync_branch("ledoent/web", "18.0", upstream_org="OCA")
    assert r["managed_overlay"] is True
    assert r["diverged"] is False


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
    assert r["managed_overlay"] is False
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

