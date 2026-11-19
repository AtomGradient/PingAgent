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
GHOSTTY_SURFACE_ID = "0x1234567890abcdef"
GHOSTTY_TERMINAL_ID = "B36E598A-A1F3-41F7-A753-128CA5A4A351"


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
        if key in ("TERM_PROGRAM", "TERM_SESSION_ID", "ITERM_SESSION_ID", "GHOSTTY_SURFACE_ID") or key.startswith("AI_COLLAB_HARNESS_"):
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


def configure_ghostty(env):
    # Stale inherited identities from other terminal apps must be ignored.
    env.update(
        TERM_PROGRAM="ghostty", GHOSTTY_SURFACE_ID=GHOSTTY_SURFACE_ID,
        ITERM_SESSION_ID="w0t0p0:stale-iterm-session",
        FAKE_OSASCRIPT_MODE="ghostty_registration",
    )


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


@pytest.mark.parametrize("backend", ["terminal", "ghostty"])
def test_nested_mailbox_lookup_and_doctor_use_terminal_identity(setup, backend):
    project, programs, env = setup
    if backend == "ghostty":
        configure_ghostty(env)
    info = register(setup)
    pane(project, "terminal_two", terminal="terminal", session_uuid="terminal:two")
    child = project / "child"
    # This differs only where the real session key has a regexp dot.
    decoy_id = ("ghostty:0x1234567890abcdee" if backend == "ghostty"
                else "terminal:terminal-sessionXone")
    pane(child, "decoy", session_uuid=decoy_id)
    # Sender lookup uses the inherited identity, not a tool's current TTY.
    env["FAKE_TTY"] = "??"
    sent = run(programs, child, env, "ai-ping", "terminal_two", "hello")
    assert sent.returncode == 0, sent.stderr
    assert "skipped nearer mailbox" in sent.stderr
    assert len(list((project / ".ai-mailbox/inbox/terminal_two").glob("*.md"))) == 1
    assert not (child / ".ai-mailbox/inbox").exists()
    doctor = run(programs, child, env, "ai-ping", "doctor")
    assert "OK   2 current session" in doctor.stdout
    assert "registered as 'terminal_one'" in doctor.stdout
    assert info["session_uuid"] in doctor.stdout
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
@pytest.mark.parametrize("backend", ["terminal", "ghostty"])
def test_terminal_delivery_confirms_only_explicit_success(setup, mode, reason, backend):
    project, programs, env = setup
    if backend == "ghostty":
        configure_ghostty(env)
    register(setup)
    calls = Path(env["FAKE_OSASCRIPT_CALLS"])
    calls.unlink()
    env["FAKE_OSASCRIPT_MODE"] = mode
    # Dispatch routing comes from the receiver record, not watcher environment.
    env["TERM_PROGRAM"] = "iTerm.app"
    env["GHOSTTY_SURFACE_ID"] = "0xffffffffffffffff"
    message = project / ".ai-mailbox/inbox/terminal_one/message.md"
    message.write_text("---\nid: message\nfrom: peer\nkind: msg\n---\nhello\n")
    result = run(REPO / "bin", project, env, "ai-collab-watch", "terminal_one", str(project / ".ai-mailbox"))
    assert result.returncode == 0, result.stderr
    sidecar = Path(str(message) + ".dispatched")
    assert sidecar.exists() == (reason is None)
    if reason:
        assert f"reason={reason}" in result.stdout
    invocation = calls.read_text()
    assert 'tell application "iTerm2"' not in invocation
    if backend == "ghostty":
        assert 'tell application "Ghostty"' in invocation
        assert 'tell application "Terminal"' not in invocation
        assert f"arg={GHOSTTY_TERMINAL_ID}\narg=/dev/ttys042\narg=[ai-collab 收信]" in invocation
        assert "input text targetMsg to aTerminal" in invocation
        assert 'send key "enter" to aTerminal' in invocation
        assert "id of aTerminal is targetID and tty of aTerminal is targetTTY" in invocation
        assert "focused terminal" not in invocation
    else:
        assert 'tell application "Terminal"' in invocation
        assert "arg=42\narg=/dev/ttys042\narg=[ai-collab 收信]" in invocation
    if sidecar.exists():
        calls.unlink()
        run(REPO / "bin", project, env, "ai-collab-watch", "terminal_one", str(project / ".ai-mailbox"))
        assert not calls.exists(), "Confirmed messages must not be reinjected"


@pytest.mark.parametrize("failure", ["reused_tty", "missing_target", "unknown_backend"])
@pytest.mark.parametrize("backend", ["terminal", "ghostty"])
def test_stale_or_invalid_terminal_record_never_injects(setup, failure, backend):
    project, programs, env = setup
    if backend == "ghostty":
        configure_ghostty(env)
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


