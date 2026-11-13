"""Mailbox CLI regression tests; all terminal/process automation is isolated."""

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]
FINGERPRINT = "Fri Nov 13 12:00:00 2026 ttys042"


@pytest.fixture
def setup(tmp_path):
    project = tmp_path / 'project "quoted"'
    project.mkdir()
    programs = tmp_path / "bin"
    shutil.copytree(REPO / "bin", programs)
    # Registration tests only need a live, isolated watcher PID. Delivery
    # tests below execute the real watcher against the same pane records.
    (programs / "ai-collab-watch").write_text("#!/bin/bash\nexec sleep 60\n")
    ps = programs / "ps"
    ps.write_text('''#!/bin/bash
case "$*" in
  *"-o lstart= -o tty=") printf '%s\\n' "${FAKE_FINGERPRINT:-Fri Nov 13 12:00:00 2026 ttys042}" ;;
  *"-o ppid= -o tty=") printf '1 %s\\n' "${FAKE_TTY:-ttys042}" ;;
  *"-o tty=") printf '%s\\n' "${FAKE_TTY:-ttys042}" ;;
  *) exit 1 ;;
esac
''')
    ps.chmod(0o755)
    env = os.environ.copy()
    for key in list(env):
        if key in ("TERM_PROGRAM", "TERM_SESSION_ID", "ITERM_SESSION_ID") or key.startswith("AI_COLLAB_HARNESS_"):
            env.pop(key)
    env.update(
        PATH=f"{programs}:{REPO / 'tests/fixtures/bin'}:{env['PATH']}",
        TERM_PROGRAM="Apple_Terminal",
        TERM_SESSION_ID="terminal-session.one",
        AI_COLLAB_OSASCRIPT_BIN=str(REPO / "tests/fixtures/bin/osascript"),
        FAKE_OSASCRIPT_CALLS=str(tmp_path / "osascript.calls"),
        FAKE_OSASCRIPT_MODE="terminal_registration",
    )
    yield project, programs, env
    for file in project.rglob(".watch-*.pid"):
        try:
            os.kill(int(file.read_text()), signal.SIGTERM)
        except ProcessLookupError:
            pass


def run(programs, cwd, env, command, *args, input=None):
    return subprocess.run(
        [str(programs / command), *args], cwd=cwd, env=env,
        input=input, text=True, capture_output=True, timeout=15,
    )


def register(setup, role="terminal_one"):
    project, programs, env = setup
    result = run(programs, project, env, "ai-pane-register", role)
    assert result.returncode == 0, result.stderr + result.stdout
    return json.loads((project / f".ai-mailbox/.panes/{role}.json").read_text())


def pane(project, role_name, **fields):
    path = project / f".ai-mailbox/.panes/{role_name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields, indent=2))
    return path


def test_terminal_registration_and_unregister_with_piped_stdin(setup):
    project, programs, env = setup
    info = register(setup)
    assert info["terminal"] == "terminal"
    assert info["session_uuid"] == "terminal:terminal-session.one"
    assert info["term_session_id"] == env["TERM_SESSION_ID"]
    assert info["terminal_tty"] == "/dev/ttys042"
    assert info["terminal_window_id"] == "42"
    assert info["terminal_owner_fingerprint"] == FINGERPRINT
    assert info["cwd"] == str(project)
    result = run(programs, project, env, "ai-pane-unregister")
    assert result.returncode == 0, result.stderr
    assert "Auto-detected role: terminal_one" in result.stdout
    assert not (project / ".ai-mailbox/.panes/terminal_one.json").exists()
    assert not (project / ".ai-mailbox/.watch-terminal_one.pid").exists()
    assert (project / ".ai-mailbox/inbox/terminal_one").exists()


def test_terminal_to_iterm_and_reply_from_legacy_registration(setup):
    project, programs, env = setup
    register(setup)
    # Old iTerm registrations have no terminal discriminator.
    pane(project, "iterm_peer", session_uuid="legacy-iterm-uuid")
    # A stale inherited iTerm variable must not override Apple_Terminal.
    env["ITERM_SESSION_ID"] = "w0t0p0:wrong-session"
    env["FAKE_TTY"] = "??"  # An AI tool can have no controlling TTY.
    sent = run(programs, project, env, "ai-ping", "iterm_peer", input="hello 中文\nfrom stdin\n")
    assert sent.returncode == 0, sent.stderr
    message = next((project / ".ai-mailbox/inbox/iterm_peer").glob("*.md"))
    assert "from: terminal_one\n" in message.read_text()
    assert "hello 中文\nfrom stdin" in message.read_text()
    iterm_env = dict(env, TERM_PROGRAM="iTerm.app", ITERM_SESSION_ID="w0t0p0:legacy-iterm-uuid")
    reply = run(programs, project, iterm_env, "ai-ping", "terminal_one", "--reply-to", message.stem, "received")
    assert reply.returncode == 0, reply.stderr
    received = next((project / ".ai-mailbox/inbox/terminal_one").glob("*.md")).read_text()
    assert "from: iterm_peer\n" in received
    assert f"reply_to: {message.stem}\n" in received


