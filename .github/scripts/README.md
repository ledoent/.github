# Org workflow scripts

Helpers invoked by workflows in `../workflows/`. Two scripts run daily as a
single chain inside `fork-sync-and-digest.yml`:

## 1. `fork_sync_digest.py` — sync + digest assembly

- Reads `../forks.yml`.
- For each fork, calls `POST /repos/{repo}/merge-upstream` on every branch
  in `branches:` (e.g. `["18.0", "19.0"]`). Non-existent branches return
  HTTP 422 and are marked as skipped, not failed.
- Queries each unique upstream `(org, repo)` once for PRs with `[MIG]` in
  the title merged in the last 24h on `upstream_track`.
- Writes `digest.html`, `digest.subject`, `digest.exit` for the email step.
- Writes `forks-parsed.json` for the next stage.

## 2. `distribute_forward_port.py` — push `forward-port.yml` to opted-in forks

- Reads `forks-parsed.json`.
- For each fork with `install_forward_port: true`, PUTs
  `.github/workflows/forward-port.yml` (from `../templates/forward-port.yml`)
  onto the fork's default branch via the Contents API.
- Idempotent: no-ops when the file already matches the template.

## The forward-port workflow itself

Lives at `../templates/forward-port.yml`. Once installed on a fork, label
any PR with `port:<branch>` (e.g. `port:19.0`) and on merge the
[`korthout/backport-action`](https://github.com/korthout/backport-action)
cherry-picks the squash commit onto the named branch and opens a
follow-up PR. Conflicts are reported in the PR body for manual resolution.

## Running locally

```sh
export GH_TOKEN=...   # PAT with Contents:write on ledoent/* + public_repo
python3 .github/scripts/fork_sync_digest.py
python3 .github/scripts/distribute_forward_port.py
cat digest.html
```

## Adding a fork

Edit `.github/forks.yml`. Each entry:

| Key | Purpose |
|---|---|
| `repo` | `ledoent/<name>` |
| `branches` | List of branches to keep synced. Add `20.0` here when OCA cuts it. |
| `upstream_org` | `OCA` for OCA forks, `null` to skip MIG digest |
| `upstream_track` | Branch on upstream to scan for `[MIG]` PRs (typically the next major) |
| `install_forward_port` | `true` to push `forward-port.yml` to this fork |

## Secrets

Set at the org or `ledoent/.github` repo level:

- `LEDOENT_FORK_SYNC_TOKEN` — fine-grained PAT, scopes:
  - `Contents: write` on `ledoent/*`
  - `Metadata: read`
  - `public_repo` (for searching OCA upstream PRs)
- `SMTP_SERVER` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_TO` / `SMTP_FROM`
  - `SMTP_USERNAME`: SES SMTP-credential AKID
  - `SMTP_PASSWORD`: derived from the IAM secret via SES's signing
    algorithm (NOT the raw IAM secret)
  - `SMTP_FROM`: full From: header, e.g. `Ledoent CI <ci@ledoweb.com>`
    on a domain SES has verified (`ledoweb.com` is already verified
    in account 058264328562, region us-east-1)
