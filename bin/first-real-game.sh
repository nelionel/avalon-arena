#!/bin/bash
# Wait for a player credential, then play one real 7-player game with claude-haiku-5-5.
# Triggered by the presence of /opt/avalon/secrets/credentials.env containing
# ANTHROPIC_API_KEY=... or CLAUDE_CODE_OAUTH_TOKEN=... (gives up after 24 hours).
set -u
CRED=/opt/avalon/secrets/credentials.env
LOG=/opt/avalon/games/first_real_run.log
deadline=$(( $(date +%s) + 86400 ))

has_cred() {
  [ -f "$CRED" ] && grep -qE '^(ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN)=.{20,}' "$CRED"
}

until has_cred; do
  if [ "$(date +%s)" -ge "$deadline" ]; then
    echo "no credential appeared in $CRED within 24h; not starting a game"
    exit 3
  fi
  sleep 5
done
# Let an editor finish writing the file.
prev=""
while :; do
  cur=$(stat -c %Y:%s "$CRED")
  [ "$cur" = "$prev" ] && break
  prev=$cur
  sleep 3
done
chmod 600 "$CRED"
echo "credential found ($(grep -oE '^(ANTHROPIC_API_KEY|CLAUDE_CODE_OAUTH_TOKEN)' "$CRED" | head -1)); starting the first real game" | tee "$LOG"
cd /opt/avalon && bin/avalon run --players 7 --tag first-real-haiku --timeout 360 >> "$LOG" 2>&1
rc=$?
tail -3 "$LOG"
exit $rc