def test_nested_mailbox_lookup_and_doctor_use_terminal_identity(setup):
    project, programs, env = setup
    register(setup)
    pane(project, "terminal_two", terminal="terminal", session_uuid="terminal:two")
    child = project / "child"
    # This differs only where the real session key has a regexp dot.
    pane(child, "decoy", session_uuid="terminal:terminal-sessionXone")
    sent = run(programs, child, env, "ai-ping", "terminal_two", "hello")
    assert sent.returncode == 0, sent.stderr
    assert "skipped nearer mailbox" in sent.stderr
    assert len(list((project / ".ai-mailbox/inbox/terminal_two").glob("*.md"))) == 1
    assert not (child / ".ai-mailbox/inbox").exists()
    doctor = run(programs, child, env, "ai-ping", "doctor")
    assert "OK   2 current session" in doctor.stdout
    assert "registered as 'terminal_one'" in doctor.stdout
    unregistered = run(programs, child, env, "ai-pane-unregister")
    assert unregistered.returncode == 0, unregistered.stderr
    assert not (project / ".ai-mailbox/.panes/terminal_one.json").exists()
    assert (child / ".ai-mailbox/.panes/decoy.json").exists()


@pytest.mark.parametrize("changes,expected", [
    ({"TERM_SESSION_ID": ""}, "TERM_SESSION_ID"),
    ({"TERM_SESSION_ID": 'bad"id'}, "TERM_SESSION_ID"),
    ({"TERM_PROGRAM": "unknown"}, "No supported terminal"),
    ({"FAKE_TTY": "??"}, "Cannot determine"),
    ({"FAKE_OSASCRIPT_MODE": "session_missing"}, "Cannot locate"),
    ({"FAKE_OSASCRIPT_MODE": "tcc_denied"}, "Automation"),
])
def test_invalid_terminal_registration_does_not_create_mailbox(setup, changes, expected):
    project, programs, env = setup
    env.update(changes)
    result = run(programs, project, env, "ai-pane-register", "terminal_one")
    assert result.returncode != 0
    assert expected in result.stderr
    assert not (project / ".ai-mailbox").exists()


def test_iterm_registration_does_not_need_terminal_automation(setup):
    project, programs, env = setup
    env.update(TERM_PROGRAM="iTerm.app", ITERM_SESSION_ID="w0t0p0:iterm-uuid", FAKE_TTY="??")
    info = register(setup, "iterm_one")
    assert info["terminal"] == "iterm2"
    assert info["session_uuid"] == "iterm-uuid"
    assert info["terminal_tty"] == ""
    assert not Path(env["FAKE_OSASCRIPT_CALLS"]).exists()


@pytest.mark.parametrize("mode,reason", [
    ("success", None),
    ("session_missing", "session_not_found"),
    ("tcc_denied", "automation_denied_-1743"),
    ("unexpected_success", "unexpected_osascript_response"),
])
def test_terminal_delivery_confirms_only_explicit_success(setup, mode, reason):
    project, programs, env = setup
    register(setup)
    calls = Path(env["FAKE_OSASCRIPT_CALLS"])
    calls.unlink()
    env["FAKE_OSASCRIPT_MODE"] = mode
    # Dispatch routing comes from the receiver record, not watcher environment.
    env["TERM_PROGRAM"] = "iTerm.app"
    message = project / ".ai-mailbox/inbox/terminal_one/message.md"
    message.write_text("---\nid: message\nfrom: peer\nkind: msg\n---\nhello\n")
    result = run(REPO / "bin", project, env, "ai-collab-watch", "terminal_one", str(project / ".ai-mailbox"))
    assert result.returncode == 0, result.stderr
    sidecar = Path(str(message) + ".dispatched")
    assert sidecar.exists() == (reason is None)
    if reason:
        assert f"reason={reason}" in result.stdout
    invocation = calls.read_text()
    assert 'tell application "Terminal"' in invocation
    assert 'tell application "iTerm2"' not in invocation
    assert "arg=42\narg=/dev/ttys042\narg=[ai-collab 收信]" in invocation
    if sidecar.exists():
        calls.unlink()
        run(REPO / "bin", project, env, "ai-collab-watch", "terminal_one", str(project / ".ai-mailbox"))
        assert not calls.exists(), "Confirmed messages must not be reinjected"


@pytest.mark.parametrize("failure", ["reused_tty", "missing_target", "unknown_backend"])
def test_stale_or_invalid_terminal_record_never_injects(setup, failure):
    project, programs, env = setup
    info = register(setup)
    if failure == "reused_tty":
        env["FAKE_FINGERPRINT"] = "Sat Nov 14 12:00:00 2026 ttys042"
    elif failure == "missing_target":
        info.pop("terminal_tty")
    else:
        info["terminal"] = "unknown"
    pane(project, "terminal_one", **info)
    message = project / ".ai-mailbox/inbox/terminal_one/message.md"
    message.write_text("---\nid: message\nfrom: peer\nkind: msg\n---\n")
    calls = Path(env["FAKE_OSASCRIPT_CALLS"])
    calls.unlink()
    result = run(REPO / "bin", project, env, "ai-collab-watch", "terminal_one", str(project / ".ai-mailbox"))
    assert not calls.exists()
    assert not Path(str(message) + ".dispatched").exists()
    if failure == "reused_tty":
        assert "reason=session_not_found" in result.stdout
    else:
        assert result.returncode != 0
