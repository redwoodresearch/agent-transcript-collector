# agent-transcript-collector

A small tool for collecting AI coding-agent session transcripts **with consent**
and uploading them to a shared S3 bucket.

It discovers transcripts from multiple agent harnesses on the contributor's
machine, lets them preview and select which sessions to share, redacts
well-formatted secrets, zips the selection (one zip per source), and uploads to
S3 under a source-first key.

## Supported sources

Detection runs on each contributor's machine; only harnesses that are actually
present show up in the UI. Each respects its own config-dir env override.

| Source | Default location | Override | Layout |
|---|---|---|---|
| **Claude Code** | `~/.claude/projects/` | `CLAUDE_CONFIG_DIR` | `<encoded-cwd>/<uuid>.jsonl` |
| **Codex** | `~/.codex/sessions/` | `CODEX_HOME` | `YYYY/MM/DD/rollout-*.jsonl` |
| **Pi** | `~/.pi/agent/sessions/` | `PI_CODING_AGENT_SESSION_DIR`, `PI_CODING_AGENT_DIR` | `--<encoded-cwd>--/<ts>_<id>.jsonl` (+ flat fallback in the agent dir) |

Sessions are grouped by working directory within each source. The canonical
artifact collected is the **raw (redacted) transcript** in its native format;
previews are best-effort, so harness-version schema drift never affects what is
stored.

**Codex subagent sessions are excluded.** Codex records `source: "cli"` for
top-level interactive sessions and `source: {"subagent": …}` for spawned
subagents (e.g. the `guardian` approval monitor, whose transcript is reviewer
scaffolding rather than a real user↔agent conversation). Only top-level sessions
are collected.

## How a contributor runs it

```bash
CTC_AWS_ACCESS_KEY_ID=AKIA... \
CTC_AWS_SECRET_ACCESS_KEY='...' \
  uvx --from 'git+https://github.com/nick-kuhn/claude-transcript-collector' \
  claude-transcript-collector
```

The destination bucket defaults to `rr-agent-transcripts` (in `us-east-1`), so
contributors only need to supply the uploader credentials. Override with
`CTC_S3_BUCKET` / `CTC_S3_REGION` if those change.

This opens a local web UI at <http://localhost:8899>. The contributor previews
each session (redacted by default), ticks the ones to share (per session, per
working directory, or per source), enters their name, and clicks **Upload
Selected**.

### Headless / no-UI mode

```bash
... claude-transcript-collector --all --name <contributor>
```

`--all` skips the UI entirely and uploads **every** transcript from **every**
detected source after redaction. There is no preview or selection step, so only
use it when bulk upload without per-session review is intended.

## Storage layout

One zip per source per upload, keyed source-first so each harness's data can be
consumed independently:

```
s3://<bucket>/<source>/<contributor>/<timestamp>-<hex>.zip
   e.g.  claude_code/nickkuhn/20260624-101500-ab12cd34.zip
         codex/nickkuhn/20260624-101500-ef56ab78.zip
         pi/nickkuhn/20260624-101500-9a8b7c6d.zip
```

Each zip contains `<group>/<session>.jsonl` (redacted) plus a `manifest.json`
recording `source`, `source_format`, the contributor, timestamp, and per-session
group/redaction info.

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CTC_S3_BUCKET` | `rr-agent-transcripts` | Destination bucket |
| `CTC_S3_REGION` | `us-east-1` | Bucket region (must match the bucket) |
| `CTC_AWS_ACCESS_KEY_ID` | _(unset)_ | Upload key; if unset, boto3's default credential chain is used |
| `CTC_AWS_SECRET_ACCESS_KEY` | _(unset)_ | Upload secret |
| `PORT` | `8899` | Local UI port |

If the `CTC_AWS_*` variables are not set, the tool falls back to boto3's normal
credential resolution (standard AWS env vars, shared config/credentials files,
SSO, or instance/container roles).

## Minimal IAM policy for the upload key

The tool only calls `s3:PutObject`. Scope the distributed upload key to exactly
that, on the one bucket:

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": "s3:PutObject",
    "Resource": "arn:aws:s3:::rr-agent-transcripts/*"
  }]
}
```

## Security notes

- **Never commit credentials.** The upload key is passed via environment
  variables at runtime, not stored in the repo.
- A key embedded in a command handed to many contributors is effectively a
  shared, exposed credential. Scope it to `s3:PutObject` only (above) so a leak
  can't read, delete, or enumerate, and rotate it if it leaves trusted hands.
- Redaction is best-effort and regex-based (see `redactor.py`): it catches
  well-formatted secrets (AWS keys, `sk-`/token patterns, JWTs, PEM keys, etc.)
  but **not** proprietary source, internal paths, or PII embedded in prose.
  Contributors should understand what a transcript contains before sharing it.

## Adding a new source

Implement the `Source` protocol in `sources/base.py` as a new module under
`sources/`, then register it in `sources/__init__.py`. A source needs `discover()`
(returns groups of `Session`s found on disk) and `parse_messages()` (raw text ->
`[{role, text}]` for preview). Redaction, zipping, upload, and the UI are all
source-agnostic and need no changes.

## Development

```bash
uv sync
uv run pytest
```
