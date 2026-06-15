"""Cover the feature-branch rebaser's branch-selection + short-circuit.

The actual git rebase/push runs only on a GitHub runner against a live
fork, so it isn't unit-tested here. What IS pinned: the glob matching
(a wrong glob silently rebases nothing or the wrong branches) and the
no-match short-circuit (must NOT clone when nothing matches, or every
fork without feature branches pays a pointless clone every night).
"""

import re

import fork_sync_digest as fsd


def _paged_gh(pages):
    """Fake gh() that serves {page_number: [branch-dict, ...]} by ?page=N."""
    def fake_gh(method, path):
        # [?&]-anchored so it doesn't match the `per_page=100` earlier in the qs
        p = int(re.search(r"[?&]page=(\d+)", path).group(1))
        return (200, pages.get(p, []))
    return fake_gh


def test_list_matching_branches_globs(monkeypatch):
    monkeypatch.setattr(fsd, "gh", _paged_gh({
        1: [
            {"name": "18.0"}, {"name": "19.0"}, {"name": "ledoent"},
            {"name": "19.0-mig-ddmrp_adjustment"}, {"name": "19.0-fix-foo"},
        ],
    }))
    out = fsd._list_matching_branches("ledoent/ddmrp", ["19.0-mig-*", "19.0-fix-*"])
    # sorted, deduped, only the matching feature branches
    assert out == ["19.0-fix-foo", "19.0-mig-ddmrp_adjustment"]


def test_list_matching_branches_pagination(monkeypatch):
    page1 = [{"name": f"19.0-mig-m{i:03d}"} for i in range(100)]  # full page
    page2 = [{"name": "19.0-mig-zzz"}, {"name": "other"}]         # partial → stop
    monkeypatch.setattr(fsd, "gh", _paged_gh({1: page1, 2: page2}))
    out = fsd._list_matching_branches("r", ["19.0-mig-*"])
    assert "19.0-mig-zzz" in out          # second page consumed
    assert "other" not in out             # non-match dropped
    assert len(out) == 101


def test_rebase_feature_branches_no_matches_does_not_clone(monkeypatch):
    monkeypatch.setattr(fsd, "gh", lambda m, p: (200, []))

    def boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("must not run git when no branches match")

    monkeypatch.setattr(fsd.subprocess, "run", boom)
    assert fsd.rebase_feature_branches("ledoent/ddmrp", ["19.0-mig-*"], "19.0") == []
