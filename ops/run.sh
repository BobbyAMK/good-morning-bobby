#!/bin/bash
# Layer 2 of failure alerting: catches what the Python never saw -- hard crashes, OOM
# kills, import errors, a missing binary. Layer 1 is the in-process except; layer 3 is
# the watchdog, which is the only one that can see a job that never ran at all.
set -uo pipefail
AGENT="${1:?usage: run.sh <agent-name> <command...>}"; shift
LOG="$HOME/Library/Logs/pet-agents/${AGENT}.wrapper.log"
mkdir -p "$(dirname "$LOG")"
echo "$(date -u +%FT%TZ) START $AGENT $*" >>"$LOG"
"$@"; rc=$?
echo "$(date -u +%FT%TZ) EXIT  $AGENT rc=$rc" >>"$LOG"
if [ "$rc" -ne 0 ]; then
  /usr/bin/osascript -e "display notification \"$AGENT failed (rc=$rc)\" with title \"Good Morning Bobby\" sound name \"Basso\"" || true
fi
exit "$rc"
