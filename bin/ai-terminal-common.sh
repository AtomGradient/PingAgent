#!/usr/bin/env bash
# Shared by the mailbox CLIs. Keep compatible with macOS Bash 3.2.

ai_detect_terminal() {
  TERMINAL_BACKEND=""
  SESSION_UUID=""
  SESSION_ERROR=""
  if [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
    TERMINAL_BACKEND="terminal"
    if [[ "${TERM_SESSION_ID:-}" =~ ^[a-zA-Z0-9._:-]+$ ]]; then
      # Children of an AI may have a different (or no) controlling TTY. Use
      # the inherited session identity for sender lookup, never their TTY.
      SESSION_UUID="terminal:$TERM_SESSION_ID"
    else
      SESSION_ERROR="TERM_SESSION_ID is missing or invalid. Run from a macOS Terminal tab."
    fi
  elif [[ -n "${ITERM_SESSION_ID:-}" ]]; then
    TERMINAL_BACKEND="iterm2"
    SESSION_UUID="${ITERM_SESSION_ID##*:}"
    if [[ "$SESSION_UUID" == "$ITERM_SESSION_ID" || ! "$SESSION_UUID" =~ ^[a-zA-Z0-9._-]+$ ]]; then
      SESSION_UUID=""
      SESSION_ERROR="Cannot parse UUID from ITERM_SESSION_ID. Run from an iTerm2 pane."
    fi
  else
    SESSION_ERROR="No supported terminal session found. Run from an iTerm2 pane or macOS Terminal tab."
  fi
}

ai_pane_field() {
  local file="$1" key="$2"
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg key "$key" '.[$key] // empty' "$file"
  else
    # Registration fields consumed here are simple strings (no JSON escapes).
    sed -n "s/.*\"$key\": *\"\([^\"]*\)\".*/\1/p" "$file" | head -n 1
  fi
}

ai_json_quote() {
  local value="$1"
  value=${value//\\/\\\\}
  value=${value//\"/\\\"}
  value=${value//$'\n'/\\n}
  value=${value//$'\r'/\\r}
  value=${value//$'\t'/\\t}
  printf '"%s"' "$value"
}

ai_process_fingerprint() {
  LC_ALL=C ps -p "$1" -o lstart= -o tty= | awk '{$1=$1; print}'
}

ai_resolve_terminal_tab() {
  local terminal_device output osascript_bin owner_pid parent_info parent_pid parent_tty
  osascript_bin="${AI_COLLAB_OSASCRIPT_BIN:-/usr/bin/osascript}"
  # ps works even when stdin is piped. Registration must run in the actual
  # Terminal tab, before launching the AI, not in a tool-created pseudo-TTY.
  terminal_device=$(ps -p $$ -o tty= | tr -d '[:space:]')
  if [[ ! "$terminal_device" =~ ^ttys[0-9]+$ ]]; then
    echo "Cannot determine this Terminal tab's TTY. Run ai-pane-register directly in Terminal." >&2
    return 1
  fi
  TERMINAL_TTY="/dev/$terminal_device"
  # Bind delivery to the lifetime of the tab's oldest controlling-TTY
  # ancestor (normally login). A closed tab's TTY can be reused in the same
  # window; window ID + TTY alone could then send to the wrong tab.
  owner_pid="$PPID"
  TERMINAL_OWNER_PID=""
  while [[ "$owner_pid" =~ ^[0-9]+$ && "$owner_pid" -gt 1 ]]; do
    parent_info=$(ps -p "$owner_pid" -o ppid= -o tty=) || break
    read -r parent_pid parent_tty <<< "$parent_info"
    [[ "$parent_tty" == "$terminal_device" ]] || break
    TERMINAL_OWNER_PID="$owner_pid"
    [[ "$parent_pid" != "$owner_pid" ]] || break
    owner_pid="$parent_pid"
  done
  if [[ -z "$TERMINAL_OWNER_PID" ]]; then
    echo "Cannot identify this Terminal tab's session process. Run ai-pane-register directly in Terminal." >&2
    return 1
  fi
  TERMINAL_OWNER_FINGERPRINT=$(ai_process_fingerprint "$TERMINAL_OWNER_PID")
  if [[ -z "$TERMINAL_OWNER_FINGERPRINT" ]]; then
    echo "Terminal session process exited during registration; re-run ai-pane-register." >&2
    return 1
  fi
  if ! output=$("$osascript_bin" -e '
on run argv
  set targetTTY to item 1 of argv
  if not (application "Terminal" is running) then return "session not found"
  tell application "Terminal"
    repeat with aWindow in windows
      repeat with aTab in tabs of aWindow
        if tty of aTab is targetTTY then return (id of aWindow) as text
      end repeat
    end repeat
  end tell
  return "session not found"
end run
' -- "$TERMINAL_TTY" 2>&1); then
    echo "Cannot locate Terminal tab: $output" >&2
    echo "Allow Automation access to Terminal in System Settings if macOS denied it." >&2
    return 1
  fi
  if [[ ! "$output" =~ ^[0-9]+$ ]]; then
    echo "Cannot locate Terminal tab for $TERMINAL_TTY: $output" >&2
    return 1
  fi
  TERMINAL_WINDOW_ID="$output"
}
