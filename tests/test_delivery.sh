#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
export PATH="$REPO_ROOT/bin:$PATH"

TEST_ROOT=$(mktemp -d "${TMPDIR:-/tmp}/pingagent-delivery.XXXXXX")
cleanup() {
  ai-watch-service stop target "$TEST_ROOT/.ai-mailbox" >/dev/null 2>&1 || true
  rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

MAILBOX="$TEST_ROOT/.ai-mailbox"
mkdir -p "$MAILBOX/.panes" "$MAILBOX/inbox/target" "$MAILBOX/inbox/sender" "$MAILBOX/sent"

cat > "$MAILBOX/.panes/target.json" <<'EOF'
{
  "role": "target",
  "session_uuid": "test-target-session"
}
EOF
cat > "$MAILBOX/.panes/sender.json" <<'EOF'
{
  "role": "sender",
  "session_uuid": "test-sender-session"
}
EOF

MOCK_OK="$TEST_ROOT/osascript-ok"
cat > "$MOCK_OK" <<'EOF'
#!/usr/bin/env bash
printf 'ok\n'
EOF
chmod 0755 "$MOCK_OK"

MOCK_FAIL="$TEST_ROOT/osascript-fail"
cat > "$MOCK_FAIL" <<'EOF'
#!/usr/bin/env bash
printf 'session not found: test-target-session\n'
EOF
chmod 0755 "$MOCK_FAIL"

wait_for_file() {
  local path="$1" attempt=0
  while (( attempt < 50 )); do
    [[ -f "$path" ]] && return 0
    sleep 0.1
    attempt=$((attempt + 1))
  done
  return 1
}

write_message() {
  local id="$1"
  cat > "$MAILBOX/inbox/target/$id.md" <<EOF
---
id: $id
from: sender
to: target
kind: notice
created: 2026-08-12T00:00:00+0800
---

delivery test
EOF
}

echo "test: successful injection writes dispatched only after ok"
export AI_COLLAB_OSASCRIPT="$MOCK_OK"
ai-watch-service start target "$MAILBOX" >/dev/null
write_message success
wait_for_file "$MAILBOX/inbox/target/success.md.dispatched"
ai-watch-service status target "$MAILBOX" >/dev/null
ai-watch-service stop target "$MAILBOX"
if ai-watch-service status target "$MAILBOX" >/dev/null 2>&1; then
  echo "watcher should be stopped" >&2
  exit 1
fi

echo "test: failed injection stays pending"
export AI_COLLAB_OSASCRIPT="$MOCK_FAIL"
ai-watch-service start target "$MAILBOX" >/dev/null
write_message failure
sleep 1.3
if [[ -f "$MAILBOX/inbox/target/failure.md.dispatched" ]]; then
  echo "failed injection was incorrectly marked dispatched" >&2
  exit 1
fi
ai-watch-service stop target "$MAILBOX"

echo "test: ai-ping auto-recovers a stopped registered target"
export AI_COLLAB_OSASCRIPT="$MOCK_OK"
export ITERM_SESSION_ID="test:test-sender-session"
PING_OUTPUT=$(cd "$TEST_ROOT" && ai-ping target --kind notice "auto-heal test" 2>&1)
printf '%s\n' "$PING_OUTPUT"
grep -q "attempting detached recovery" <<<"$PING_OUTPUT"
grep -q "notification: dispatched" <<<"$PING_OUTPUT"
ai-watch-service status target "$MAILBOX" >/dev/null

echo "PASS"
