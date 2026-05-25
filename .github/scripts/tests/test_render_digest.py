"""Cover the digest rendering — subject tagging and failure surfacing.

The render() function is small but it owns the inbox UX. If it stops
flagging dist failures in the subject (because of a refactor that
forgets the branch), the next 14/14-failure run looks identical to a
healthy one. Pin the behavior here.
"""

import render_digest


def _sync(status: int, merge_type=None, skipped=False, **kw):
    return {
        "repo": "ledoent/x",
        "branch": "18.0",
        "status": status,
        "message": kw.get("message", ""),
        "merge_type": merge_type,
        "skipped": skipped,
    }


def test_clean_subject_no_failures():
    subj, body, exit_marker = render_digest.render(
        sync_results=[_sync(200, "fast-forward")],
        mig_buckets={},
        distribution=[{"repo": "ledoent/x", "action": "unchanged", "detail": "abc"}],
    )
    assert "Ledoent digest" in subj
    assert "fail" not in subj.lower()
    assert exit_marker == "0"
    assert "Distributor failures" not in body


def test_sync_failure_tagged_in_subject():
    subj, body, exit_marker = render_digest.render(
        sync_results=[
            _sync(200, "fast-forward"),
            _sync(403, message="Forbidden"),
        ],
        mig_buckets={},
        distribution=[],
    )
    assert "⚠️ 1 sync fail" in subj
    assert exit_marker == "1"
    assert "Sync failures" in body
    assert "Forbidden" in body


def test_distributor_failure_tagged_separately():
    subj, body, exit_marker = render_digest.render(
        sync_results=[_sync(200, "fast-forward")],
        mig_buckets={},
        distribution=[
            {"repo": "ledoent/x", "action": "failed", "detail": "PUT 403"},
            {"repo": "ledoent/y", "action": "created", "detail": "abc"},
        ],
    )
    assert "⚠️ 1 dist fail" in subj
    # Sync was clean — sync-fail shouldn't appear
    assert "sync fail" not in subj
    assert exit_marker == "1"
    assert "Distributor failures" in body
    assert "PUT 403" in body


def test_skipped_branches_are_not_failures():
    # 404 + "Branch not found" is the "branch n/a on upstream" case;
    # 422 + "does not exist" is the "branch n/a on fork" case. Neither
    # should bump the failure counter.
    subj, _, exit_marker = render_digest.render(
        sync_results=[
            _sync(404, skipped=True, message="Branch not found"),
            _sync(422, skipped=True, message="does not exist"),
        ],
        mig_buckets={},
        distribution=[],
    )
    assert "fail" not in subj.lower()
    assert exit_marker == "0"


def test_mig_count_in_subject():
    subj, body, _ = render_digest.render(
        sync_results=[_sync(200, "none")],
        mig_buckets={
            "OCA/web@19.0": [
                {"title": "[19.0][MIG] foo", "url": "http://x", "number": 1,
                 "merged_at": "2026-05-20T01:02:03Z", "user": "alice"}
            ]
        },
        distribution=[],
    )
    assert "1 MIG" in subj
    assert "[19.0][MIG] foo" in body
    assert "@alice" in body


def test_both_sync_and_dist_failures_in_subject():
    subj, _, exit_marker = render_digest.render(
        sync_results=[_sync(403, message="X")],
        mig_buckets={},
        distribution=[{"repo": "y", "action": "failed", "detail": "Y"}],
    )
    assert "⚠️ 1 sync fail" in subj
    assert "⚠️ 1 dist fail" in subj
    assert exit_marker == "2"


def test_diverged_branch_warning_in_subject_and_body():
    subj, body, exit_marker = render_digest.render(
        sync_results=[
            _sync(200, "fast-forward"),
            {
                "repo": "ledoent/social",
                "branch": "18.0",
                "status": 200,
                "message": "",
                "merge_type": "merge",
                "skipped": False,
                "ahead_by": 30,
                "behind_by": 0,
                "ahead_commits": [
                    "chore(ci): created forward-port.yml from ledoent/.github distributor",
                    "[FIX] something real",
                ],
                "diverged": True,
                "managed_overlay": False,
            },
        ],
        mig_buckets={},
        distribution=[],
    )
    assert "⚠️ 1 diverged" in subj
    # Divergence is a warning, not a failure — exit_marker should still be 0
    # so the workflow doesn't conflate "needs manual fix" with "scripts broke".
    assert exit_marker == "0"
    assert "Diverged from upstream" in body
    assert "ledoent/social" in body
    assert "30 ahead" in body
    # Unmanaged subject is shown; managed one is suppressed.
    assert "[FIX] something real" in body
    assert "forward-port.yml from ledoent/.github distributor" not in body


def test_managed_overlay_is_quiet_footer_not_alarm():
    # The everyday state for install_forward_port forks: 1 ahead by the
    # chore commit (+ possibly a sync merge). Must NOT alarm in subject
    # or body, but should show a single-line footer for transparency.
    subj, body, exit_marker = render_digest.render(
        sync_results=[
            {
                "repo": "ledoent/web",
                "branch": "18.0",
                "status": 200,
                "message": "",
                "merge_type": "none",
                "skipped": False,
                "ahead_by": 1,
                "behind_by": 0,
                "ahead_commits": [
                    "chore(ci): created forward-port.yml from ledoent/.github distributor",
                ],
                "diverged": False,
                "managed_overlay": True,
            },
        ],
        mig_buckets={},
        distribution=[],
    )
    assert "diverged" not in subj.lower()
    assert "fail" not in subj.lower()
    assert exit_marker == "0"
    assert "Diverged from upstream" not in body
    assert "Managed overlay:" in body
    assert "1 branches" in body


def test_html_escaping_in_failure_message():
    # Defensive: an API error message containing < > & shouldn't break
    # the rendered HTML (or open an XSS hole if anyone ever pipes the
    # digest into a different surface).
    subj, body, _ = render_digest.render(
        sync_results=[_sync(500, message="<script>alert(1)</script>")],
        mig_buckets={},
        distribution=[],
    )
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;" in body
