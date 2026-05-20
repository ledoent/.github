# `ledoent/.github` — org control plane

This repo holds two things that apply to the whole `ledoent` org:

1. **Community health files** under `profile/` — org-level README, avatar,
   banner (rendered on https://github.com/ledoent).

2. **Automation that operates across all ledoent forks** — daily
   upstream sync, MIG-PR digest email, and forward-port workflow
   distribution. See `.github/workflows/fork-sync-and-digest.yml` and
   `.github/scripts/README.md` for details.

## Automation at a glance

| Workflow | Trigger | What it does |
|---|---|---|
| `fork-sync-and-digest` | daily 13:00 UTC + manual | Syncs each fork's tracked branches from upstream, distributes the forward-port workflow to opted-in forks, queries upstream OCA for `[MIG]` PRs, and emails an HTML digest |
| `tests` | PR + push to main | Runs pytest on the org-control scripts |

The tracked fork list lives in [`.github/forks.yml`](.github/forks.yml).
Edit that file (not the workflow YAML) when adding or removing forks.

## Secrets required

Set at the org level (Settings → Secrets and variables → Actions) or
on this repo:

| Secret | What it is | Notes |
|---|---|---|
| `LEDOENT_FORK_SYNC_TOKEN` | Fine-grained PAT | **Resource owner MUST be the `ledoent` org**, not a personal account — the forks live under the org. Scopes: Contents R/W, Workflows R/W, Metadata Read, public_repo. |
| `SMTP_SERVER` | SMTP host | e.g. `email-smtp.us-east-1.amazonaws.com` for SES |
| `SMTP_USERNAME` | SMTP auth username | For SES, the SMTP credential AKID |
| `SMTP_PASSWORD` | SMTP auth password | For SES, derived from the IAM secret via the [SES SMTP-credential algorithm](https://docs.aws.amazon.com/ses/latest/dg/smtp-credentials.html#smtp-credentials-convert) — **NOT the raw IAM secret** |
| `SMTP_TO` | Digest recipient | e.g. `dkendall@ledoweb.com` |
| `SMTP_FROM` | Full `From:` header | e.g. `Ledoent CI <ci@ledoweb.com>` — domain must be SES-verified |

## Adding a new fork

Edit `.github/forks.yml` and add an entry:

```yaml
- repo: ledoent/<name>
  branches: ["18.0", "19.0"]
  upstream_org: OCA
  upstream_track: "19.0"
  install_forward_port: true
```

The next scheduled run (or a manual dispatch) picks it up. Add `"20.0"`
to `branches:` when OCA cuts that release.

## Forward-port workflow

Forks that opt in (`install_forward_port: true`) get
`.github/workflows/forward-port.yml` installed by the distributor.
Label any fork-internal PR with `port:19.0` (or other branch name);
on merge, the labelled commit is cherry-picked onto that branch and a
follow-up PR is opened. Conflicts surface in the PR body for manual
resolution.

## Local development

```sh
# Run the tests
python3 -m pytest .github/scripts/tests/ -v

# Run the collect step against the live API (uses your gh CLI token —
# limit blast radius accordingly)
GH_TOKEN=$(gh auth token) python3 .github/scripts/fork_sync_digest.py

# Then render the digest from the collected JSON
python3 .github/scripts/render_digest.py
```

Generated `digest.html` opens in any browser; `digest.subject` is
what the email step sends.
