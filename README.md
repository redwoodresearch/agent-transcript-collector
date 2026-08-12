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

## Install

Run the default setup command:

```bash
uvx --from 'git+https://github.com/redwoodresearch/agent-transcript-collector' \
  rr-trans
```

This installs `rr-trans` on your `PATH`, starts the review UI in the background,
and configures it to start whenever you log in. The UI is then always available
at <http://localhost:8123>. Run the same command again to fetch the latest version
from the repository and restart the UI with it. The background process also
checks for an updated version whenever it starts.

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
2. Check **auto upload** beside each folder you consent to share.
3. Click **Enable**.

The watcher uploads every existing transcript in those exact folders, then checks
once an hour for new transcripts or changed content. Each session has one S3
object: when its redacted content changes, that object is overwritten; unchanged
content is skipped. Selecting a folder does not implicitly select similarly named
or descendant folders.

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

### Tool output stored outside the transcript

Harnesses move oversized tool output into separate files and leave only a
pointer in the transcript, so a transcript on its own records that the agent
saw something without recording what. Those files are collected alongside the
transcript that points at them:

| Source | Folder | Holds |
|---|---|---|
| Claude Code | `<project>/<session>/tool-results/` | Tool results too large to inline |
| Claude Code | `<tmp>/claude-<uid>/<project>/<session>/tasks/` | Background command output |
| Cursor | `<project>/agent-tools/` | Oversized tool results the agent reads back |
| Cursor | `<project>/terminals/` | Shell output the agent reads back |

Only files a transcript actually names are collected, resolved from the
pointer rather than by listing a folder, because a resumed session keeps
pointing at the folder it inherited from the session before it. A pointer is
followed only when it stays inside the folder its harness owns, so a symlink
leading elsewhere is ignored. Claude Code additionally points a finished task
at the subagent transcript, which is collected as a session in its own right.

Harnesses delete these files on their own schedule, so some pointers are
already dead by the time a transcript is uploaded. Those are listed in the
manifest instead, and an upload that has lost side files never overwrites an
earlier upload that still had them.

Codex and Pi keep tool output inline, so they have no such files.

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

## Storage layout

Each transcript is stored in one ZIP at a stable key:

```text
s3://rr-agent-transcripts/mts-trans/<contributor>/<project-name>/<source>/<session>/transcript.zip
s3://rr-agent-transcripts/mts-trans/<contributor>/<project-name>/<source>/<parent>/subagents/<session>/transcript.zip
```

Transcripts with the same project name share one project directory. Each ZIP
contains one redacted transcript and a `manifest.json` with project, source,
contributor, session, content-fingerprint, and redaction metadata. Resuming a
session overwrites the same object only when the privacy-safe fingerprint
changes.

The ZIP has this folder structure. `transcript.<ext>` and `manifest.json` are
always present; the side-file folders are included only when the source
transcript refers to files of that kind:

```text
transcript.zip
├── transcript.<ext>             # .jsonl or .txt, matching the source format
├── manifest.json
├── tool-results/                # Claude Code oversized tool results
│   └── <name>.txt
├── task-outputs/                # Claude Code background-task output
│   └── <name>.output
├── agent-tools/                 # Cursor oversized tool results
│   └── <name>.txt
└── terminals/                   # Cursor terminal output
    └── <name>.txt
```

### `manifest.json`

Each archive's manifest has this structure:

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
    "missing": [
      "/redacted/path/to/missing.output"
    ],
    "skipped_too_large": [
      "/redacted/path/to/oversized.txt"
    ]
  }
}
```

`project` identifies the local project grouping, while `session` identifies the
transcript and its parent relationship. A subagent has `is_subagent` set to
`true` and its parent session ID in `parent`. `version` records the fingerprints
used to detect changes, the hash of the redacted transcript, the identity
redaction policy, and the archive timestamp. `size_bytes` is the redacted
transcript size, and `redactions` counts replacements across the transcript,
sidecars, and project identity fields.

Every included sidecar is listed under `sidecars.files`, with its ZIP path,
kind, redacted path as referenced by the transcript, redacted size, and hash.
Pointers whose target was already gone are listed under `sidecars.missing`;
files rejected by the size budget are listed under `sidecars.skipped_too_large`.
All three fields are present even when their arrays are empty. A session with no
side files keeps the fingerprint it always had, so this does not rewrite earlier
uploads.

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
