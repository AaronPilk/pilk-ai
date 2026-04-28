# file_organization_agent

A low-risk reference agent shipped with PILK. It surveys the files in its
sandbox workspace and proposes (or executes) an organization plan.

## What it does

1. Inventories the sandbox: `ls -la`, then a shallow `find`.
2. If the tree is messy, proposes a folder structure and stops for your
   review.
3. If you confirm, executes the moves with `mkdir -p` + `mv` and verifies
   with `ls`.

## Where it works

Only inside its sandbox workspace at
`~/PILK/sandboxes/sb_process_file_organization_agent_file_organization_agent/workspace/`.
The tool gateway hard-rejects any path outside that root.

## How to use

From the dashboard: open the **Agents** tab, pick this agent, and type a
task in the run box. For example:

- `Scan the workspace and propose a folder layout.`
- `Move everything into folders: docs/, code/, data/.`

Drop any files you want organized into the sandbox workspace first (you
can see the exact path in the **Sandboxes** tab).
