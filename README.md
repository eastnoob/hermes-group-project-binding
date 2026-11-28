# Hermes group project binding

## GitHub installation

Publish this package in a GitHub repository, public or private according to
your distribution choice, then install the exact release commit in each Hermes
profile that should load it. The package contains no credentials or profile
data.

```text
hermes plugins install OWNER/REPO --ref <40-character-commit-sha> --enable
```

Use the same immutable commit for `personalassistant-hub` and `slave`. A later
release is an explicit replacement with a new commit SHA; do not run a branch
tip in a profile. Keep the previous installed revision as the rollback target
until the WebApp panel and backend discovery checks pass.

Profile configuration, Tencent credentials, room bindings, memory data, and
runtime logs remain outside the repository.

This plugin dynamically binds a Canonical room to a Tencent Team and Task. The primary interface is the Hermes WebApp. Its supported desktop-plugin half opens the project-binding form, reads the authenticated room roster through Gateway RPC, asks the plugin command bridge for live Core metadata, and submits a versioned proposal. The native room approval remains the final owner confirmation. Ordinary group turns receive Tencent's four official routing headers.

The WebApp entry is `desktop/plugin.js`, loaded from the host's supported `desktop-plugins/<id>/plugin.js` surface. Native Desktop can load the same package, but it is optional. Project mode stays inactive when the authenticated WebApp loader surface is unavailable.

The dynamic flow does not require a `project-options.json` room or member map. The legacy file remains accepted for compatibility with existing confirmed rooms and the text fallback.

```json
{
  "core": "http://127.0.0.1:8096",
    "memoryHub": "http://memory-hub:8125",
  "spaceId": "default",
  "userKey": "<owner-user-key>",
  "userId": "<owner-user-id>",
  "ownerHome": "<owner-hermes-home>",
  "profile": "default",
  "rooms": ["<room-id>"],
  "projects": {
    "project-a": {
      "space_id": "default",
      "team_id": "<team-id>",
      "task_id": "<task-id>",
      "shared_memory_id": "mem-project-a",
      "roles": {
        "<member-id>": {
          "agent_id": "<agent-id>",
          "conversation_id": "{room_id}-{project_id}-{member_id}"
        }
      }
    }
  }
}
```

`memoryHub` is optional. When it is configured, the WebApp lists the Team's
shared Chat Memory assets. The owner can choose one asset or keep the
private-only option. A confirmed shared asset is allocated to each selected
Agent through the official Memory Hub `chat-memory` API. The plugin does not
write L1 records or perform memory fan-out itself.

In a room, send `@slave 初始化 project-a` and approve the native confirmation once. Later ordinary messages use that room's saved project. To switch, send `@slave 切换项目 project-b` and approve the new confirmation once.

The conversation ID template includes the project ID because Tencent MemoryProxy caches session initialization by conversation ID. This keeps a project switch from reusing the previous project's session scope.

Private sessions bypass this room mapping and keep their own Profile configuration.

In a confirmed group, read the current binding without invoking a model by
sending `@personalassistant-hub 当前绑定` (or addressing the other room
member). Use `@personalassistant-hub 绑定校验` to recheck the saved room scope
against the current roster and Core metadata. These replies never expose
credentials or routing headers.

When using the WebApp form, choose the shared Chat Memory before or after the
Task; changing the Task keeps the selected asset in the proposal.

`rooms` is optional. Omit it to let the plugin discover rooms from the owner Gateway. An unlisted or unconfirmed room continues with native Hermes routing until it receives a project initialization command.

Runtime diagnostics are written to `runtime-trace.jsonl` and `runtime-errors.jsonl`. Each file is capped at 256 KiB with three rotated backups, so recent routing and failure evidence remains available without unbounded log or cache growth.

## Memory status panel and group notice

The plugin can keep a bounded, metadata-only status history for asynchronous
Tencent L1 extraction. The optional external `project-options.json` setting is
disabled by default until the current Hermes `bot_room` host passes the real
capability probe:

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

When enabled after that probe, the WebApp panel shows `queued`, `processing`,
`updated`, `no_change`, `failed`, and `unknown_timeout` states. A group status
message is used only when the host proves that one message can be posted and
edited; otherwise the panel remains the source of truth. The live source reads
Core's read-only memory-generation log and pipeline status routes, then matches
the frozen Team/Agent/User/Task/Session scope and a pre-turn L1 baseline. If
that source or its external token is unavailable, status remains panel-only.
The plugin never uses `atomic/write`, never sends a second model turn for a
notice, and does not store prompt or memory content in the status file. The
Core token must be supplied outside GitHub (for example through the external
project options file or `MEMORY_STATUS_CORE_SERVICE_TOKEN`).

Group notices follow Hermes' native write-notification behavior: they stay silent
while extraction is running and when a turn adds nothing. After a scoped L1
write is confirmed, one concise notice is shown, for example
`🧠 L1 已写入：hub 2 条`. Unknown, failed, and no-change results remain in the
WebApp status panel and are never rendered as an L1 success. The notice contains
the role and count only; memory content is not copied into the chat.
