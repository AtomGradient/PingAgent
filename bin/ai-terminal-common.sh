#!/usr/bin/env bash
# Shared by the mailbox CLIs. Keep compatible with macOS Bash 3.2.

ai_detect_terminal() {
  TERMINAL_BACKEND=""
  SESSION_UUID=""
  SESSION_ERROR=""
  if [[ "${TERM_PROGRAM:-}" == "ghostty" ]]; then
    TERMINAL_BACKEND="ghostty"
    # Ghostty 1.4+ exports a core surface ID. It is NOT the UUID exposed
    # by its macOS AppleScript terminal object; resolve that at registration.
    if ai_valid_ghostty_surface_id "${GHOSTTY_SURFACE_ID:-}"; then
      SESSION_UUID="ghostty:$GHOSTTY_SURFACE_ID"
    else
      SESSION_ERROR="GHOSTTY_SURFACE_ID is missing or invalid. Use macOS Ghostty 1.4.0+ and open a new pane after upgrading."
    fi
  elif [[ "${TERM_PROGRAM:-}" == "Apple_Terminal" ]]; then
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
    SESSION_ERROR="No supported terminal session found. Run from iTerm2, macOS Terminal, or macOS Ghostty 1.4.0+."
  fi
}

ai_valid_ghostty_surface_id() {
  [[ "$1" =~ ^0x[0-9a-f]{16}$ && "$1" != "0x0000000000000000" ]]
}

