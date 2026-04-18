# /agents

First-party agent templates shipped with PILK. Each subdirectory is a
self-contained agent bundle (`manifest.yaml` + docs + optional code). The
registry discovers every subdirectory on startup and upserts it into the
`agents` table.

| Folder | Purpose |
|---|---|
| `_template/` | Scaffold — copy to start a new agent. Underscore-prefixed folders are ignored by the registry. |
| `file_organization_agent/` | Reference agent — organizes files inside its sandbox workspace. |

To add a new agent: `cp -R _template my_new_agent`, edit
`my_new_agent/manifest.yaml` (set `name`, `profile`, `system_prompt`,
`tools`), restart pilkd.
