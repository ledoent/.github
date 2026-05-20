"""Cover the hand-rolled YAML parser in fork_sync_digest.load_forks().

The parser is fragile (regex-driven, not PyYAML) so this is where we
spend tests. If load_forks() ever stops parsing a real-world entry,
the workflow silently runs against an incomplete fork list — which
looks identical to "nothing changed" until somebody notices.
"""

from pathlib import Path

import fork_sync_digest


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "forks.yml"
    p.write_text(content)
    return p


def test_minimal_entry(tmp_path):
    p = _write(
        tmp_path,
        """
forks:
  - repo: ledoent/web
    branches: ["18.0"]
    upstream_org: OCA
    upstream_track: "19.0"
    install_forward_port: true
""",
    )
    forks = fork_sync_digest.load_forks(p)
    assert forks == [
        {
            "repo": "ledoent/web",
            "branches": ["18.0"],
            "upstream_org": "OCA",
            "upstream_track": "19.0",
            "install_forward_port": True,
        }
    ]


def test_multi_branch_list_with_two_entries(tmp_path):
    p = _write(
        tmp_path,
        """
forks:
  - repo: ledoent/web
    branches: ["18.0", "19.0"]
    upstream_org: OCA
    upstream_track: "19.0"
    install_forward_port: true

  - repo: ledoent/sale-workflow
    branches: ["18.0", "19.0"]
    upstream_org: OCA
    upstream_track: "19.0"
    install_forward_port: false
""",
    )
    forks = fork_sync_digest.load_forks(p)
    assert len(forks) == 2
    assert forks[0]["branches"] == ["18.0", "19.0"]
    assert forks[0]["install_forward_port"] is True
    assert forks[1]["install_forward_port"] is False


def test_null_upstream_for_non_oca_fork(tmp_path):
    p = _write(
        tmp_path,
        """
forks:
  - repo: ledoent/odoo
    branches: ["18.0"]
    upstream_org: null
    upstream_track: null
    install_forward_port: false
""",
    )
    forks = fork_sync_digest.load_forks(p)
    assert forks[0]["upstream_org"] is None
    assert forks[0]["upstream_track"] is None


def test_inline_comments_stripped(tmp_path):
    p = _write(
        tmp_path,
        """
forks:
  - repo: ledoent/calendar  # active feature work, 5 open PRs
    branches: ["18.0", "19.0"]  # both releases tracked
    upstream_org: OCA
    upstream_track: "19.0"
    install_forward_port: true
""",
    )
    forks = fork_sync_digest.load_forks(p)
    assert forks[0]["repo"] == "ledoent/calendar"
    assert forks[0]["branches"] == ["18.0", "19.0"]


def test_blank_lines_terminate_entries(tmp_path):
    p = _write(
        tmp_path,
        """
forks:
  - repo: ledoent/a
    branches: ["18.0"]

  - repo: ledoent/b
    branches: ["19.0"]
""",
    )
    forks = fork_sync_digest.load_forks(p)
    assert [f["repo"] for f in forks] == ["ledoent/a", "ledoent/b"]


def test_real_forks_yml_parses(tmp_path):
    # Smoke test against the actual file shipped in this repo so a
    # malformed edit there fails fast in CI rather than at run time.
    real = Path(__file__).parent.parent.parent / "forks.yml"
    forks = fork_sync_digest.load_forks(real)
    assert len(forks) >= 15  # current count is 20; allow churn
    for f in forks:
        assert "repo" in f
        assert "branches" in f and isinstance(f["branches"], list)
        assert "upstream_org" in f
        assert "upstream_track" in f
        assert "install_forward_port" in f