def test_ghostty_registration_and_unregister(setup):
    project, programs, env = setup
    configure_ghostty(env)
    info = register(setup, "ghostty_one")
    assert info["terminal"] == "ghostty"
    assert info["session_uuid"] == f"ghostty:{GHOSTTY_SURFACE_ID}"
    assert info["ghostty_surface_id"] == GHOSTTY_SURFACE_ID
    assert info["ghostty_terminal_id"] == GHOSTTY_TERMINAL_ID
    assert info["terminal_tty"] == "/dev/ttys042"
    assert info["terminal_owner_fingerprint"] == FINGERPRINT
    assert info["cwd"] == str(project)
    calls = Path(env["FAKE_OSASCRIPT_CALLS"])
    invocation = calls.read_text()
    assert 'tell application "Ghostty"' in invocation
    assert "arg=/dev/ttys042\n" in invocation
    assert "focused terminal" not in invocation
    assert "input text" not in invocation  # Resolving a pane is read-only.
    calls.unlink()
    env["FAKE_TTY"] = "ttys099"  # A tool-created PTY is not the parent pane.
    result = run(programs, project, env, "ai-pane-unregister", input="")
    assert result.returncode == 0, result.stderr
    assert "Auto-detected role: ghostty_one" in result.stdout
    assert not (project / ".ai-mailbox/.panes/ghostty_one.json").exists()
    assert not (project / ".ai-mailbox/.watch-ghostty_one.pid").exists()
    assert (project / ".ai-mailbox/inbox/ghostty_one").exists()
    assert not calls.exists()


@pytest.mark.parametrize("peer_backend", ["iterm2", "terminal", "ghostty"])
def test_ghostty_mixed_terminal_round_trip_without_controlling_tty(setup, peer_backend):
    project, programs, env = setup
    configure_ghostty(env)
    register(setup, "ghostty_one")
    calls = Path(env["FAKE_OSASCRIPT_CALLS"])
    calls.unlink()
    env["FAKE_TTY"] = "??"
    peer_env = dict(env)
    if peer_backend == "iterm2":
        pane(project, "peer", session_uuid="legacy-iterm-uuid")
        peer_env.update(TERM_PROGRAM="iTerm.app", ITERM_SESSION_ID="w0t0p0:legacy-iterm-uuid")
    elif peer_backend == "terminal":
        pane(project, "peer", terminal="terminal", session_uuid="terminal:peer-session")
        peer_env.update(TERM_PROGRAM="Apple_Terminal", TERM_SESSION_ID="peer-session")
    else:
        pane(project, "peer", terminal="ghostty", session_uuid="ghostty:0x0000000000000001")
        peer_env["GHOSTTY_SURFACE_ID"] = "0x0000000000000001"
    sent = run(programs, project, env, "ai-ping", "peer", input='hello 中文\n"quoted" \\ input\n')
    assert sent.returncode == 0, sent.stderr
    message = next((project / ".ai-mailbox/inbox/peer").glob("*.md"))
    assert "from: ghostty_one\n" in message.read_text()
    assert 'hello 中文\n"quoted" \\ input' in message.read_text()
    reply = run(programs, project, peer_env, "ai-ping", "ghostty_one", "--reply-to", message.stem, "received")
    assert reply.returncode == 0, reply.stderr
    received = next((project / ".ai-mailbox/inbox/ghostty_one").glob("*.md")).read_text()
    assert "from: peer\n" in received
    assert f"reply_to: {message.stem}\n" in received
    assert not calls.exists(), "Sender identity must not need AppleScript or the current TTY"


@pytest.mark.parametrize("changes,expected", [
    ({"GHOSTTY_SURFACE_ID": ""}, "GHOSTTY_SURFACE_ID"),
    ({"GHOSTTY_SURFACE_ID": 'bad"id'}, "GHOSTTY_SURFACE_ID"),
    ({"GHOSTTY_SURFACE_ID": GHOSTTY_TERMINAL_ID}, "GHOSTTY_SURFACE_ID"),
    ({"GHOSTTY_SURFACE_ID": "0x0000000000000000"}, "GHOSTTY_SURFACE_ID"),
    ({"FAKE_TTY": "??"}, "Cannot determine"),
    ({"FAKE_OSASCRIPT_MODE": "session_missing"}, "Cannot locate Ghostty"),
    ({"FAKE_OSASCRIPT_MODE": "tcc_denied"}, "Automation"),
    ({"FAKE_GHOSTTY_TERMINAL_ID": GHOSTTY_SURFACE_ID}, "Cannot locate Ghostty"),
])
def test_invalid_ghostty_registration_leaves_no_mailbox(setup, changes, expected):
    project, programs, env = setup
    configure_ghostty(env)
    env.update(changes)
    result = run(programs, project, env, "ai-pane-register", "ghostty_one")
    assert result.returncode != 0
    assert expected in result.stderr
    assert not (project / ".ai-mailbox").exists()


