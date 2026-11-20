"""crewkit.stopline run as Claude Code runs it: a subprocess fed a Stop event, through a crew's interpreter."""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import usagecase as uc

from crewkit import install, spec, stopline

LAUNCH = """\
import sys
seen = []
def audit(event, args):
    if event in ("open", "os.scandir", "os.listdir") and args and args[0] is not None:
        seen.append(str(args[0]))
sys.addaudithook(audit)
from crewkit import stopline
stopline.main(["developer"])
sys.stdout.write("\\nAUDIT " + __import__("json").dumps(seen))
"""


class Proc(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.home = self.d / "home"
        self.home.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in ("XDG_CACHE_HOME", "VIRTUAL_ENV")}
        self.env["HOME"] = str(self.home)
        self.crew = self.d / "crew"
        uc.wrapper(self.crew, sys.executable)

    def hook(self, stdin, skill="developer", cwd=None, command=None, timeout=10):
        cmd = command or stopline.COMMAND.format(crew_path=self.crew, skill=skill)
        return subprocess.run(["sh", "-c", cmd], input=stdin, capture_output=True, text=True, env=self.env,
                              cwd=cwd or self.d, timeout=timeout, check=False)

    def assert_silent(self, r, stderr=""):
        self.assertEqual((r.returncode, r.stdout), (0, ""))
        if stderr is not None:
            self.assertEqual(r.stderr, stderr)

    def state_files(self):
        root = self.home / ".cache" / "crewkit" / "stopline"
        return sorted(root.glob("*.json")) if root.exists() else []


class Silence(Proc):
    def test_p1_p7_bad_input_is_silent_with_exit_zero(self):
        for stdin in ("garbage", "{}", uc.event(self.d / "missing.jsonl"), json.dumps({"hook_event_name": "Stop"})):
            r = subprocess.run([sys.executable, "-I", "-m", "crewkit.stopline", "developer"], input=stdin,
                               capture_output=True, text=True, env=self.env, timeout=10, check=False)
            self.assert_silent(r)

    def test_p4_stdin_that_never_closes_ends_inside_the_alarm(self):
        t = time.monotonic()
        p = subprocess.Popen([sys.executable, "-I", "-m", "crewkit.stopline", "developer"], stdin=subprocess.PIPE,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=self.env)
        try:
            code = p.wait(timeout=10)
        finally:
            p.stdin.close()
            if p.poll() is None:
                p.kill()
                p.wait()
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - t, 3.5)

    def test_p9_a_crew_without_a_venv_exits_zero(self):
        self.crew = self.d / "bare"
        self.crew.mkdir()
        self.assert_silent(self.hook(uc.event(uc.build(self.d))), stderr=None)  # sh names the missing file

    def test_p10_a_fifo_transcript_is_silent_at_once(self):
        fifo = self.d / "f.jsonl"
        os.mkfifo(fifo)
        t = time.monotonic()
        self.assert_silent(self.hook(uc.event(fifo)))
        self.assertLess(time.monotonic() - t, 1.0)

    def test_p12_check_through_the_wrapper_and_through_a_bare_interpreter(self):
        r = subprocess.run([str(self.crew / ".venv/bin/python"), "-I", "-m", "crewkit.stopline", "--check"],
                           capture_output=True, text=True, env=self.env, timeout=10, check=False)
        self.assertEqual((r.returncode, r.stdout), (0, "stopline ok\n"))
        uc.wrapper(self.crew, sys.executable, "-S")  # no site-packages: crewkit cannot import
        self.assert_silent(self.hook("", command=f"{self.crew}/.venv/bin/python -I -m crewkit.stopline --check 2>/dev/null || true"))
        self.assert_silent(self.hook(uc.event(uc.build(self.d))), stderr=None)  # python names the missing module


