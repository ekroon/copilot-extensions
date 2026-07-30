"""Tests for the live-cutover handoff mux primitives + ``handoff-cutover`` cmd.

These cover the *pure* argv construction and the command's control flow
(mode selection, arg validation, plan reconstruction) with the mux
subprocess boundary mocked -- no real tmux/psmux is invoked.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import textwrap
import uuid
from pathlib import Path

import pytest

from agent_worktrees import __main__ as m
from agent_worktrees import sessions


# â”€â”€ build_mux_new_window_argv (pure) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class TestBuildMuxNewWindowArgv:
    def test_tmux_no_wrapper_strips_identity_and_propagates_env(self):
        argv = sessions.build_mux_new_window_argv(
            "wt1-abc",
            "/w/wt1",
            ["bash", "setup.sh", "--allow-all-tools", "-i", "seed text"],
            {"COPILOT_FEATURE_FLAGS": "x"},
            mux="tmux",
            pane_wrapper="/does/not/exist",
        )
        # target uses the tmux exact-match prefix
        assert argv[:2] == ["tmux", "new-window"]
        assert "-P" in argv and "#{pane_id}" in argv
        i = argv.index("-t")
        assert argv[i + 1] == "=wt-wt1-abc"
        # work dir
        j = argv.index("-c")
        assert argv[j + 1] == "/w/wt1"
        # env propagation
        k = argv.index("-e")
        assert argv[k + 1] == "COPILOT_FEATURE_FLAGS=x"
        # identity strip prefix precedes the command
        assert "env" in argv
        e = argv.index("env")
        assert argv[e:e + 7] == [
            "env", "-u", "WORKTREE_PROJECT", "-u", "WORKTREE_ID",
            "-u", "APERTURE_WORKTREE_ID",
        ]
        # command tail is verbatim (no -- separator, no wrapper)
        assert argv[-5:] == ["bash", "setup.sh", "--allow-all-tools", "-i", "seed text"]
        assert "--" not in argv

    def test_tmux_with_wrapper_wraps_command(self, tmp_path):
        wrapper = tmp_path / "pane-wrapper.sh"
        wrapper.write_text("#!/usr/bin/env bash\nexec \"$@\"\n")
        argv = sessions.build_mux_new_window_argv(
            "id1", "/w", ["copilot", "-i", "hi"], None,
            mux="tmux", pane_wrapper=str(wrapper),
        )
        # env -u ... bash <wrapper> copilot -i hi
        assert "bash" in argv
        b = argv.index("bash")
        assert argv[b + 1] == str(wrapper)
        assert argv[b + 2:] == ["copilot", "-i", "hi"]

    def test_psmux_runs_command_directly_no_identity_prefix(self):
        argv = sessions.build_mux_new_window_argv(
            "id2", "C:/w", ["pwsh.exe", "-File", "s.ps1", "-i", "seed"], None,
            mux="psmux",
        )
        assert argv[:2] == ["psmux", "new-window"]
        # psmux target has NO '=' prefix
        i = argv.index("-t")
        assert argv[i + 1] == "wt-id2"
        # no identity-strip prefix on Windows
        assert "env" not in argv
        assert argv[-5:] == ["pwsh.exe", "-File", "s.ps1", "-i", "seed"]

    def test_psmux_runs_command_verbatim_no_quoting(self):
        # The seed is NOT a launch arg (it is send-keys'd), so the psmux branch
        # runs the command verbatim -- no quoting layer that could break the spawn.
        argv = sessions.build_mux_new_window_argv(
            "id2", "C:/w",
            ["pwsh.exe", "--allow-all-tools"], None, mux="psmux",
        )
        assert argv[-2:] == ["pwsh.exe", "--allow-all-tools"]

    def test_empty_work_dir_omits_c_flag(self):
        argv = sessions.build_mux_new_window_argv(
            "id3", "", ["copilot"], None, mux="tmux", pane_wrapper="/nope",
        )
        assert "-c" not in argv


# â”€â”€ mux_new_window / mux_retire_pane (subprocess mocked) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
class TestMuxNewWindow:
    def test_success_returns_new_pane(self, monkeypatch):
        class R:
            returncode = 0
            stdout = "%7\n"
            stderr = ""

        import subprocess
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
        out = sessions.mux_new_window("id", "/w", ["copilot"], None, mux="tmux")
        assert out["ok"] is True
        assert out["new_pane"] == "%7"

    def test_failure_returns_error(self, monkeypatch):
        class R:
            returncode = 1
            stdout = ""
            stderr = "no such session"

        import subprocess
        monkeypatch.setattr(subprocess, "run", lambda *a, **k: R())
        out = sessions.mux_new_window("id", "/w", ["copilot"], None, mux="tmux")
        assert out["ok"] is False
        assert "no such session" in out["error"]


class TestMuxRetirePane:
    def test_already_gone(self, monkeypatch):
        monkeypatch.setattr(sessions, "_mux_pane_alive", lambda p, b: False)
        out = sessions.mux_retire_pane("%3", mux="tmux")
        assert out == {"ok": True, "pane": "%3", "gone": True,
                       "method": "already-gone"}

    def test_graceful_quit(self, monkeypatch):
        # alive once (initial check), then gone after the double Ctrl-C
        states = iter([True, False])
        monkeypatch.setattr(sessions, "_mux_pane_alive",
                            lambda p, b: next(states))
        import subprocess
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 0})())
        out = sessions.mux_retire_pane("%3", mux="tmux", ctrl_c_gap=0,
                                       poll_interval=0, settle_timeout=1)
        assert out["gone"] is True
        assert out["method"] == "graceful"

    def test_hard_kill_fallback(self, monkeypatch):
        # never gone via graceful; kill-pane also fails to remove it
        monkeypatch.setattr(sessions, "_mux_pane_alive", lambda p, b: True)
        import subprocess
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **k: type("R", (), {"returncode": 0})())
        out = sessions.mux_retire_pane("%3", mux="tmux", ctrl_c_gap=0,
                                       poll_interval=0, settle_timeout=0)
        assert out["gone"] is False
        assert out["method"] == "failed"


# â”€â”€ cmd_handoff_cutover control flow â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
def _ns(**kw):
    base = dict(seed=None, worktree_id=None, old_pane=None, retire_pane=None,
                dry_run=False, copilot_args=[], recovery=False)
    base.update(kw)
    return argparse.Namespace(**base)


class TestCmdHandoffCutover:
    def test_retire_mode(self, monkeypatch, capfd):
        monkeypatch.setattr(sessions, "mux_retire_pane",
                            lambda p, **k: {"ok": True, "pane": p, "gone": True,
                                            "method": "graceful"})
        rc = m.cmd_handoff_cutover(_ns(retire_pane="%9"))
        assert rc == 0
        out = json.loads(capfd.readouterr().out)
        assert out["pane"] == "%9" and out["gone"] is True

    def test_spawn_requires_seed(self, capfd):
        rc = m.cmd_handoff_cutover(_ns())
        assert rc == 1
        assert "requires --seed" in capfd.readouterr().out

    def test_spawn_no_mux_session_exits_3(self, monkeypatch, capfd):
        monkeypatch.setattr(m, "_infer_worktree_id_from_cwd", lambda: "wtX")
        monkeypatch.setattr(sessions, "has_mux_session", lambda w: False)
        rc = m.cmd_handoff_cutover(_ns(seed="go"))
        assert rc == 3
        assert "not under mux" in capfd.readouterr().out

    def test_spawn_unresolvable_worktree_exits_2(self, monkeypatch, capfd):
        monkeypatch.setattr(m, "_infer_worktree_id_from_cwd", lambda: None)
        rc = m.cmd_handoff_cutover(_ns(seed="go"))
        assert rc == 2
        assert "could not resolve" in capfd.readouterr().out

    def test_spawn_dry_run_reports_plan_and_old_pane(
        self, monkeypatch, capfd, tmp_path,
    ):
        monkeypatch.setattr(m, "_infer_worktree_id_from_cwd", lambda: "wtY")
        monkeypatch.setattr(sessions, "has_mux_session", lambda w: True)
        monkeypatch.setattr(sessions, "_mux_bin", lambda mux=None: "tmux")
        monkeypatch.setattr(sessions, "mux_active_pane", lambda w: "%1")

        # Fake config + record + launch cmd
        yaml_path = tmp_path / "wtY.yaml"
        yaml_path.write_text("x")

        class _Cfg:
            pass

        monkeypatch.setattr(m.cfg, "load_config", lambda: _Cfg())
        monkeypatch.setattr(m.cfg, "tracking_dir", lambda: tmp_path)

        class _Rec:
            worktree_path = str(tmp_path / "w")

        monkeypatch.setattr(m.tracking, "load_record", lambda p: _Rec())
        monkeypatch.setattr(
            m, "_build_launch_cmd",
            lambda cfg_, args, wd: ["bash", "setup.sh", "--allow-all-tools"],
        )
        monkeypatch.setattr(m, "_build_env", lambda p, s: {})
        monkeypatch.setattr(m, "_repo_session_env", lambda c, w: {})

        # Guard: a real window must NOT be created in dry-run.
        monkeypatch.setattr(sessions, "mux_new_window",
                            lambda *a, **k: pytest.fail("should not spawn"))

        rc = m.cmd_handoff_cutover(_ns(seed="continue the work", dry_run=True))
        assert rc == 0
        out = json.loads(capfd.readouterr().out)
        assert out["dry_run"] is True
        assert out["old_pane"] == "%1"
        assert out["session"] == "wt-wtY"
        assert out["cmd"] == [
            "bash", "setup.sh", "--allow-all-tools",
        ]
        assert out["seed_len"] == len("continue the work")
        assert out["seed_delivery"] == "send-keys-confirmed"

    def test_spawn_success_opens_window(self, monkeypatch, capfd, tmp_path):
        monkeypatch.setattr(m, "_infer_worktree_id_from_cwd", lambda: "wtZ")
        monkeypatch.setattr(sessions, "has_mux_session", lambda w: True)
        monkeypatch.setattr(sessions, "_mux_bin", lambda mux=None: "tmux")
        monkeypatch.setattr(sessions, "mux_active_pane", lambda w: "%2")
        (tmp_path / "wtZ.yaml").write_text("x")
        monkeypatch.setattr(m.cfg, "load_config", lambda: object())
        monkeypatch.setattr(m.cfg, "tracking_dir", lambda: tmp_path)

        class _Rec:
            worktree_path = str(tmp_path / "w")

        monkeypatch.setattr(m.tracking, "load_record", lambda p: _Rec())
        monkeypatch.setattr(m, "_build_launch_cmd",
                            lambda c, a, wd: ["copilot"])
        monkeypatch.setattr(m, "_build_env", lambda p, s: {})
        monkeypatch.setattr(m, "_repo_session_env", lambda c, w: {})

        captured = {}

        def _fake_new_window(wt, wd, cmd, env, **k):
            captured["cmd"] = cmd
            return {"ok": True, "new_pane": "%5", "error": None}

        monkeypatch.setattr(sessions, "mux_new_window", _fake_new_window)
        confirmed = {}
        monkeypatch.setattr(
            sessions, "mux_seed_pane_and_confirm",
            lambda pane, seed, work_dir, prior_sessions, **k: confirmed.update(
                pane=pane,
                seed=seed,
                work_dir=work_dir,
                prior_sessions=prior_sessions,
            ) or {
                "ok": True,
                "pane": pane,
                "ready": True,
                "sent": True,
                "submitted": True,
                "reason": "accepted-turn",
                "session_id": "new-session",
            },
        )
        monkeypatch.setattr(
            sessions, "copilot_session_ids_for_cwd",
            lambda work_dir: {"old-session"},
        )

        rc = m.cmd_handoff_cutover(_ns(seed="resume the multi word work", old_pane="%2"))
        assert rc == 0
        out = json.loads(capfd.readouterr().out)
        assert out["ok"] is True
        assert out["old_pane"] == "%2"
        assert out["new_pane"] == "%5"
        assert out["seed_len"] == len("resume the multi word work")
        assert out["seeded"] is True
        assert captured["cmd"] == ["copilot"]
        assert confirmed == {
            "pane": "%5",
            "seed": "resume the multi word work",
            "work_dir": str(tmp_path / "w"),
            "prior_sessions": {"old-session"},
        }

    def test_spawn_direct_seed_not_accepted_closes_successor(
        self, monkeypatch, capfd, tmp_path,
    ):
        monkeypatch.setattr(m, "_infer_worktree_id_from_cwd", lambda: "wtZ")
        monkeypatch.setattr(sessions, "has_mux_session", lambda w: True)
        monkeypatch.setattr(sessions, "_mux_bin", lambda mux=None: "tmux")
        monkeypatch.setattr(sessions, "mux_active_pane", lambda w: "%2")
        monkeypatch.setattr(
            sessions, "copilot_session_ids_for_cwd",
            lambda work_dir: {"old-session"},
        )
        (tmp_path / "wtZ.yaml").write_text("x")
        monkeypatch.setattr(m.cfg, "load_config", lambda: object())
        monkeypatch.setattr(m.cfg, "tracking_dir", lambda: tmp_path)

        class _Rec:
            worktree_path = str(tmp_path / "w")

        monkeypatch.setattr(m.tracking, "load_record", lambda p: _Rec())
        monkeypatch.setattr(m, "_build_launch_cmd",
                            lambda c, a, wd: ["copilot"])
        monkeypatch.setattr(m, "_build_env", lambda p, s: {})
        monkeypatch.setattr(m, "_repo_session_env", lambda c, w: {})
        monkeypatch.setattr(
            sessions, "mux_new_window",
            lambda *a, **k: {"ok": True, "new_pane": "%5", "error": None},
        )
        monkeypatch.setattr(
            sessions, "mux_seed_pane_and_confirm",
            lambda *a, **k: {
                "ok": False,
                "pane": "%5",
                "ready": True,
                "sent": True,
                "submitted": False,
                "reason": "acceptance-timeout",
            },
        )
        retired = []
        monkeypatch.setattr(
            sessions, "mux_retire_pane",
            lambda pane, **k: retired.append(pane) or {
                "ok": True, "pane": pane, "gone": True, "method": "graceful",
            },
        )

        rc = m.cmd_handoff_cutover(_ns(seed="resume work", old_pane="%2"))

        assert rc == 5
        out = json.loads(capfd.readouterr().out)
        assert out["ok"] is False
        assert out["reason"] == "acceptance-timeout"
        assert out["successor_cleanup"]["ok"] is True
        assert retired == ["%5"]

    def test_spawn_seed_failure_closes_successor_and_reports_failure(
        self, monkeypatch, capfd, tmp_path,
    ):
        monkeypatch.setattr(m, "_infer_worktree_id_from_cwd", lambda: "wtZ")
        monkeypatch.setattr(sessions, "has_mux_session", lambda w: True)
        monkeypatch.setattr(sessions, "_mux_bin", lambda mux=None: "psmux")
        monkeypatch.setattr(sessions, "mux_active_pane", lambda w: "%2")
        (tmp_path / "wtZ.yaml").write_text("x")
        monkeypatch.setattr(m.cfg, "load_config", lambda: object())
        monkeypatch.setattr(m.cfg, "tracking_dir", lambda: tmp_path)

        class _Rec:
            worktree_path = str(tmp_path / "w")

        monkeypatch.setattr(m.tracking, "load_record", lambda p: _Rec())
        monkeypatch.setattr(m, "_build_launch_cmd",
                            lambda c, a, wd: ["copilot"])
        monkeypatch.setattr(m, "_build_env", lambda p, s: {})
        monkeypatch.setattr(m, "_repo_session_env", lambda c, w: {})
        monkeypatch.setattr(
            sessions, "mux_new_window",
            lambda *a, **k: {"ok": True, "new_pane": "%5", "error": None},
        )
        monkeypatch.setattr(
            sessions, "mux_seed_pane",
            lambda *a, **k: {
                "ok": False,
                "pane": "%5",
                "ready": False,
                "sent": False,
                "submitted": False,
                "reason": "not-ready-timeout",
            },
        )

        retired = []
        monkeypatch.setattr(
            sessions, "mux_retire_pane",
            lambda pane, **k: retired.append(pane) or {
                "ok": True,
                "pane": pane,
                "gone": True,
                "method": "graceful",
            },
        )

        rc = m.cmd_handoff_cutover(_ns(seed="resume work", old_pane="%2"))

        assert rc != 0
        out = json.loads(capfd.readouterr().out)
        assert out["ok"] is False
        assert out["old_pane"] == "%2"
        assert out["new_pane"] == "%5"
        assert out["seeded"] is False
        assert out["seed_ready"] is False
        assert out["reason"] == "not-ready-timeout"
        assert out["successor_cleanup"]["ok"] is True
        assert retired == ["%5"]


@pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
def test_tmux_cutover_accepts_seed_before_old_pane_retirement(
    monkeypatch, capfd, tmp_path,
):
    """Exercise the real tmux boundary with a disposable Copilot-shaped process."""
    worktree_id = f"handoff-smoke-{uuid.uuid4().hex[:8]}"
    session_name = f"wt-{worktree_id}"
    work_dir = tmp_path / "worktree"
    fake_home = tmp_path / "home"
    work_dir.mkdir()
    fake_home.mkdir()

    successor = tmp_path / "fake-copilot.py"
    successor.write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys
            import time
            from pathlib import Path

            work_dir = sys.argv[1]
            session_dir = (
                Path(os.environ["HOME"])
                / ".copilot"
                / "session-state"
                / "successor-session"
            )
            session_dir.mkdir(parents=True)
            (session_dir / "workspace.yaml").write_text(
                f"cwd: {json.dumps(work_dir)}\\n",
                encoding="utf-8",
            )

            print("╻", flush=True)
            print("┃", flush=True)
            print("╹", flush=True)
            seed = sys.stdin.readline().rstrip("\\n")
            (session_dir / "events.jsonl").write_text(
                json.dumps(
                    {"type": "user.message", "data": {"content": seed}}
                ) + "\\n",
                encoding="utf-8",
            )
            time.sleep(30)
            """
        ),
        encoding="utf-8",
    )

    old = subprocess.run(
        [
            "tmux", "new-session", "-d", "-s", session_name,
            "-P", "-F", "#{pane_id}", "sleep", "30",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    old_pane = old.stdout.strip()

    try:
        tracking_dir = tmp_path / "tracking"
        tracking_dir.mkdir()
        (tracking_dir / f"{worktree_id}.yaml").write_text("x", encoding="utf-8")

        class _Rec:
            worktree_path = str(work_dir)

        monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
        monkeypatch.setattr(m, "_resolve_worktree_id", lambda raw: raw)
        monkeypatch.setattr(m.cfg, "load_config", lambda: object())
        monkeypatch.setattr(m.cfg, "tracking_dir", lambda: tracking_dir)
        monkeypatch.setattr(m.tracking, "load_record", lambda path: _Rec())
        monkeypatch.setattr(
            m,
            "_build_launch_cmd",
            lambda config, args, cwd: [sys.executable, str(successor), cwd],
        )
        monkeypatch.setattr(m, "_build_env", lambda profile, env: {"HOME": str(fake_home)})
        monkeypatch.setattr(m, "_repo_session_env", lambda config, cwd: {})

        seed = "continue exact handoff smoke"
        rc = m.cmd_handoff_cutover(
            _ns(seed=seed, worktree_id=worktree_id, old_pane=old_pane)
        )

        assert rc == 0
        result = json.loads(capfd.readouterr().out)
        assert result["seeded"] is True
        assert result["old_pane"] == old_pane
        assert result["new_pane"] != old_pane

        event = json.loads(
            (
                fake_home
                / ".copilot"
                / "session-state"
                / "successor-session"
                / "events.jsonl"
            ).read_text(encoding="utf-8")
        )
        assert event == {"type": "user.message", "data": {"content": seed}}
        assert sessions._mux_pane_alive(old_pane, "tmux") is True
        assert sessions._mux_pane_alive(result["new_pane"], "tmux") is True

        retirement = sessions.mux_retire_pane(
            old_pane,
            mux="tmux",
            ctrl_c_gap=0,
            poll_interval=0.05,
            settle_timeout=2,
        )
        assert retirement["gone"] is True
        assert sessions._mux_pane_alive(result["new_pane"], "tmux") is True
    finally:
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={session_name}"],
            capture_output=True,
            check=False,
        )
