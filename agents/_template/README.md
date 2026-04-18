# _template

Scaffold for a new PILK agent. Copy this folder to
`agents/{your_agent_name}/` and fill in:

- `manifest.yaml` — identity, tools, sandbox, budget
- `PLAN.md` — what this agent does and how it does it
- `README.md` — user-facing docs

Then restart pilkd; the registry will pick the manifest up on startup.

## Layout

```
manifest.yaml     required
README.md         user-facing docs
PLAN.md           approach / expected behavior
src/              python entry point (reserved — optional in batch 2)
config/           per-agent configuration (optional)
data/             persistent data the agent owns (optional)
logs/             structured logs (optional)
```

An agent's runtime state (sandbox workspace, session profile) lives under
`~/PILK/sandboxes/sb_{type}_{agent_name}_{profile}/` — not here.
