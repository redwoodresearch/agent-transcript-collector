# agent-transcript-collector

Collect AI coding-agent transcripts with consent, redact secrets locally, upload
them to S3, and download them later for analysis.

Supported sources: **Claude Code**, **Codex**, **Cursor**, and **Pi**. Uploads go
to `s3://rr-agent-transcripts` in `us-east-1`.

- [Prerequisites](#prerequisites)
- [Install](#install)
- [Upload transcripts](#upload-transcripts)
- [Automatic hourly uploads](#automatic-hourly-uploads)
- [Download transcripts](#download-transcripts)
- [What gets collected](#what-gets-collected)
- [Privacy and redaction](#privacy-and-redaction)
- [Storage layout](#storage-layout)
- [Configuration](#configuration)

## Prerequisites

You need an AWS SSO profile for the Redwood General Sandbox account. Follow the
AWS prerequisites and profile setup in the
[Redwood devbox getting-started guide](https://github.com/redwoodresearch/research-devboxes/blob/main/docs/getting-started.md),
then point the collector at that profile:

```bash
export AWS_PROFILE=<profile>
```

The profile name is a local label. Use the same name when refreshing an expired
session with `aws sso login --profile <profile>`.

Uploads require `s3:PutObject` and `s3:ListBucket`: the collector lists its
receipt prefix so it can skip transcript versions that were already uploaded.
Downloads additionally require `s3:GetObject`.

## Install

Run it directly with no install:

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  agent-transcript-collector
```

Or install once so the commands are on your `PATH`:

```bash
uv tool install 'git+https://github.com/redwoodresearch/agent-transcript-collector'
```

Either way you get three commands:

| Command | Purpose |
|---|---|
| `agent-transcript-collector` | Local review UI, uploads, watcher setup |
| `agent-transcript-watcher` | Watcher status and removal |
| `agent-transcript-downloader` | List and download uploaded transcripts |

Examples below use the short names. If you prefer `uvx`, prefix each one with
`uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector'`.

## Upload transcripts

Open the local review UI:

```bash
agent-transcript-collector
```

This serves a web UI at <http://localhost:8899> (if that port is busy, the next
free port is used). Preview the transcripts, select the ones you want to share,
enter your name, and click **Upload Selected**. Transcripts already uploaded
under that contributor name are marked **uploaded**.

To skip the UI and upload everything found on the machine:

```bash
agent-transcript-collector --all --name <contributor>
```

Use `--all` only when you intend a bulk upload without per-session review.

## Automatic hourly uploads

The review UI can install a per-user hourly watcher on macOS or Linux:

1. Enter the contributor name that should own the uploads.
2. Check **hourly** beside each folder you consent to share.
3. Click **Install / Update**.

The watcher uploads every existing transcript in those exact folders, then checks
about once an hour for new transcripts or changed content. A transcript that
grows after an upload is stored as a new content version; unchanged content is
skipped. Selecting a folder does not implicitly select similarly named or
descendant folders.

Installation uses a macOS LaunchAgent or a Linux systemd user timer, so it never
needs `sudo` and runs only while your login session is available (unless you
separately configured systemd lingering). `uv` must stay installed at the path
recorded during setup.

The watcher uses the configured AWS SSO profile but cannot open an interactive
login. If the last-run status reports expired credentials, refresh it yourself:

```bash
aws sso login --profile <profile>
```

Status and removal are also available from the terminal:

```bash
agent-transcript-watcher status
agent-transcript-watcher uninstall
```

`uninstall` stops future runs but preserves your folder choices, last-run state,
and logs; add `--purge` to delete them too.

Your folder choices are stored with user-only permissions:

| | macOS | Linux |
|---|---|---|
| Config | `~/Library/Application Support/agent-transcript-collector/` | `${XDG_CONFIG_HOME:-~/.config}/agent-transcript-collector/` |
| Logs | `~/Library/Logs/agent-transcript-collector/watcher.log` | `${XDG_STATE_HOME:-~/.local/state}/agent-transcript-collector/watcher.log` |

## Download transcripts

List what is available:

```bash
agent-transcript-downloader --list
```

Download one source into `./transcripts`:

```bash
agent-transcript-downloader --source claude_code
```

Download everything matched by your filters:

```bash
agent-transcript-downloader --all
```

Given no download filter, the downloader only prints the catalog and a hint, so
it will not accidentally pull the whole bucket.

| Flag | Effect |
|---|---|
| `--list` | Print available archives grouped by source. Add `--verbose` for contributor breakdowns. |
| `--source S` | Download only source `S`; repeatable, e.g. `--source claude_code --source codex`. |
| `--contributor N` | Download only contributor/collection `N`; repeatable. |
| `--prefix P` | Download only keys under S3 prefix `P`, e.g. `--prefix claude_code/alice/`. |
| `--all` | Download everything matched by the filters. |
| `--tui` | Open a checkbox selector; needs `textual` installed (see below). |
| `--dest DIR` | Destination folder, default `./transcripts`. |
| `--no-extract` | Keep raw `.zip` archives instead of extracting `.jsonl` files. |
| `--concurrency N` | Parallel unit downloads, default `4`. |

The `--tui` selector needs the optional `textual` dependency:

```bash
uv tool install --with textual 'git+https://github.com/redwoodresearch/agent-transcript-collector'
```

Extracted downloads land in:

```text
transcripts/<source>/<contributor>/<group>/<session>.jsonl
transcripts/<source>/<contributor>/_manifests/<unit>.json
```

Downloads are idempotent and resumable. A unit already present on disk is
skipped, so rerunning after an interruption only fetches what is missing.

## What gets collected

Detection runs locally on your machine, and only sources actually present appear
in the UI:

| Source | Default location | Override | Layout |
|---|---|---|---|
| Claude Code | `~/.claude/projects/` | `CLAUDE_CONFIG_DIR` | `<encoded-cwd>/<uuid>.jsonl` |
| Codex | `~/.codex/sessions/` | `CODEX_HOME` | `YYYY/MM/DD/rollout-*.jsonl` |
| Cursor | `~/.cursor/projects/` | `CURSOR_HOME` | `<encoded-project>/agent-transcripts/<id>/<id>.jsonl` |
| Pi | `~/.pi/agent/sessions/` | `PI_CODING_AGENT_SESSION_DIR`, `PI_CODING_AGENT_DIR` | `--<encoded-cwd>--/<ts>_<id>.jsonl` |

The uploaded artifact is the raw transcript in its native format after
redaction. Preview rendering is best-effort, so schema drift between harness
versions never changes what is uploaded.

Cursor Agent JSONL transcripts include user messages, assistant text, and
tool-call inputs; Cursor does not record tool outputs in these native files.

Subagents are collected and marked in the manifest. Monitor and scaffolding
sessions are excluded where the source schema makes that distinction possible.

## Privacy and redaction

Redaction happens on your machine before anything is written to an archive, and
the count of replacements is recorded in each unit's manifest.

Two kinds of content are rewritten:

- **Credentials** matched by a set of high-precision patterns: AWS keys, `sk-`
  and `sk-ant-` API keys, GitHub, GitLab, Slack, Stripe, HuggingFace, and Google
  keys, JWTs, PEM private keys, database and messaging connection URIs, and
  explicit `password`/`token`/`api_key` assignments.
- **Identity**: usernames in `/home/<user>/` and `/Users/<user>/` paths and in
  dash-encoded project keys, your local username as a bare token, and email
  addresses. Paths become `/home/[USER]/` and addresses become `[EMAIL]`.
  Shared system logins such as `ubuntu` or `root` are left alone; extend that
  stoplist with `CTC_USERNAME_STOPLIST`.

Credentials are replaced with type-preserving **mocks** rather than a blanket
`[REDACTED]`, so a reader can still tell which kind of credential appeared and
trace one secret through a transcript (env var to tool argument to file write to
echoed output). The same real secret maps to the same mock everywhere within a
single run, but the salt is random per process and discarded on exit, so nothing
reverses a mock back to the original and a guessed secret cannot be confirmed.
Every mock embeds the marker `4d4f434b` (hex for `MOCK`), which you can grep for
to enumerate synthetic values.

The patterns favor precision over recall: they only match values that are almost
certainly secrets, so unusual or unformatted credentials can survive. Review
what you select before uploading rather than treating redaction as a guarantee.

## Storage layout

Uploads are split into size-budgeted zip units. Completed units use
deterministic keys, so rerunning an upload overwrites the same S3 objects instead
of creating duplicates:

```text
s3://rr-agent-transcripts/<source>/<contributor>/<group-hash>/part-NNN-<members-hash>.zip
```

Each zip contains redacted transcript files plus a `manifest.json` with the
source, contributor, timestamp, session metadata, and redaction counts.

Successful uploads also write opaque per-transcript receipts under
`<source>/<contributor>/_uploaded/<identity-hash>/<archive-hash>`. The archive
hash covers both the source bytes and the redaction policy, and every upload
path checks these receipts before creating an archive, so unchanged sessions are
skipped consistently. Older receipt formats are not treated as exact matches.

## Configuration

Normally the only thing you set is the SSO profile from the
[Redwood devbox guide](https://github.com/redwoodresearch/research-devboxes/blob/main/docs/getting-started.md).
These knobs exist for overriding defaults:

| Env var | Default | Purpose |
|---|---|---|
| `AWS_PROFILE` | _(unset)_ | Standard AWS profile selector; set it to your General Sandbox profile. |
| `CTC_AWS_PROFILE` | _(unset)_ | Collector-specific profile override. |
| `CTC_UNIT_BYTES` | `26214400` (25 MB) | Per-unit upload size budget. |
| `CTC_UPLOAD_CONCURRENCY` | `4` | Units uploaded in parallel. |
| `CTC_DOWNLOAD_CONCURRENCY` | `4` | Units downloaded in parallel. |
| `CTC_USERNAME_STOPLIST` | _(unset)_ | Comma-separated logins to never redact. |
| `PORT` | `8899` | Local review UI port. |

Profile resolution prefers `CTC_AWS_PROFILE`, then `AWS_PROFILE`, then
`AWS_DEFAULT_PROFILE`. For compatibility with existing setups it falls back to
the local profile name `rw-eng`. The bucket and region are fixed to
`rr-agent-transcripts` in `us-east-1`.
