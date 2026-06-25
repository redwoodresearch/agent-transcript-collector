# Follow-ups

(GitHub Issues are disabled on this repo, so tracked here.)

## Pi (`pi-subagents`) subagent collection — open

Claude Code and Codex task subagents are collected and marked; **Pi subagents
are not yet collected.**

Verified format (from `nicobailon/pi-subagents` source + earendil-works/pi
session format):
- The subagent transcript is **standard Pi session JSONL** (`{"type":"session",
  "version":3, …}` header, then `{"type":"message", …}` lines) — the existing Pi
  parser can read it as-is.
- Default location is **parent-derived**:
  `~/.pi/agent/sessions/<parentBasename>/<runId>/run-N/session.jsonl`
  (not `sessions/subagent/`). Configurable via `params.sessionDir` /
  `config.defaultSessionDir`.
- `context: "fork"` produces a branched session file carrying a `parentSession`
  header (may land in the normal sessions dir).
- **Do not parse** `events.jsonl` or `subagent-artifacts/*.jsonl` in the run
  dirs — different schemas.
- No monitor/guardian concept in Pi (`reviewer` is a normal task subagent).
- Parent recoverable: `<parentBasename>` (two levels up) or the fork
  `parentSession` header.

Plan: in the Pi adapter, also glob `<session-dir>/*/*/run-*/session.jsonl`
(honoring the configurable session dir), confirm the `{"type":"session"}`
header, mark `is_subagent=True`, set `parent=<parentBasename>`; mark forked
sessions via their `parentSession` header; skip `events.jsonl` /
`subagent-artifacts/`. Add synthetic-fixture tests. Confirm against one real
`pi-subagents` run before finalizing.

## Durable secret scanning — open

The per-provider credential patterns (Neon `npg_`, RunPod, DB connection URIs)
are a stopgap. Durable direction:
- Replace hand-rolled provider/entropy patterns with a maintained scanner
  (e.g. `detect-secrets`, pure-Python) + a structural URI/DSN pass + recall-tuned
  entropy, benchmarked against the chippy corpus.
- **GitHub handle redaction** — not covered by `redact_identity` (needs an
  account lookup, not a pure regex); deferred.
- **Third-party names in free text** — only the local machine's own identity is
  redacted as a bare token; documented limitation.

## Done

- Credential redaction decoupled from the PII toggle: Neon/RunPod/DB-URI
  patterns added to the secret pass, and secret/credential redaction made
  always-on (not toggleable) in UI, preview, upload, and headless.
- Codex subagent classification aligned to the real `SessionSource` schema
  (drop review/compact/memory_consolidation/internal; keep+mark thread_spawn
  with `parent_thread_id`, and the `other` catch-all).
