# Hermes group project binding

## GitHub installation

Publish this package in a private GitHub repository, then install the exact
release commit in each Hermes profile that should load it:

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

`rooms` is optional. Omit it to let the plugin discover rooms from the owner Gateway. An unlisted or unconfirmed room continues with native Hermes routing until it receives a project initialization command.

Runtime diagnostics are written to `runtime-trace.jsonl` and `runtime-errors.jsonl`. Each file is capped at 256 KiB with three rotated backups, so recent routing and failure evidence remains available without unbounded log or cache growth.
