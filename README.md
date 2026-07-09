# agent-transcript-collector

Collect AI coding-agent transcripts with consent, redact well-formatted secrets,
upload them to S3, and download them later for analysis.

The tool supports Claude Code, Codex, and Pi transcripts. Uploads go to
`s3://rr-agent-transcripts` in `us-east-1` by default.

## Quick Start

### First time: set up AWS SSO

Redwood users should use an AWS SSO profile named `rw-eng`.

```bash
aws configure sso
```

When prompted, use:

```text
SSO start URL: https://d-90662ff878.awsapps.com/start
SSO region: us-east-1
Profile name: rw-eng
```

Choose the Redwood engineering AWS account/role. Then log in:

```bash
aws sso login --profile rw-eng
```

If you do not have AWS SSO access, DM Tyler Tracy on Slack.

You only run `aws configure sso` once per machine. When your login expires, rerun
only:

```bash
aws sso login --profile rw-eng
```

The collector and downloader automatically use the local `rw-eng` profile when
it exists. You can also force it explicitly:

```bash
export AWS_PROFILE=rw-eng
```

### Upload Transcripts

Open the local review UI:

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  agent-transcript-collector
```

This opens <http://localhost:8899>. Preview the transcripts, select the ones you
want to share, enter your name, and click **Upload Selected**.

To upload everything without the UI:

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  agent-transcript-collector --all --name <contributor>
```

Use `--all` only when bulk upload without per-session review is intended.

### Download Transcripts

List what is available:

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  agent-transcript-downloader --list
```

Download one source into `./transcripts`:

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  agent-transcript-downloader --source claude_code
```

Download everything matched by your filters:

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  agent-transcript-downloader --all
```

With no download filter, the downloader only prints the catalog and a hint. It
will not accidentally pull the whole bucket.

## What Gets Collected

Detection runs locally on the contributor's machine. Only sources that are
actually present appear in the UI.

| Source | Default location | Override | Layout |
|---|---|---|---|
| Claude Code | `~/.claude/projects/` | `CLAUDE_CONFIG_DIR` | `<encoded-cwd>/<uuid>.jsonl` |
| Codex | `~/.codex/sessions/` | `CODEX_HOME` | `YYYY/MM/DD/rollout-*.jsonl` |
| Pi | `~/.pi/agent/sessions/` | `PI_CODING_AGENT_SESSION_DIR`, `PI_CODING_AGENT_DIR` | `--<encoded-cwd>--/<ts>_<id>.jsonl` |

The collected artifact is the raw transcript in its native format after
redaction. Preview rendering is best-effort, so harness-version schema drift
does not affect what is uploaded.

Subagents are collected and marked in the manifest. Monitor/scaffolding sessions
are excluded where the source schema makes that distinction possible.

## Download Options

| Flag | Effect |
|---|---|
| `--list` | Print available archives grouped by source. Add `--verbose` for contributor breakdowns. |
| `--source S` | Download only source `S`; repeatable, e.g. `--source claude_code --source codex`. |
| `--contributor N` | Download only contributor/collection `N`; repeatable. |
| `--prefix P` | Download only keys under S3 prefix `P`, e.g. `--prefix claude_code/alice/`. |
| `--all` | Download everything matched by the filters. |
| `--tui` | Open a checkbox selector; install with `agent-transcript-collector[tui]`. |
| `--dest DIR` | Destination folder, default `./transcripts`. |
| `--no-extract` | Keep raw `.zip` archives instead of extracting `.jsonl` files. |

By default, downloads are extracted into:

```text
transcripts/<source>/<contributor>/<group>/<session>.jsonl
transcripts/<source>/<contributor>/_manifests/<unit>.json
```

Downloads are idempotent and resumable. A unit already present on disk is
skipped, so rerunning after an interruption only fetches what is missing.

## Storage Layout

Uploads are split into size-budgeted zip units. Completed units use
deterministic keys, so rerunning an upload overwrites the same S3 objects instead
of creating duplicates:

```text
s3://rr-agent-transcripts/<source>/<contributor>/<group-hash>/part-NNN-<members-hash>.zip
```

Each zip contains redacted transcript files plus a `manifest.json` with source,
contributor, timestamp, session metadata, and redaction counts.

## Configuration

Most users only need the `rw-eng` SSO profile. These knobs are available when you
need to override defaults:

Both CLI commands automatically load a `.env` file from the directory where they
are run. Values explicitly set in your shell take precedence, so `uv run
agent-transcript-collector` works without `--env-file .env`.

| Env var | Default | Purpose |
|---|---|---|
| `AWS_PROFILE` | _(unset)_ | Standard AWS profile selector; set to `rw-eng` if you want to be explicit. |
| `CTC_AWS_PROFILE` | _(unset)_ | Collector-specific profile override. |
| `CTC_UNIT_BYTES` | `26214400` (25 MB) | Per-unit upload size budget. |
| `CTC_UPLOAD_CONCURRENCY` | `4` | Units uploaded in parallel. |
| `CTC_DOWNLOAD_CONCURRENCY` | `4` | Units downloaded in parallel. |
| `PORT` | `8899` | Local upload UI port. |

The tool uses AWS SSO profiles only. It chooses `CTC_AWS_PROFILE`, then
`AWS_PROFILE`, then `AWS_DEFAULT_PROFILE`, and finally `rw-eng`.

The bucket and region are fixed to `rr-agent-transcripts` in `us-east-1`.

## AWS Permissions

Uploading needs `s3:PutObject` on `rr-agent-transcripts/*`.

Downloading needs `s3:GetObject` on `rr-agent-transcripts/*` and `s3:ListBucket`
on `rr-agent-transcripts`.

## Security Notes

- The local UI always redacts well-formatted secrets before upload.
- Redaction is best-effort and regex-based. Contributors should still preview
  what they are sharing.
- Identity/PII redaction is optional, but secret/credential redaction is always
  on.
- Detected secrets are replaced with type-preserving mocks, not a blanket
  `[REDACTED]`, so analysts can see what kind of credential was present without
  seeing the real value.
- Do not commit credentials. Use AWS SSO for bucket access.

## Adding a New Source

Implement the `Source` protocol in `sources/base.py` as a new module under
`sources/`, then register it in `sources/__init__.py`. A source needs
`discover()` and `parse_messages()`; redaction, zipping, upload, and the UI are
source-agnostic.

## Development

```bash
uv sync
uv run pytest
```
