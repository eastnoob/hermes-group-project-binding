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
    "coreServiceToken": "<external-only>"
  }
}
```

Enable group notices only after the Hermes `bot_room` post/edit capability probe
passes. If the Core source or editable message capability is unavailable, the
WebApp panel remains the source of truth.
