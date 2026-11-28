# Verbose memory notice capability probe

Branch: `memory-verbose-notice`

The probe used read-only routes against the running Tencent Memory Core. It did
not write memory, call a model, or change production configuration.

## Observed contract

- `GET /v3/memory-generation-log/list` returns successful L1 log summaries.
- `GET /v3/memory-generation-log/get` returns Team, Agent, User, Task, Session,
  input refs, output refs, prompt metadata, timestamps, and status.
- The generation log does not contain an operation field, a display summary, or
  memory content.
- A scoped `POST /v3/atomic/query` can return the L1 records whose IDs match
  generation `output_refs`. The returned records include `content`, type,
  background, version, and tenancy fields.
- The returned record metadata did not expose `operation`, `action`, or
  `dedup_action`. Version can indicate that a record has history, but it does
  not distinguish update from merge and is not a safe user-facing operation
  label.

## Decision

The current Core supports a bounded, read-only content lookup but does not
provide the redacted summary contract required for Hermes-like verbose notices.
The deployed version remains concise (`L1 已写入：角色 N 条`). The debug branch
adds an explicit opt-in verbose mode that performs the scoped lookup only when
selected, then applies deterministic plugin-side redaction and truncation. It
never exposes raw content or infers add/update/merge.

## Architecture boundary

The verbose branch must not modify Hermes or Tencent Core. It may use only the
official Hermes plugin/SDK surfaces and the existing read-only Core routes. All
preview selection, deterministic secret filtering, truncation, UI state, and
notice rendering belong to this plugin. It must not add a second model call,
write or update Tencent memory, or patch the host at runtime.

Because the native read contract does not identify `store` versus `update` or
`merge`, the plugin-only mode may say `L1 新记录`/`L1 已写入` and show a redacted
preview, but it must not claim a more specific operation.

## Next implementation gate

Verbose mode can proceed only after either:

1. Core exposes a scoped `operation` plus `display_summary`/redacted preview;
   or
2. an explicitly opt-in plugin path is approved to fetch scoped content and
   apply deterministic secret filtering, truncation, and no-content fallback.

The second path must still avoid writing raw content to the status file or
feeding any preview back into the model context.

The branch implementation stores only the bounded, redacted preview required
to deliver a WebApp toast; concise mode does not query or persist previews.
It remains unpushed and undeployed pending branch validation.
