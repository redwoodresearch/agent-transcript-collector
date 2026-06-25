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

**Subagents are collected and marked; monitors are excluded.** Spawned task
subagents are included and flagged `is_subagent` in the manifest (with their
`parent` session id), and shown with a "subagent" badge in the UI:
- **Claude Code** — `<session-id>/subagents/agent-*.jsonl`.
- **Codex** — classified by `session_meta.source` (per the upstream
  `SessionSource`/`SubAgentSource` schema): genuine task subagents
  (`{"subagent": {"thread_spawn": …}}`, with `parent` taken from
  `parent_thread_id`, plus the catch-all `{"subagent": {"other": …}}`) are kept
  and marked; **automated scaffolding is dropped** —
  `{"subagent": "review"|"compact"|"memory_consolidation"}` and
  `{"internal": …}`. (Top-level `"cli"`/`"vscode"`/`{"custom": …}` sessions are
  kept, unmarked.)
- **Pi** — `pi-subagents` task runs at
  `~/.pi/agent/sessions/<parent>/<runId>/run-N/session.jsonl` (parent from the
  path) and forked sessions (parent from the `parentSession` header). Only
  `session.jsonl` is collected; `events.jsonl` and `subagent-artifacts/*.jsonl`
  in those run dirs are different schemas and are skipped.

## How a contributor runs it

```bash
CTC_AWS_ACCESS_KEY_ID=AKIA... \
CTC_AWS_SECRET_ACCESS_KEY='...' \
  uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  agent-transcript-collector
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
... agent-transcript-collector --all --name <contributor>
```

`--all` skips the UI entirely and uploads **every** transcript from **every**
detected source after redaction. There is no preview or selection step, so only
use it when bulk upload without per-session review is intended.

## Storage layout

Uploads are split into **size-budgeted units** (one working-dir group per unit; a
group over `CTC_UNIT_BYTES`, default 25 MB, is split into parts of whole sessions
— transcripts are never split). Each unit is one zip with a **deterministic key**,
so an aborted upload's completed units stay durable in S3 and re-running
overwrites the same keys in place (idempotent — no duplicates):

```
s3://<bucket>/<source>/<contributor>/<group-hash>/part-NNN-<members-hash>.zip
   e.g.  claude_code/nickkuhn/g1a2b3c4d5e6/part-000-9f8e7d6c.zip
         codex/nickkuhn/g0f1e2d3c4b5/part-000-aa11bb22.zip
```

Each unit zip contains `<group>/<session>.jsonl` (redacted, subagents nested
under `…/<parent>/subagents/`) plus a `manifest.json` recording `source`,
`source_format`, contributor, timestamp, and per-session group/redaction info.

Uploads run as a **background job** on the local server, so closing the browser
tab doesn't abort them — reopening the page re-attaches to the in-progress job.
(The job still ends if the tool's process is stopped; just re-run it — completed
units are overwritten in place, not duplicated.)

## Configuration

| Env var | Default | Purpose |
|---|---|---|
| `CTC_S3_BUCKET` | `rr-agent-transcripts` | Destination bucket |
| `CTC_S3_REGION` | `us-east-1` | Bucket region (must match the bucket) |
| `CTC_AWS_ACCESS_KEY_ID` | _(unset)_ | Upload key; if unset, boto3's default credential chain is used |
| `CTC_AWS_SECRET_ACCESS_KEY` | _(unset)_ | Upload secret |
| `CTC_UNIT_BYTES` | `26214400` (25 MB) | Per-unit upload size budget |
| `CTC_UPLOAD_CONCURRENCY` | `4` | Units uploaded in parallel |
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
- **Secret/credential redaction is always on** (not toggleable) so it can never
  be disabled by accident; only the identity/PII pass is optional.
- Redaction is best-effort and regex-based (see `redactor.py`): it catches
  well-formatted secrets and credentials — AWS keys, `sk-`/token patterns, JWTs,
  PEM keys, DB/messaging connection URIs (`postgres://…@`, etc.), Neon (`npg_…`)
  and RunPod (`…@ssh.runpod.io`) credentials — but **not** proprietary source,
  internal paths, or PII embedded in prose.
  Contributors should understand what a transcript contains before sharing it.
- **Secrets are replaced with type-preserving mocks, not a blanket `[REDACTED]`.**
  A detected secret is swapped for a fake of the same type (an `sk-ant-…` stays
  an `sk-ant-…`, `postgres://user:pass@host` keeps its scheme and host), so an
  analyst can see *what kind* of credential was present and trace one secret's
  flow through a transcript — without ever exposing the real value. The same real
  secret maps to the same mock everywhere within a single run (the mapping uses a
  random per-process salt that is never stored, so it is irreversible and a
  guessed secret can't be confirmed). Every mock embeds the marker `4d4f434b`
  (hex of `MOCK`); grep `(?i)4d4f434b` to enumerate or confirm synthetic values.

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
