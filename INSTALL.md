# Install

Install the immutable GitHub commit with Hermes's supported plugin installer:

```text
hermes plugins install eastnoob/hermes-group-project-binding --ref <40-character-commit-sha> --enable
```

Install the same commit into every profile that participates in the Canonical
group. Keep the previous commit as the rollback target. Do not copy profile
configuration, room bindings, runtime logs, databases, or credentials into the
plugin package.

The optional memory-status source is configured outside this repository through
the external project options file or environment:

```json
{
  "memoryStatus": {
    "enabled": false,
    "groupNoticeEnabled": false,
    "coreEndpoint": "http://memory-core:8420",
    "coreServiceToken": "<external-only>",
    "noticeMode": "concise"
  }
}
```

Enable group notices only after the Hermes `bot_room` post capability probe
passes. Notices are write-triggered: no-change, processing, unknown, and failed
turns stay silent in chat while remaining visible in the WebApp panel. The
WebApp's memory status selector can opt the shared room into `verbose`; this
performs plugin-side deterministic redaction and truncation through the existing
scoped read-only Core query. It does not modify Hermes or Core, call another
model, or write memory. If the Core source or post capability is unavailable,
the WebApp panel remains the source of truth.
