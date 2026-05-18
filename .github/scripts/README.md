# Org workflow scripts

Helpers invoked by workflows in `../workflows/`.

## `fork_sync_digest.py`

Runs daily via `fork-sync-and-digest.yml`. Reads `../forks.yml`, calls
`POST /repos/{repo}/merge-upstream` on each fork's base branch, and queries
upstream OCA for recently-merged `[MIG]` PRs on the configured track. Emits
`digest.html` + `digest.subject` in CWD, which the email step consumes.

### Running locally

```sh
export GH_TOKEN=...   # PAT with Contents:write on ledoent/* + public_repo
python3 .github/scripts/fork_sync_digest.py
cat digest.html
```

### Adding a fork

Edit `.github/forks.yml` only. Each entry pins:
- `repo` — `ledoent/<name>`
- `base` — branch on the fork to sync from upstream
- `upstream_org` — set to `OCA` to also include the [MIG] digest for this
  upstream, or `null` to skip the digest portion
- `upstream_track` — branch on upstream to watch (typically the next major
  version's branch since [MIG] PRs land there)

### Secrets

Configured at the org or repo level on `ledoent/.github`:
- `LEDOENT_FORK_SYNC_TOKEN` — fine-grained PAT
- `SMTP_SERVER` / `SMTP_USERNAME` / `SMTP_PASSWORD` / `SMTP_TO`
