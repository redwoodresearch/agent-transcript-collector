# agent-transcript-collector

Review, redact, and upload transcripts from Claude Code, Codex, Cursor, and Pi.
Uploads go to `s3://rr-agent-transcripts/mts-trans/` in `us-east-1`.

## Requirements

- macOS or Linux
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- An AWS SSO profile with access to the Redwood General Sandbox account

Follow the AWS setup in the
[Redwood devbox guide](https://github.com/redwoodresearch/research-devboxes/blob/main/docs/getting-started.md),
then select the profile in your shell:

```bash
export AWS_PROFILE=<profile>
```

If the session expires, refresh it with:

```bash
aws sso login --profile <profile>
```

## Install

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  rr-trans
```

This installs `rr-trans`, starts a per-user background service, and opens the
review UI at <http://localhost:8123>. Run the command again to update the CLI and
restart the UI from the latest `main` branch. If automatic uploads have already
been configured, the command also updates and reloads the watcher without
changing its saved consent. The services check `main` for updates when they run.

No `sudo` is required. The installer uses a LaunchAgent on macOS and a systemd
user service on Linux.

## Upload transcripts

Open <http://localhost:8123>, enter the contributor name, review the discovered
sessions, and select the transcripts to upload. Click a session summary to
preview its redacted text before uploading.

For a temporary foreground server, run:

```bash
rr-trans ui
```

It opens <http://localhost:8899> by default and uses the next available port if
8899 is busy.

### Automatic uploads

In the review UI:

1. Enter the contributor name.
2. Select **auto upload** for each project you consent to share.
3. Click **Enable**.

The watcher runs immediately and then once an hour. It uploads new transcripts
and replaces an existing transcript ZIP when the local transcript or its
attachments have changed. Unchanged transcripts are skipped.

Each check also refreshes the watcher from the supported release branch. When
that branch advances to a new Git revision, the watcher updates the installed
`rr-trans` CLI. An update failure is recorded in watcher state but does not stop
the transcript upload.

The watcher cannot open an interactive AWS login. If its status reports expired
credentials, run `aws sso login --profile <profile>` yourself. `uv` must remain
at the path recorded when the watcher was enabled.

```bash
rr-trans watcher status
rr-trans watcher uninstall
rr-trans watcher uninstall --purge
```

`uninstall` removes the scheduled job but keeps its configuration, last-run
state, and logs. `--purge` also removes the configuration and state.

## Commands

| Command | Purpose |
|---|---|
| `rr-trans` or `rr-trans install` | Update the CLI and UI, plus the watcher when configured |
| `rr-trans ui` | Run the review UI in the foreground |
| `rr-trans ui-service install` | Reinstall the UI and configured watcher |
| `rr-trans ui-service status` | Show background UI status and paths |
| `rr-trans ui-service uninstall` | Remove the background UI service |
| `rr-trans watcher status` | Show automatic-upload status |
| `rr-trans watcher uninstall [--purge]` | Remove automatic uploads |
| `rr-trans tui` | Browse uploaded transcript keys in S3 |

`rr-trans tui` never modifies S3. It starts at `mts-trans/`; use
`--prefix mts-trans/<contributor>/` to narrow the listing. Press Enter to expand
a folder, `v` to view a selected transcript's ATIF in Vim, and `q` to quit. The
viewer downloads the ATIF to a private temporary directory and removes it after
Vim exits.

## What is collected

Only sources found on the local machine appear in the UI.

| Source | Default location | Files |
|---|---|---|
| Claude Code | `~/.claude/projects/` | `<encoded-cwd>/<uuid>.jsonl` |
| Codex | `~/.codex/sessions/` | `YYYY/MM/DD/rollout-*.jsonl` |
| Cursor | `~/.cursor/projects/` | `<project>/agent-transcripts/**/*.jsonl` and legacy `.txt` files |
| Pi | `~/.pi/agent/sessions/` | project session JSONL files and subagent `session.jsonl` files |

Codex review, compaction, memory-maintenance, and other internal sessions are
excluded. Cursor transcript files do not contain tool output unless Cursor
stored it in a referenced external file.

Claude Code and Cursor sometimes store agent-visible tool output outside the
transcript. The collector includes such a file only when the transcript points
to it (or, for a Claude parent session, when it is in that session's own tool
result directory) and the resolved file stays inside a source-owned directory.
Missing files and files beyond the 100 MiB per-session attachment budget are
omitted from the ZIP and reported in the local preview.

## Bucket layout

Each transcript is stored as one stable ZIP. Uploading a changed transcript
overwrites that object; it does not create a dated copy. Main and subagent
transcripts are peers in the same flat project directory.

```text
s3://rr-agent-transcripts/mts-trans/<contributor>/<project>/<source>--<native-id>.zip
```

The `source` values are listed below. Including the source in the transcript ID
prevents native IDs from different harnesses from colliding. Contributor and
transcript ID segments are made S3-safe; project labels preserve their readable
names with path separators percent-encoded.

Each ZIP contains the redacted native transcript, a manifest, an optional ATIF
trajectory, and any collected attachments:

```text
transcript.zip
├── transcript.jsonl (or transcript.txt)
├── trajectory.atif.json                                      # when available
├── manifest.json
└── tool-results/, task-outputs/, agent-tools/, or terminals/  # when present
```

| Source | `source` | Native file | `source_format` | ATIF |
|---|---|---|---|---|
| Claude Code | `claude_code` | `transcript.jsonl` | `claude-jsonl` | ATIF v1.7 |
| Codex | `codex` | `transcript.jsonl` | `codex-rollout-jsonl` | ATIF v1.7 |
| Cursor | `cursor` | `transcript.jsonl` or `transcript.txt` | `cursor-agent-transcript` | Unsupported |
| Pi | `pi` | `transcript.jsonl` | `pi-session-jsonl-v3` | Unsupported |

The native transcript is the canonical artifact. ATIF conversion is
best-effort: a failed conversion leaves the ATIF absent and does not block the
upload. Parent and subagent ZIPs remain separate peer objects. Their
relationship is stored in the manifest and S3 metadata rather than the object
hierarchy. A child ATIF records its parent transcript ID in
`extra.agent_transcript_collector.parent_transcript_id`.

When an exact child ID is present in a Claude Code or Codex spawn-tool result,
the parent's ATIF observation also contains an external
`subagent_trajectory_ref`. Its `source_call_id` identifies the spawning tool
call and its `trajectory_path` is the child ZIP's relative sibling filename.
Viewers can therefore open that child directly without listing the project or
reading S3 object metadata. The collector does not guess a link when the native
result lacks an exact child ID.

### `manifest.json`

The manifest deliberately contains only information that cannot be learned by
listing the ZIP. Version 7 has this structure:

```json
{
  "manifest_version": 7,
  "id": "claude_code--child-id",
  "format": "claude-jsonl",
  "source": {
    "type": "claude_code",
    "id": "child-id"
  },
  "collection": {
    "type": "project",
    "contributor": "example-contributor",
    "name": "example-project"
  },
  "parent_id": "claude_code--parent-id",
  "redaction": {
    "policy": "agent-transcript-collector/1",
    "count": 3
  },
  "launch_kind": "human"
}
```

`parent_id` is present only for a subagent. The fixed archive names identify the
native transcript and optional ATIF; all other files are attachments. Their
paths, sizes, and ZIP checksums are available from the ZIP directory and are not
repeated in the manifest. Missing or size-limited attachments are not recorded in
the portable archive manifest.

S3 object metadata contains:

| Field | Meaning |
|---|---|
| `mts-manifest-version` | Version of `manifest.json`. |
| `mts-transcript-id` | Stable `<source>--<native-id>` transcript identity. |
| `mts-parent-id` | Parent identity for a subagent; omitted otherwise. |
| `mts-source` | Source adapter ID. |
| `source-hash` | Hash of the original transcript and included attachments. |
| `source-hash-version` | Version of the source-hash algorithm. |
| `redaction-version` | Version of the local redaction policy. |
| `launch-kind` | `human`, `programmatic`, or `unknown` — see below. |

### Storage backend

Uploads go to Redwood's `rr-agent-transcripts` bucket on AWS by default, using
the SSO profile. The same code drives any S3-compatible store — Google Cloud
Storage, Cloudflare R2, MinIO — which is useful for keeping a private copy or
testing without writing to the shared bucket. Point it elsewhere with:

| Variable | Purpose |
| --- | --- |
| `CTC_S3_BUCKET` | Bucket name. |
| `CTC_S3_REGION` | Region. |
| `CTC_S3_ENDPOINT_URL` | Endpoint for a non-AWS store, e.g. `https://storage.googleapis.com`. |
| `CTC_S3_ACCESS_KEY_ID` | HMAC access key, for stores that authenticate that way. |
| `CTC_S3_SECRET_ACCESS_KEY` | HMAC secret. Keep it in a file only you can read, not in a shell profile. |

When both HMAC variables are set they take precedence over the AWS profile.
Non-AWS endpoints reject the SDK's default checksum headers, which the client
accounts for.

### System prompt

An archive records the instructions the session ran under, in
`manifest.json` under `system_prompt` and as the first step of the packaged
ATIF trajectory, so it reads as part of the conversation.

Where a source records the prompt itself, the archive already contains it and
the manifest just identifies it (`status: "in_transcript"`). Otherwise the full
text is stored as `system_prompt.txt` in every archive that has one, so each
archive stands alone.

Storing it once and referring to it by hash was tried and removed: prompts
embed per-session values (a prompt id, the working directory), so identical
copies are rarer than they look, and an archive that points at text living in
some other upload is only useful to a reader who can find that upload.
Deduplicating is better done later across the whole corpus, which is what the
recorded `sha256` is for. What has already been
sent is tracked per contributor in the state directory, and only after an
upload succeeds. Prompts go through the same redaction as transcripts.

**Codex** writes its instructions into the transcript's `session_meta` header,
so nothing extra is needed.

**Claude Code** never writes its system prompt to the transcript; it appears
only on the API request. One command sets that up, once:

```bash
python tools/system_prompt_watcher.py install
```

That adds an `env` block to `~/.claude/settings.json` turning on OpenTelemetry
raw-body logging, and starts a small background service. Every Claude Code
session is then covered — interactive, `-p`, IDE, scripts — with no wrapper to
remember and no change to how you launch anything. Sessions already running
pick it up on their next request, without restarting. Undo it with `uninstall`.

Enabling automatic uploads does this for you, and disabling removes it: the
prompt is part of what an upload describes, so it shares the same consent and
the same lifetime.

The service exists because raw bodies cannot simply be left on disk: each one
contains the whole conversation so far and is written per turn, so a long
session dumps hundreds of megabytes. The watcher reads each body once, files
its system prompt under the session id the body carries in `metadata.user_id`,
and deletes the body immediately, keeping a few kilobytes per session and no
conversation content.

For a single session without enabling any of that, set the same three
variables yourself and collect afterwards:

```bash
CLAUDE_CODE_ENABLE_TELEMETRY=1 OTEL_LOGS_EXPORTER=otlp \
  OTEL_LOG_RAW_API_BODIES=file:/tmp/bodies claude -p "explain this repo"
CTC_SYSTEM_PROMPT_DUMP_DIR=/tmp/bodies python tools/system_prompt_watcher.py drain
```

Sessions run without any of this record `status: "unavailable"`.

### Injected memory

Claude Code injects CLAUDE.md files and the user's auto-memory into the first
user message and, like the system prompt, does not keep them in the saved
transcript. The same watcher takes them from the same request bodies, so this
costs no extra capture and no extra disk.

`manifest.json` records it under `injected_memory`: the hash, the size, and the
files it quotes (`sources`), so a reader can see what was in scope without
opening the text. The text is stored as `injected_memory.txt` and appears as a
step ahead of the conversation.

Note the size: memory runs to roughly the same length as a system prompt (~9 KB
each here), which on a short session can exceed the conversation itself. The
trajectory steps are tagged, so a reader that does not need them can skip them.

Codex records its own `AGENTS.md` injection in the transcript, inside the first
user message, so nothing extra is needed there.

### Launch kind

Each archive records how the run was driven: `human`, `programmatic`, or
`unknown` when neither can be established. Read it as "was this an interactive
agent session", which correlates with human involvement without being the same
question — a developer who types `claude -p "..."` or `codex exec "..."` at
their own shell is a person, but the run is recorded as `programmatic`.

Each source states this its own way, and they answer slightly different
questions.

**Claude Code** stamps provenance on every prompt — `origin.kind`,
`promptSource`, `entrypoint` — so the label reflects where each message came
from: `human` when any prompt was typed into the interactive CLI,
`programmatic` when every prompt arrived over the SDK (`claude -p`, the Agent
SDK). Because it is per prompt, a session that mixes both is detectable.
Only the headless CLI marks its prompts that way, though: the Python and
TypeScript SDKs are recognised by their session `entrypoint` (`sdk-py`,
`sdk-ts`), so for those the label is session-wide rather than per message.

**Codex** states only how the process was launched, once, in its `session_meta`
header: an interactive terminal reports `originator: codex-tui` with
`source: cli`, while `codex exec` reports `originator: codex_exec` with
`source: exec`. The `source` decides, because it is the field Codex defines —
`cli` and `vscode` are interactive, `exec` and `mcp` are not — and the
originator is only consulted for a source that names none of them. It says
nothing about who supplied the prompt text, so a Codex label is coarser than a
Claude Code one — session launch mode, not message provenance.

**Cursor** and **Pi** carry no equivalent marker yet and classify as
`unknown`.

A subagent has no prompts of its own and inherits the label of the run that
spawned it.

Neither direction is proof. A script that drives an interactive session by
sending keystrokes is indistinguishable from a person at the keyboard, so
`human` is evidence rather than certainty — and more easily so for Codex, where
every TUI session counts as human regardless of who typed. In the other
direction, `programmatic` does not mean no person was involved; it means the
run was not an interactive session. For analysis, treat the label as a
description of how the agent was invoked rather than a claim about who was
present.

Where a source records provenance per prompt, a session mixing both — an
interactive run later resumed with `claude -p` — reads as `human`, because a
person did prompt it at some point.

The label lands in `manifest.json`, in the packaged ATIF trajectory (with
per-message origins where the source records them, which today means Claude
Code), and in the `launch-kind` S3 object metadata so it can be read
without downloading the archive. Archives uploaded before this existed carry no
label.

The source hash covers the unredacted transcript bundle and its discovered
direct-child IDs, and is used to detect content or link changes. Anyone who can
read the metadata could use it to confirm a guess about exact source content.

## Redaction and privacy

Redaction runs locally after an upload starts and before ATIF conversion or ZIP
creation. The ATIF converter receives the redacted native transcript. The
collector replaces high-confidence credential formats, including common cloud
and API keys, JWTs, private keys, credential-bearing connection URIs, and
explicit password or token assignments. It also replaces local usernames in
paths, bare occurrences of the local username, and email addresses.

Credential replacements are type-shaped mock values rather than a generic
`[REDACTED]`. Equal secrets map to the same mock within one process, which keeps
references traceable inside an upload. The random mapping is discarded when the
process exits. Mock values contain `4d4f434b` (hex for `MOCK`).

Redaction favors precision and can miss unusual or unformatted secrets. Preview
and select transcripts deliberately; redaction is not a guarantee.

Before an upload, status refreshes may read and hash original local content and
query S3 metadata. They do not create an archive or send transcript content.
Temporary archives use user-only permissions and are deleted when the upload
job finishes.

## Configuration

Profile selection uses the first value set in this order:
`CTC_AWS_PROFILE`, `AWS_PROFILE`, `AWS_DEFAULT_PROFILE`, then `rw-eng`.

| Variable | Default | Purpose |
|---|---|---|
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude Code data directory |
| `CODEX_HOME` | `~/.codex` | Codex data directory |
| `CURSOR_HOME` | `~/.cursor` | Cursor data directory |
| `CURSOR_USER_DATA_DIR` | platform default | Cursor state database used to recover project paths |
| `PI_CODING_AGENT_SESSION_DIR` | `~/.pi/agent/sessions` | Pi session directory |
| `PI_CODING_AGENT_DIR` | `~/.pi/agent` | Pi data directory; lower priority than the session override |
| `CTC_USERNAME_STOPLIST` | unset | Comma-separated usernames that identity redaction should leave unchanged |
| `CTC_HASH_CONCURRENCY` | `16` | Concurrent source hashing |
| `CTC_ARCHIVE_CONCURRENCY` | `8` | Concurrent redaction and archive jobs |
| `CTC_UPLOAD_CONCURRENCY` | `8` | Concurrent S3 uploads |
| `CTC_METADATA_CONCURRENCY` | `20` | Concurrent S3 metadata requests |
| `CTC_ATTACHMENT_MAX_BYTES` | `104857600` | Attachment budget for a foreground `rr-trans ui` process |
| `PORT` | `8899` | Starting port for `rr-trans ui` |

Source paths, profile selection, concurrency settings, and the username stoplist
are captured when background services are installed or enabled. Reinstall the UI
service or watcher after changing them. The background UI always listens on
`127.0.0.1:8123`; `PORT` only affects the foreground command. The installed
services currently use the default attachment budget.

### Local files

| Data | macOS | Linux |
|---|---|---|
| Config | `~/Library/Application Support/agent-transcript-collector/` | `${XDG_CONFIG_HOME:-~/.config}/agent-transcript-collector/` |
| State/cache | same as config | `${XDG_STATE_HOME:-~/.local/state}/agent-transcript-collector/` |
| Watcher log | `~/Library/Logs/agent-transcript-collector/watcher.log` | state directory, `watcher.log` |
| UI log | `~/Library/Logs/agent-transcript-collector/ui.log` | state directory, `ui.log` |

The local `pipeline-cache.json` stores file metadata, source hashes, S3 keys, and
upload status to avoid unnecessary reads and S3 requests. It does not contain
transcript text. The cache is disposable and written with user-only permissions.
