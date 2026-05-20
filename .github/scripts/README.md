# Org-control scripts

Helpers invoked by `.github/workflows/fork-sync-and-digest.yml` and
`.github/workflows/tests.yml`. The repo-root [`../../README.md`](../../README.md)
has the high-level overview; this file documents internals.

## Three-stage daily pipeline

| Stage | Script | Reads | Writes |
|---|---|---|---|
| Collect | `fork_sync_digest.py` | `../forks.yml` | `forks-parsed.json`, `sync-results.json`, `mig-buckets.json` |
| Distribute | `distribute_forward_port.py` | `forks-parsed.json`, `../templates/forward-port.yml` | `forward-port-distribution.json` |
| Render | `render_digest.py` | `sync-results.json`, `mig-buckets.json`, `forward-port-distribution.json` | `digest.html`, `digest.subject`, `digest.exit` |

The render stage is separate from collect so the distributor's
outcomes land in the email body — without the split, a 14/14
distributor failure would only appear in the artifact tarball.

## Shared helper

`_github.py` holds the stdlib-only HTTP wrapper used by both scripts.
Calls `require_token()` at startup to fail fast with an actionable
message when `GH_TOKEN` is unset, then `make_headers()` and
`request()` for the actual API calls. Add retry / rate-limit handling
here (one place) if it becomes necessary.

## Forward-port workflow template

`../templates/forward-port.yml` is the file the distributor pushes
to every opted-in fork. It uses `pull_request_target` (deliberate;
see comment in the file) and SHA-pinned actions.

## Tests

`tests/` covers the parser, the merge-upstream state mapping, and the
digest rendering. Run with:

```sh
python3 -m pytest .github/scripts/tests/ -v
```

CI runs them via `.github/workflows/tests.yml` on every PR that
touches `.github/scripts/**`, `.github/forks.yml`, or the tests
workflow itself.

## Adding a fork

Edit `../forks.yml`. Each entry:

| Key | Purpose |
|---|---|
| `repo` | `ledoent/<name>` |
| `branches` | List of branches to keep synced. Add `"20.0"` here when OCA cuts it. |
| `upstream_org` | `OCA` for OCA forks, `null` to skip MIG digest |
| `upstream_track` | Branch on upstream to scan for `[MIG]` PRs (typically the next major) |
| `install_forward_port` | `true` to push `forward-port.yml` to this fork |

## Running locally

```sh
export GH_TOKEN=$(gh auth token)   # or a fine-grained PAT
python3 .github/scripts/fork_sync_digest.py
python3 .github/scripts/distribute_forward_port.py
python3 .github/scripts/render_digest.py
cat digest.subject && open digest.html  # macOS
```

## Secrets

See [`../../README.md`](../../README.md). The single load-bearing
detail: the PAT's **resource owner must be the `ledoent` org**, not
a personal account. A PAT issued under a personal account can't see
ledoent-owned forks even with full permissions.
