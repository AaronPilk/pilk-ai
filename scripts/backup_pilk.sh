#!/usr/bin/env bash
# PILK backup script — copies the SQLite DB, brain vault, and state
# files into a timestamped folder under ~/PILK/backups/.
#
# Run before any schema migration, agent rewrite, or other change that
# could damage memory, brain content, or operating state. Safe to run
# any time; backups are additive and never overwrite an existing
# timestamped folder.
#
# Usage:
#     bash scripts/backup_pilk.sh
#
# The script prints the backup path on stdout when it finishes so the
# caller can record it or roll back to it.

set -euo pipefail

PILK_HOME="${PILK_HOME:-$HOME/PILK}"
BRAIN_DIR="${PILK_BRAIN_VAULT_PATH:-$HOME/PILK-brain}"
TS="$(date +%Y-%m-%d_%H-%M-%S)"
DEST="$PILK_HOME/backups/$TS"

mkdir -p "$DEST"

echo "[backup] target: $DEST" >&2

# 1. SQLite database (and WAL/shm sidecars). Use sqlite3 .backup so
#    a write in flight doesn't tear the copy.
if [ -f "$PILK_HOME/pilk.db" ]; then
    if command -v sqlite3 >/dev/null 2>&1; then
        sqlite3 "$PILK_HOME/pilk.db" ".backup '$DEST/pilk.db'"
    else
        cp "$PILK_HOME/pilk.db" "$DEST/pilk.db"
    fi
    [ -f "$PILK_HOME/pilk.db-wal" ] && cp "$PILK_HOME/pilk.db-wal" "$DEST/pilk.db-wal" || true
    [ -f "$PILK_HOME/pilk.db-shm" ] && cp "$PILK_HOME/pilk.db-shm" "$DEST/pilk.db-shm" || true
    echo "[backup] copied pilk.db" >&2
else
    echo "[backup] no pilk.db found at $PILK_HOME/pilk.db (skipping)" >&2
fi

# 2. Brain vault — markdown notes, projects, sessions, persona.
if [ -d "$BRAIN_DIR" ]; then
    cp -R "$BRAIN_DIR" "$DEST/PILK-brain"
    echo "[backup] copied brain vault from $BRAIN_DIR" >&2
else
    echo "[backup] no brain vault at $BRAIN_DIR (skipping)" >&2
fi

# 3. State files (active project, telegram bridge offset, etc.)
if [ -d "$PILK_HOME/state" ]; then
    cp -R "$PILK_HOME/state" "$DEST/state"
    echo "[backup] copied state from $PILK_HOME/state" >&2
fi

# 4. Identity (persona seed, accounts metadata)
if [ -d "$PILK_HOME/identity" ]; then
    cp -R "$PILK_HOME/identity" "$DEST/identity"
    echo "[backup] copied identity from $PILK_HOME/identity" >&2
fi

# 5. Agent manifests (so an agent rewrite is reversible)
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ -d "$REPO_ROOT/agents" ]; then
    cp -R "$REPO_ROOT/agents" "$DEST/agents"
    echo "[backup] copied agent manifests from $REPO_ROOT/agents" >&2
fi

# 6. Drop a manifest that records what's in this backup + how to restore.
cat > "$DEST/RESTORE.md" <<EOF
# PILK backup — $TS

## Contents
- pilk.db (+ WAL/shm sidecars if present)
- PILK-brain/ (full markdown vault)
- state/ (active_project.txt, telegram-bridge.json, etc.)
- identity/
- agents/ (manifest snapshot from $REPO_ROOT/agents)

## How to restore (manual)

Stop pilkd first:
\`\`\`
lsof -ti:7424 | xargs kill 2>/dev/null
\`\`\`

Then copy files back:
\`\`\`
cp '$DEST/pilk.db' '$PILK_HOME/pilk.db'
rm -rf '$BRAIN_DIR' && cp -R '$DEST/PILK-brain' '$BRAIN_DIR'
rm -rf '$PILK_HOME/state' && cp -R '$DEST/state' '$PILK_HOME/state'
\`\`\`

(For agents/, copy back into the repo's agents/ folder if you need
to revert manifest changes. Don't restore agents if your code has
moved on — manifests are coupled to code.)

Restart pilkd: \`uv run pilkd\`
EOF

echo "$DEST"
