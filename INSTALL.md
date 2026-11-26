# Install the pinned release

Use Hermes' supported plugin installer from each target profile:

```text
hermes plugins install OWNER/REPO --ref <40-character-commit-sha> --enable
```

Install the same full commit SHA for `personalassistant-hub` and `slave`.
Do not install a branch tip in a running profile. For an update, publish a new
commit and explicitly reinstall it with `--force --ref <new-sha>`. Keep the
previous installed revision as a rollback target until discovery, the WebApp
entry, and read-only room/session checks pass.

The repository intentionally contains no profile configuration, Tencent
credentials, room bindings, memory data, runtime logs, caches, or databases.