ai_valid_ghostty_terminal_id() {
  [[ "$1" =~ ^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$ ]]
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

ai_resolve_terminal_process() {
  local label="$1" terminal_device owner_pid parent_info parent_pid parent_tty
  # ps works even when stdin is piped. Registration must run in the actual
  # terminal, before launching the AI, not in a tool-created pseudo-TTY.
  terminal_device=$(ps -p $$ -o tty= | tr -d '[:space:]')
  if [[ ! "$terminal_device" =~ ^ttys[0-9]+$ ]]; then
    echo "Cannot determine this $label's TTY. Run ai-pane-register directly in its shell." >&2
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
    echo "Cannot identify this $label's session process. Run ai-pane-register directly in its shell." >&2
    return 1
  fi
  TERMINAL_OWNER_FINGERPRINT=$(ai_process_fingerprint "$TERMINAL_OWNER_PID")
  if [[ -z "$TERMINAL_OWNER_FINGERPRINT" ]]; then
    echo "$label session process exited during registration; re-run ai-pane-register." >&2
    return 1
  fi
}

ai_resolve_terminal_tab() {
  local output osascript_bin
  osascript_bin="${AI_COLLAB_OSASCRIPT_BIN:-/usr/bin/osascript}"
  ai_resolve_terminal_process "Terminal tab" || return 1
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

ai_resolve_ghostty_terminal() {
  local output osascript_bin
  osascript_bin="${AI_COLLAB_OSASCRIPT_BIN:-/usr/bin/osascript}"
  ai_resolve_terminal_process "Ghostty pane" || return 1
  # The inherited core surface ID identifies senders without a TTY. The
  # AppleScript UUID identifies delivery targets. Bind the two using this
  # pane's TTY, never its title, working directory, or current focus.
  if ! output=$("$osascript_bin" -e '
on run argv
  set targetTTY to item 1 of argv
  if not (application "Ghostty" is running) then return "session not found"
  tell application "Ghostty"
    repeat with aTerminal in terminals
      if tty of aTerminal is targetTTY then return (id of aTerminal) as text
    end repeat
  end tell
  return "session not found"
end run
' -- "$TERMINAL_TTY" 2>&1); then
    echo "Cannot locate Ghostty pane: $output" >&2
    echo "Use macOS Ghostty 1.4.0+ with macos-applescript enabled; allow Automation access to Ghostty in System Settings." >&2
    return 1
  fi
  if ! ai_valid_ghostty_terminal_id "$output"; then
    echo "Cannot locate Ghostty pane for $TERMINAL_TTY: $output" >&2
    echo "Run ai-pane-register directly in a Ghostty pane with macos-applescript enabled." >&2
    return 1
  fi
  GHOSTTY_TERMINAL_ID="$output"
}

ai_set_pane_title() {
  local title="$1" osascript_bin script output exit_code
  local -a target_args
  TITLE_ERROR=""
  osascript_bin="${AI_COLLAB_OSASCRIPT_BIN:-/usr/bin/osascript}"
  if [[ ! -x "$osascript_bin" ]]; then
    TITLE_ERROR="osascript_not_found"
    return 1
  fi
  case "$TERMINAL_BACKEND" in
    terminal)
      if [[ -z "${TERMINAL_WINDOW_ID:-}" || -z "${TERMINAL_TTY:-}" ]]; then
        TITLE_ERROR="missing_terminal_target"
        return 1
      fi
      script='
on run argv
  set targetWindow to item 1 of argv as integer
  set targetTTY to item 2 of argv
  set targetTitle to item 3 of argv
  if not (application "Terminal" is running) then return "session not found: " & targetTTY
  tell application "Terminal"
    repeat with aWindow in windows
      if id of aWindow is targetWindow then
        repeat with aTab in tabs of aWindow
          if tty of aTab is targetTTY then
            set custom title of aTab to targetTitle
            set title displays custom title of aTab to true
            return "ok"
          end if
        end repeat
      end if
    end repeat
  end tell
  return "session not found: " & targetTTY
end run
'
      target_args=("$TERMINAL_WINDOW_ID" "$TERMINAL_TTY" "$title")
      ;;
    ghostty)
      if [[ -z "${GHOSTTY_TERMINAL_ID:-}" || -z "${TERMINAL_TTY:-}" ]]; then
        TITLE_ERROR="missing_ghostty_target"
        return 1
      fi
      script='
on run argv
  set targetID to item 1 of argv
  set targetTTY to item 2 of argv
  set targetTitle to item 3 of argv
  if not (application "Ghostty" is running) then return "session not found: " & targetID
  tell application "Ghostty"
    repeat with aTerminal in terminals
      if id of aTerminal is targetID and tty of aTerminal is targetTTY then
        set name override of aTerminal to targetTitle
        return "ok"
      end if
    end repeat
  end tell
  return "session not found: " & targetID
end run
'
      target_args=("$GHOSTTY_TERMINAL_ID" "$TERMINAL_TTY" "$title")
      ;;
    iterm2)
      if [[ -z "${SESSION_UUID:-}" ]]; then
        TITLE_ERROR="missing_iterm_session"
        return 1
      fi
      script='
on run argv
  set targetID to item 1 of argv
  set targetTitle to item 2 of argv
  if not (application "iTerm2" is running) then return "session not found: " & targetID
  tell application "iTerm2"
    repeat with aWindow in windows
      repeat with aTab in tabs of aWindow
        repeat with aSession in sessions of aTab
          if id of aSession is targetID then
            set name of aSession to targetTitle
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
    return "session not found: " & targetID
  end tell
end run
'
      target_args=("$SESSION_UUID" "$title")
      ;;
    *)
      TITLE_ERROR="unsupported_terminal_backend"
      return 1
      ;;
  esac

  if output=$("$osascript_bin" -e "$script" -- "${target_args[@]}" 2>&1); then
    exit_code=0
  else
    exit_code=$?
  fi
  if [[ "$exit_code" -eq 0 && "$output" == "ok" ]]; then
    return 0
  fi
  if [[ "$output" == *"session not found:"* ]]; then
    TITLE_ERROR="session_not_found"
  elif [[ "$output" == *"(-1743)"* || "$output" == *"error number -1743"* ]]; then
    TITLE_ERROR="automation_denied_-1743"
  elif [[ "$exit_code" -ne 0 ]]; then
    TITLE_ERROR="osascript_exit_$exit_code"
  else
    TITLE_ERROR="unexpected_osascript_response"
  fi
  return 1
}
