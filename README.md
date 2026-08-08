# agent-transcript-collector

Collect AI coding-agent transcripts with consent, redact well-formatted secrets,
upload them to S3, and download them later for analysis.

The tool supports Claude Code, Codex, Cursor, and Pi transcripts. Uploads go to
`s3://rr-agent-transcripts` in `us-east-1` by default.

## Quick Start

### First time: set up AWS SSO

Follow the AWS prerequisites and profile setup in the
[Redwood devbox getting-started guide](https://github.com/redwoodresearch/research-devboxes/blob/main/docs/getting-started.md).
Use a profile for the Redwood General Sandbox account and make that profile
available to the collector:

```bash
export AWS_PROFILE=<profile>
```

The profile name is a local label. Use the same profile when refreshing an
expired session with `aws sso login --profile <profile>`.

### Upload Transcripts

Open the local review UI:

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  agent-transcript-collector
```

This opens <http://localhost:8899>. Preview the transcripts, select the ones you
want to share, enter your name, and click **Upload Selected**. Transcripts
previously uploaded under that contributor name are marked **uploaded**.

To upload everything without the UI:

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  agent-transcript-collector --all --name <contributor>
```

Use `--all` only when bulk upload without per-session review is intended.

### Hourly Watcher

The local review UI can install a per-user hourly watcher on macOS or Linux:

1. Enter the contributor name that should own the uploads.
2. Check **hourly** beside each folder you consent to share.
3. Click **Install / Update**.

The watcher uploads every existing transcript in those exact folders, then
checks about once an hour for new transcripts or changed content. A transcript
that grows after an upload is stored as a new content version; unchanged content
is skipped. Selecting a folder does not implicitly select similarly named or
descendant folders.

The installer uses a macOS LaunchAgent or Linux systemd user timer. It runs only
while the user's login session is available (unless systemd lingering was
separately configured) and never requires `sudo`. **Uninstall** stops future
runs while preserving the folder choices, last-run state, and logs.

`uv` must remain installed at the path recorded during setup. The watcher uses
the configured AWS SSO profile, but it cannot open an interactive login. If the
last-run status reports expired credentials, run:

```bash
aws sso login --profile <profile>
```

Watcher status and removal are also available from the terminal:

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  agent-transcript-watcher status
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  agent-transcript-watcher uninstall
```

Configuration is stored with user-only permissions under
`~/Library/Application Support/agent-transcript-collector/` on macOS or
`${XDG_CONFIG_HOME:-~/.config}/agent-transcript-collector/` on Linux. Logs use
`~/Library/Logs/agent-transcript-collector/watcher.log` on macOS and
`${XDG_STATE_HOME:-~/.local/state}/agent-transcript-collector/watcher.log` on
Linux.

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
| Cursor | `~/.cursor/projects/` | `CURSOR_HOME` | `<encoded-project>/agent-transcripts/<id>/<id>.jsonl` |
| Pi | `~/.pi/agent/sessions/` | `PI_CODING_AGENT_SESSION_DIR`, `PI_CODING_AGENT_DIR` | `--<encoded-cwd>--/<ts>_<id>.jsonl` |

The collected artifact is the raw transcript in its native format after
redaction. Preview rendering is best-effort, so harness-version schema drift
does not affect what is uploaded.

Cursor Agent JSONL transcripts include user messages, assistant text, and
tool-call inputs. Cursor does not include tool outputs in these native files.

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

Successful uploads also create opaque per-transcript receipts under
`<source>/<contributor>/_uploaded/<identity-hash>/<archive-hash>`. The archive
hash covers both the source bytes and the redaction policy. Every upload path
checks these receipts before creating an archive, so unchanged sessions are
skipped consistently. Older receipt formats are not treated as exact matches.

## Configuration

Use the General Sandbox SSO profile configured through the
[Redwood devbox guide](https://github.com/redwoodresearch/research-devboxes/blob/main/docs/getting-started.md).
These knobs are available when you need to override defaults:

| Env var | Default | Purpose |
|---|---|---|
| `AWS_PROFILE` | _(unset)_ | Standard AWS profile selector; set it to your General Sandbox profile. |
| `CTC_AWS_PROFILE` | _(unset)_ | Collector-specific profile override. |
| `CTC_UNIT_BYTES` | `26214400` (25 MB) | Per-unit upload size budget. |
| `CTC_UPLOAD_CONCURRENCY` | `4` | Units uploaded in parallel. |
| `CTC_DOWNLOAD_CONCURRENCY` | `4` | Units downloaded in parallel. |
| `PORT` | `8899` | Local upload UI port. |

The tool chooses `CTC_AWS_PROFILE`, then `AWS_PROFILE`, then
`AWS_DEFAULT_PROFILE`. For compatibility with existing setups, it falls back to
the local profile name `rw-eng`.

The bucket and region are fixed to `rr-agent-transcripts` in `us-east-1`.
