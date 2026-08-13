# agent-transcript-collector

Review, redact, and upload transcripts from Claude Code, Codex, Cursor, and Pi.
Uploads go to `s3://rr-agent-transcripts/mts-trans/` in `us-east-1`.

## Bucket layout

Each session is stored as one stable ZIP. Uploading a changed session overwrites
that object; it does not create a dated copy.

```text
s3://rr-agent-transcripts/mts-trans/<contributor>/<project>/<source>/<session>/transcript.zip
s3://rr-agent-transcripts/mts-trans/<contributor>/<project>/<source>/<parent>/subagents/<session>/transcript.zip
```

The `source` values are listed below. Contributor and session segments are made
S3-safe; project labels preserve their readable names with path separators
percent-encoded.

Each ZIP contains the redacted native transcript, a manifest, an optional ATIF
trajectory, and any collected sidecars:

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
best-effort: a failed conversion is recorded in the manifest and does not block
the upload. Parent and subagent ZIPs remain separate. A child ATIF records its
parent transcript ID in
`extra.agent_transcript_collector.parent_transcript_id`.

### `manifest.json`

Format version 6 has this structure:

```json
{
  "transcript_format_version": 6,
  "source": "claude_code",
  "source_format": "claude-jsonl",
  "contributor": "example-contributor",
  "project": {
    "key": "project-a1b2c3d4e5f6",
    "name": "example-project"
  },
  "session": {
    "id": "session-id",
    "is_subagent": false,
    "parent": null
  },
  "version": {
    "source_hash": "SHA-256 of the original transcript bundle",
    "source_hash_version": 3,
    "redaction_version": 1,
    "content_sha256": "SHA-256 of the redacted native transcript",
    "uploaded_at": "2026-01-01T00:00:00+00:00"
  },
  "size_bytes": 12345,
  "redactions": 3,
  "atif": {
    "status": "complete",
    "schema_version": "ATIF-v1.7",
    "converter": {
      "name": "harbor",
      "version": "0.20.0"
    },
    "artifact": "trajectory.atif.json"
  },
  "sidecars": {
    "files": [
      {
        "path": "tool-results/result.txt",
        "kind": "tool-results",
        "referenced_as": "/redacted/path/to/result.txt",
        "size_bytes": 456,
        "sha256": "SHA-256 of the redacted sidecar"
      }
    ],
    "missing": ["/redacted/path/to/missing.output"],
    "skipped_too_large": ["/redacted/path/to/oversized.txt"]
  }
}
```

`atif.status` is `complete`, `unsupported`, or `failed`. Only `complete` has an
`artifact`; `failed` also has an `error`. The three sidecar arrays are always
present. `size_bytes` is the redacted native transcript size, not the ZIP size.

S3 object metadata contains `source-hash`, `source-hash-version`,
`redaction-version`, and `transcript-format-version`. The source hash covers the
unredacted transcript bundle and is used to detect changes. Anyone who can read
the metadata could use it to confirm a guess about exact source content.

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
review UI at <http://localhost:8123>. Run the command again to update and restart
the service. The service also checks the `main` branch for updates when it
starts.

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

The watcher runs immediately and then once an hour. It uploads new sessions and
replaces an existing session ZIP when the local transcript or its sidecars have
changed. Unchanged sessions are skipped.

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
| `rr-trans` or `rr-trans install` | Install or update the CLI and background UI |
| `rr-trans ui` | Run the review UI in the foreground |
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
Missing files and files beyond the 100 MiB per-session sidecar budget are listed
in the manifest but not uploaded.

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
| `CTC_SIDECAR_MAX_BYTES` | `104857600` | Sidecar budget for a foreground `rr-trans ui` process |
| `PORT` | `8899` | Starting port for `rr-trans ui` |

Source paths, profile selection, concurrency settings, and the username stoplist
are captured when background services are installed or enabled. Reinstall the UI
service or watcher after changing them. The background UI always listens on
`127.0.0.1:8123`; `PORT` only affects the foreground command. The installed
services currently use the default sidecar budget.

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
