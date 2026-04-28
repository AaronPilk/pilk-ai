# Plan — file_organization_agent

## Objective
Keep a directory tidy without the user having to specify move-by-move ops.

## Inputs
A short natural-language task ("scan", "organize into folders X/Y/Z", …).

## Outputs
- A text summary of what it did.
- Re-arranged files inside the sandbox workspace.

## Tools
- `shell_exec` — `ls`, `find`, `mkdir -p`, `mv`.
- `fs_read` — peek at a file's contents when disambiguating category.
- `fs_write` — rare; used only if the agent wants to leave a `README.md`
  in a new folder.

## Safety
- Hard-scoped to sandbox workspace via tool gateway.
- Never deletes.
- Asks before moving if the tree is non-trivial.

## Open questions
- Should the agent auto-detect "media" folders and apply conventional
  structure (by-year/by-type)? Revisit after user feedback.
