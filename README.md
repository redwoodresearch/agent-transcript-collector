# agent-transcript-collector

Collect AI coding-agent transcripts with consent, redact secrets locally, and
upload them to S3.

Supported sources: **Claude Code**, **Codex**, **Cursor**, and **Pi**. Uploads go
to `s3://rr-agent-transcripts` in `us-east-1`.

- [Prerequisites](#prerequisites)
- [Install](#install)
- [Review and upload transcripts](#review-and-upload-transcripts)
- [Automatic uploads](#automatic-uploads)
- [Browse uploaded transcripts](#browse-uploaded-transcripts)
- [Transcript data and storage](#transcript-data-and-storage)
- [Privacy and redaction](#privacy-and-redaction)
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

## Install

Run the default setup command:

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  rr-trans
```

This installs `rr-trans` on your `PATH`, starts the review UI in the background,
configures it to start whenever you log in, and opens it in your browser. The UI
is then always available at <http://localhost:8123>. Run the same command again
to fetch the latest version from the repository, restart the UI with it, and open
it in your browser. The background process also checks for an updated version
whenever it starts.

The service is installed for your user account with a macOS LaunchAgent or Linux
systemd user service.

After setup, the user-facing command is `rr-trans`:

| Command | Purpose |
|---|---|
| `rr-trans` or `rr-trans install` | Install/update the CLI and background UI |
| `rr-trans ui` | Run a temporary foreground UI session |
| `rr-trans ui-service status` | Show the background UI status and log path |
| `rr-trans ui-service uninstall` | Stop and remove the background UI service |
| `rr-trans tui` | Explore uploaded transcript folders in S3 |
| `rr-trans watcher` | Watcher status and removal |

## Review and upload transcripts

After setup, open <http://localhost:8123>. Preview the transcripts, select the
projects you want to share, enter your name, and click **Upload X transcripts
now**.

For a one-off foreground session instead:

```bash
rr-trans ui
```

The foreground command serves at <http://localhost:8899> and opens it in your
browser (if that port is busy, the next free port is used).

## Automatic uploads

The review UI can install a per-user watcher on macOS or Linux:

1. Enter the contributor name that should own the uploads.
2. Check **auto upload** beside each project you consent to share.
3. Click **Enable**.

The watcher uploads every existing transcript associated with those projects
across the supported agent harnesses, then checks once an hour for new transcripts
or changed content. Each session has one S3 object: when its redacted content
changes, that object is overwritten; unchanged content is skipped.

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
rr-trans watcher status
rr-trans watcher uninstall
```

`uninstall` stops future runs but preserves your folder choices, last-run state,
and logs; add `--purge` to delete them too.

Your folder choices are stored with user-only permissions:

| | macOS | Linux |
|---|---|---|
| Config | `~/Library/Application Support/agent-transcript-collector/` | `${XDG_CONFIG_HOME:-~/.config}/agent-transcript-collector/` |
| Logs | `~/Library/Logs/agent-transcript-collector/watcher.log` | `${XDG_STATE_HOME:-~/.local/state}/agent-transcript-collector/watcher.log` |

## Browse uploaded transcripts

Open the read-only terminal browser:

```bash
rr-trans tui
```

Use Enter to expand folders and `q` to quit. It opens at `mts-trans/` by default;
pass `--prefix mts-trans/<contributor>/` to start at a narrower S3 prefix. The
browser only lists object names and sizes. It does not download or extract data.

## Transcript data and storage

The collector discovers native transcript files on your machine. Only sources
that are present appear in the review UI.

| Source | Search path | Transcript layout |
|---|---|---|
| Claude Code | `~/.claude/projects/` (`CLAUDE_CONFIG_DIR`) | `<encoded-cwd>/<uuid>.jsonl` |
| Codex | `~/.codex/sessions/` (`CODEX_HOME`) | `YYYY/MM/DD/rollout-*.jsonl` |
| Cursor | `~/.cursor/projects/` (`CURSOR_HOME`) | `<encoded-project>/agent-transcripts/<id>/<id>.jsonl` |
| Pi | `~/.pi/agent/sessions/` (`PI_CODING_AGENT_SESSION_DIR` or `PI_CODING_AGENT_DIR`) | `--<encoded-cwd>--/<ts>_<id>.jsonl` |

Each session goes through the same pipeline:

1. **Discover:** find native transcripts and group them by project and source.
2. **Assemble:** keep each transcript in its native JSONL or text format, link
   subagent sessions to their parents, and resolve referenced external files.
3. **Redact:** replace detected credentials and local identity data on the local
   machine, in both the transcript and its external files.
4. **Store:** package one redacted session per ZIP and upload it to a stable S3
   key. A changed session replaces its previous ZIP; unchanged content is skipped.

The uploaded transcript contains the source's native data without schema
conversion.

### What is not collected

- Cursor's native transcripts omit tool output. The collector can include only
  output that Cursor saved to a referenced external file.
- Codex's internal review, compaction, and memory-maintenance sessions are
  ignored; user sessions and task subagents are collected.
- An external file is omitted if it has disappeared or exceeds the size budget.
  Its redacted reference remains in the manifest under `missing` or
  `skipped_too_large`.

### Subagents and sidecars

A subagent transcript is a separate session, not a file inside its parent's ZIP.
Its manifest sets `session.is_subagent` to `true` and records the parent session
ID. Its ZIP is stored below the parent under `subagents/`.

A **sidecar** is agent-visible content that a transcript points to instead of
embedding, such as an oversized tool result or background-task output. The
collector follows only explicit pointers that stay within folders owned by the
source. It does not treat a referenced subagent transcript as a sidecar.

Sidecars are redacted and stored in the referring session's ZIP. Their manifest
entries record the archive path, original reference after identity redaction,
size, and content hash.

### Folder structure

Each session has one stable S3 object. Subagents are nested under their parent:

```text
s3://rr-agent-transcripts/mts-trans/<contributor>/<project>/<source>/<session>/transcript.zip
s3://rr-agent-transcripts/mts-trans/<contributor>/<project>/<source>/<parent>/subagents/<session>/transcript.zip
```

Each ZIP contains the redacted native transcript, its manifest, and any sidecars
referenced by that transcript:

```text
transcript.zip
├── transcript.<ext>             # .jsonl or .txt
├── manifest.json
└── <sidecar-kind>/              # only when sidecars are present
    └── <name>
```

The transcript and manifest are always present. Sidecar folders appear only
when the session includes files of that kind. Current sidecar kinds are
`tool-results`, `task-outputs`, `agent-tools`, and `terminals`.

### `manifest.json`

The manifest identifies the source, project, session, stored content, and
redaction result:

```json
{
  "transcript_format_version": 4,
  "source": "claude_code",
  "source_format": "claude-jsonl",
  "contributor": "example-contributor",
  "project": {
    "key": "example-project",
    "name": "example-project"
  },
  "session": {
    "id": "session-id",
    "is_subagent": false,
    "parent": null
  },
  "version": {
    "fingerprint": "privacy-safe archive fingerprint",
    "body_fingerprint": "privacy-safe transcript fingerprint",
    "content_sha256": "SHA-256 of the redacted transcript",
    "redact_identity": true,
    "uploaded_at": "2026-01-01T00:00:00+00:00"
  },
  "size_bytes": 12345,
  "redactions": 3,
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

`version` holds the fingerprints used for change detection, the redacted
transcript hash, and the upload time. `size_bytes` is the redacted transcript
size, and `redactions` is the total number of replacements. Each sidecar entry
records its ZIP path, type, redacted original reference, redacted size, and hash.
The three sidecar arrays are present even when empty.

## Privacy and redaction

Redaction happens on your machine before anything is written to a ZIP, and the
count of replacements is recorded in its manifest.

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

## Configuration

Normally the only thing you set is the SSO profile from the
[Redwood devbox guide](https://github.com/redwoodresearch/research-devboxes/blob/main/docs/getting-started.md).
These knobs exist for overriding defaults:

| Env var | Default | Purpose |
|---|---|---|
| `AWS_PROFILE` | _(unset)_ | Standard AWS profile selector; set it to your General Sandbox profile. |
| `CTC_AWS_PROFILE` | _(unset)_ | Collector-specific profile override. |
| `CTC_REDACTION_CONCURRENCY` | `16` | Changed transcripts redacted and packaged in parallel. |
| `CTC_UPLOAD_CONCURRENCY` | `4` | Transcripts uploaded in parallel. |
| `CTC_METADATA_CONCURRENCY` | `16` | S3 fingerprint checks performed in parallel. |
| `CTC_SIDECAR_MAX_BYTES` | `104857600` | Side-file bytes collected per session. |
| `CTC_USERNAME_STOPLIST` | _(unset)_ | Comma-separated logins to never redact. |
| `PORT` | `8899` | Local review UI port. |

Profile resolution prefers `CTC_AWS_PROFILE`, then `AWS_PROFILE`, then
`AWS_DEFAULT_PROFILE`. For compatibility with existing setups it falls back to
the local profile name `rw-eng`. The bucket and region are fixed to
`rr-agent-transcripts` in `us-east-1`.