@pytest.mark.parametrize("changes", [
    {"ghostty_terminal_id": ""},
    {"ghostty_terminal_id": GHOSTTY_SURFACE_ID},
    {"ghostty_surface_id": GHOSTTY_TERMINAL_ID},
    {"session_uuid": "ghostty:0x0000000000000001"},
])
def test_inconsistent_ghostty_record_never_injects(setup, changes):
    project, programs, env = setup
    configure_ghostty(env)
    info = register(setup, "ghostty_one")
    info.update(changes)
    pane(project, "ghostty_one", **info)
    message = project / ".ai-mailbox/inbox/ghostty_one/message.md"
    message.write_text("---\nid: message\nfrom: peer\nkind: msg\n---\n")
    calls = Path(env["FAKE_OSASCRIPT_CALLS"])
    calls.unlink()
    result = run(REPO / "bin", project, env, "ai-collab-watch", "ghostty_one", str(project / ".ai-mailbox"))
    assert result.returncode != 0
    assert not calls.exists()
    assert not Path(str(message) + ".dispatched").exists()


def test_whoami_fast_lookup_nested_mailbox_and_reserved_role(setup):
    project, programs, env = setup
    configure_ghostty(env)
    register(setup, "collgue_a")
    env["FAKE_TTY"] = "??"

    nested = project / "subrepo"
    (nested / ".ai-mailbox/.panes").mkdir(parents=True)
    pane(nested, "other_role", session_uuid="ghostty:0x0000000000000099")

    plain = run(programs, nested, env, "ai-ping", "whoami")
    assert plain.returncode == 0, plain.stderr
    assert plain.stdout.strip() == "collgue_a"

    as_json = run(programs, nested, env, "ai-ping", "whoami", "--json")
    assert as_json.returncode == 0, as_json.stderr
    payload = json.loads(as_json.stdout)
    assert payload == {
        "role": "collgue_a",
        "mailbox": str(project / ".ai-mailbox"),
        "session_uuid": f"ghostty:{GHOSTTY_SURFACE_ID}",
    }

    unregistered_env = dict(env, GHOSTTY_SURFACE_ID="0x0000000000000042")
    unregistered = run(programs, project, unregistered_env, "ai-ping", "whoami")
    assert unregistered.returncode == 1
    assert "not registered" in unregistered.stderr

    reserved_reg = run(programs, project, env, "ai-pane-register", "whoami")
    assert reserved_reg.returncode != 0
    assert "reserved" in reserved_reg.stderr

    reserved_unreg = run(programs, project, env, "ai-pane-unregister", "whoami")
    assert reserved_unreg.returncode != 0
    assert "reserved" in reserved_unreg.stderr

    reserved_send = run(programs, project, env, "ai-ping", "whoami_ok", "--from", "whoami", "hi")
    assert reserved_send.returncode != 0
    assert "reserved" in reserved_send.stderr

    for harness_var in ("AI_COLLAB_HARNESS_CONTEXT", "AI_COLLAB_HARNESS_CLIENT_EXECUTABLE", "AI_COLLAB_HARNESS_CLIENT_PYTHONPATH"):
        harness_env = dict(env, **{harness_var: "/tmp/fake-harness-marker"})
        harness = run(programs, project, harness_env, "ai-ping", "whoami")
        assert harness.returncode == 2
        assert "Harness" in harness.stderr

    # If the same session is also registered in the nested mailbox while an ancestor
    # mailbox holds the peer, bare 'whoami' fails closed on ambiguity whereas
    # 'whoami --to peer_root' matches select_mailbox_for_auto_from routing.
    pane(project, "peer_root", session_uuid="ghostty:0x0000000000000088")
    pane(nested, "local_seat", session_uuid=f"ghostty:{GHOSTTY_SURFACE_ID}")
    ambig = run(programs, nested, env, "ai-ping", "whoami")
    assert ambig.returncode == 1
    assert "Ambiguous registration" in ambig.stderr

    disambig = run(programs, nested, env, "ai-ping", "whoami", "--to", "peer_root")
    assert disambig.returncode == 0, disambig.stderr
    assert disambig.stdout.strip() == "collgue_a"