class Line(Proc):
    def test_p2_p6_the_command_prints_one_json_object_and_ignores_the_cwd(self):
        cwd = self.d / "work"
        (cwd / "crewkit").mkdir(parents=True)
        marker = self.d / "planted"
        (cwd / "crewkit" / "__init__.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
        r = self.hook(uc.event(uc.build(self.d)), cwd=cwd)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(json.loads(r.stdout), {"systemMessage": uc.LINE})
        self.assertEqual(r.stdout.count("\n"), 1)
        self.assertFalse(marker.exists())

    def test_p3_no_prompt_tool_or_reply_text_leaves_the_hook(self):
        mark = "MARK-7f3a"
        rows = uc.lead_rows() + [uc.typed(f"say {mark}")] + uc.call("mT", 5, tools=["tuT"], text=f"reply {mark}")
        rows[-1]["message"]["content"].append({"type": "tool_use", "id": "x", "name": "Bash", "input": {"command": mark}})
        rows.append({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": mark}]}})
        s = self.d / "sess.jsonl"
        s.write_text(uc.dump(rows))
        uc.write_agents(s)
        r = self.hook(uc.event(s, last=f"reply {mark}"))
        self.assertNotIn(mark, r.stdout + r.stderr)
        self.assertTrue(r.stdout)
        for f in self.state_files():
            self.assertNotIn(mark.encode(), f.read_bytes())

    def test_p5_state_under_home_or_xdg_with_private_modes(self):
        s = uc.build(self.d)
        self.hook(uc.event(s))
        [f] = self.state_files()
        self.assertEqual(f.parent, self.home / ".cache" / "crewkit" / "stopline")
        self.assertEqual((stat.S_IMODE(f.stat().st_mode), stat.S_IMODE(f.parent.stat().st_mode)), (0o600, 0o700))
        self.env["XDG_CACHE_HOME"] = str(self.d / "xdg")
        self.hook(uc.event(s))
        self.assertTrue((self.d / "xdg" / "crewkit" / "stopline" / "sess-1-developer.json").is_file())

    def test_p8_others_come_from_every_manifest_less_the_own_skill(self):
        cfg = self.home / ".config"
        mans = {"c1": {"links": [{"path": str(self.home / ".claude/skills/employ"), "target": "x"}]},
                "c2": {"links": [{"path": str(self.d / "proj/.claude/skills/scout"), "target": "x"}]},
                "c3": {"links": [{"path": str(self.home / ".claude/skills/developer"), "target": "x"}]}}
        for crew, man in mans.items():
            (cfg / crew).mkdir(parents=True)
            (cfg / crew / "install.json").write_text(json.dumps(man))
        (cfg / "c4").mkdir()
        (cfg / "c4" / "install.json").write_text("{not json")
        with mock.patch.dict(os.environ, {"HOME": str(self.home)}):
            self.assertEqual(spec.installed_skills(), {"employ", "scout", "developer"})
            said = []
            install._read_others("c0", said)
            self.assertEqual(said, [f"ignored {cfg / 'c4' / 'install.json'}: it is not a JSON object"])
        r = self.hook(uc.event(uc.build(self.d)))
        self.assertEqual(json.loads(r.stdout)["systemMessage"], uc.LINE)

    def test_p11_no_agent_path_is_touched_before_a_cycle(self):
        quiet = self.d / "quiet.jsonl"
        quiet.write_text(uc.dump([uc.typed("hello")] + uc.call("m1", 10)))
        uc.write_agents(quiet)
        busy = uc.build(self.d)
        for s, touched in ((quiet, False), (busy, True)):
            r = subprocess.run([str(self.crew / ".venv/bin/python"), "-I", "-c", LAUNCH], input=uc.event(s),
                               capture_output=True, text=True, env=self.env, timeout=10, check=False)
            paths = json.loads(r.stdout.split("AUDIT ", 1)[1])
            sub = str(s.with_suffix("") / "subagents")
            self.assertEqual(any(p.startswith(sub) for p in paths), touched, paths)


if __name__ == "__main__":
    unittest.main()
