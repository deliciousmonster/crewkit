"""install and uninstall on a scratch HOME: what they make, what they leave, and what they name."""
import json
import os
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from crewkit import install as ik
from installcase import ODD, PYPROJECT, THEIRS, Base


class Install(Base):
    def test_a_launcher_runs_the_console_script_and_uninstall_removes_it(self):
        self.run_install()
        launcher = self.home / ".local/bin/democrew"
        self.assertTrue(os.access(launcher, os.X_OK))
        line = f'exec uv run --quiet --project {shlex.quote(str(self.root))} democrew "$@"'
        self.assertIn(line, launcher.read_text())
        ik.uninstall(self.spec)
        self.assertFalse(launcher.exists())

    def test_a_launcher_hands_uv_a_clone_path_with_quotes_dollars_and_backticks_as_written(self):
        root = self.clone_at(ODD)
        self.run_install()
        r = subprocess.run([self.home / ".local/bin/democrew", "a b", "$HOME"], capture_output=True, text=True,
                           env=self.argv_printer("uv"))
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual([a[1:-1] for a in r.stdout.splitlines()],
                         ["run", "--quiet", "--project", str(root), "democrew", "a b", "$HOME"])

    def test_each_hook_hands_a_shell_a_clone_path_with_a_space_and_a_dollar_as_written(self):
        root = self.clone_at(ODD)
        self.run_install()
        hooks, env = json.loads((self.claude / "settings.json").read_text())["hooks"], self.argv_printer("guard")
        for event, said in (("SessionStart", f"{root}\n"), ("PreToolUse", f"[{root}]\n")):  # echo, then guard
            command = hooks[event][0]["hooks"][0]["command"]
            r = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, env=env)
            self.assertEqual((r.returncode, r.stderr, r.stdout), (0, "", said), command)

    def test_the_launcher_names_the_command_the_owner_types(self):
        (self.root / "pyproject.toml").write_text(PYPROJECT.replace('bin = ["democrew"]', 'bin = ["demo"]'))
        self.spec = ik.load_spec(self.root)
        self.run_install()
        text = (self.home / ".local/bin/demo").read_text()
        self.assertIn("`demo install`", text)
        self.assertIn("`demo uninstall`", text)
        self.assertNotIn("democrew install", text)

    def test_a_launcher_never_replaces_a_file_it_did_not_write(self):
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".local/bin/democrew").write_text("someone else's\n")
        with self.assertRaises(ik.InstallError):
            self.run_install()

    def test_doctor_warns_when_the_launcher_folder_is_off_path(self):
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            _, lines = ik.doctor(self.spec, which=lambda b: "/bin/" + b)
        self.assertTrue(any(l.startswith("[warn]") and ".local/bin on PATH" in l for l in lines))

    def test_fresh_install_links_writes_path_and_records(self):
        said = self.run_install()
        self.assert_linked()
        self.assertEqual((self.home / ".config/democrew/crew-path").read_text().strip(), str(self.root))
        self.assertEqual((self.claude / "agents/a.md").read_text(), f"see {self.root}\n")
        settings = json.loads((self.claude / "settings.json").read_text())
        self.assertEqual(settings["hooks"]["SessionStart"][0]["hooks"][0]["command"], f"echo {self.root}")
        self.assertIn("Bash(democrew *)", settings["permissions"]["allow"])
        man = json.loads((self.home / ".config/democrew/install.json").read_text())
        self.assertEqual(len(man["links"]), 2)
        self.assertTrue(any("restart Claude Code" in l for l in said))
        self.assertTrue(any(str(self.root) in l for l in said))

    def test_second_run_changes_nothing(self):
        self.run_install()
        snap = lambda: {str(p): (p.read_text() if p.is_file() else os.readlink(p) if p.is_symlink() else "") for p in sorted(self.home.rglob("*"))}
        before = snap()
        self.run_install()
        self.assertEqual(snap(), before)

    def test_conflicting_existing_path_stops(self):
        (self.claude / "skills/demo").mkdir(parents=True)
        with self.assertRaises(ik.InstallError) as c:
            self.run_install()
        self.assertIn("--force", str(c.exception))
        self.assertTrue((self.claude / "skills/demo").is_dir() and not (self.claude / "skills/demo").is_symlink())

    def test_link_to_elsewhere_stops_then_force_moves_aside(self):
        other = self.tmp / "other"
        other.mkdir()
        (self.claude / "skills").mkdir(parents=True)
        (self.claude / "skills/demo").symlink_to(other)
        with self.assertRaises(ik.InstallError):
            self.run_install()
        self.run_install(force=True)
        self.assertEqual(Path(os.path.realpath(self.claude / "skills/demo")), (self.root / "skills/demo").resolve())
        asides = list((self.claude / "skills").glob("demo.bak-*"))
        self.assertEqual(len(asides), 1)
        self.assertEqual(Path(os.path.realpath(asides[0])), other.resolve())

    def test_project_scope_writes_under_target(self):
        target = self.tmp / "proj"
        target.mkdir()
        self.run_install(scope="project", target=target)
        self.assertTrue((target / ".claude/skills/demo").is_symlink())
        self.assertFalse((self.claude / "skills").exists())
        with self.assertRaises(ik.InstallError):
            self.run_install(scope="project")

    def test_merge_keeps_existing_settings_and_backs_up(self):
        self.claude.mkdir(parents=True)
        (self.claude / "settings.json").write_text(json.dumps({"model": "x", "permissions": {"allow": ["Read"]}}))
        self.run_install()
        s = json.loads((self.claude / "settings.json").read_text())
        self.assertEqual(s["model"], "x")
        self.assertEqual(s["permissions"]["allow"], ["Read", "Bash(democrew *)"])
        self.assertEqual(len(list(self.claude.glob("settings.json.bak-*"))), 1)

    def test_invalid_settings_json_stops_and_is_left_alone(self):
        self.claude.mkdir(parents=True)
        (self.claude / "settings.json").write_text("{not json")
        with self.assertRaises(ik.InstallError) as c:
            self.run_install()
        self.assertIn("not valid JSON", str(c.exception))
        self.assertEqual((self.claude / "settings.json").read_text(), "{not json")

    def test_os_error_is_a_message_not_a_traceback(self):
        with mock.patch.object(ik, "install", side_effect=PermissionError(13, "Permission denied", "/x")):
            out = self.failed_install()
        self.assertIn("Permission denied", out)
        self.assertIn("owner/democrew", out)

    def test_missing_skill_md_is_a_clear_error(self):
        (self.root / "skills/demo/SKILL.md").unlink()
        with self.assertRaises(ik.InstallError) as c:
            self.run_install()
        self.assertIn("SKILL.md", str(c.exception))


class Uninstall(Base):
    def test_removes_only_managed_paths(self):
        self.claude.mkdir(parents=True)
        (self.claude / "settings.json").write_text(json.dumps({"model": "x", "permissions": {"allow": ["Read"]}}))
        (self.claude / "skills").mkdir()
        (self.claude / "skills/mine").mkdir()
        (self.claude / "agents").mkdir()
        (self.claude / "agents/b.md").write_text("mine")
        self.run_install()
        ik.uninstall(self.spec)
        self.assertFalse((self.claude / "skills/demo").exists() or (self.claude / "skills/demo").is_symlink())
        self.assertTrue((self.claude / "skills/mine").is_dir())
        self.assertEqual((self.claude / "agents/b.md").read_text(), "mine")
        self.assertFalse((self.claude / "agents/a.md").exists())
        s = json.loads((self.claude / "settings.json").read_text())
        self.assertEqual(s, {"model": "x", "permissions": {"allow": ["Read"]}})
        self.assertFalse((self.home / ".config/democrew/install.json").exists())
        self.assertTrue((self.root / "skills/demo/SKILL.md").exists())

    def test_an_event_hook_is_installed_with_its_matcher_and_removed_alone(self):
        self.write_settings({"hooks": {"PreToolUse": [THEIRS]}})
        self.run_install()
        self.run_install()
        pre = json.loads((self.claude / "settings.json").read_text())["hooks"]["PreToolUse"]
        self.assertEqual(pre, [THEIRS, {"matcher": "Bash", "hooks": [{"type": "command", "command": f"guard {self.root}"}]}])
        ik.uninstall(self.spec)
        self.assertEqual(json.loads((self.claude / "settings.json").read_text()), {"hooks": {"PreToolUse": [THEIRS]}})

    def test_an_install_recorded_before_hook_events_still_uninstalls(self):
        self.run_install()
        path = self.home / ".config/democrew/install.json"
        man = json.loads(path.read_text())
        rec = man["settings"][0]
        rec["hooks"] = [h["group"] if h["event"] == "SessionStart" else h for h in rec["hooks"]]
        path.write_text(json.dumps(man))
        ik.uninstall(self.spec)
        self.assertNotIn("hooks", json.loads((self.claude / "settings.json").read_text()))

    def test_a_hook_without_a_command_is_refused(self):
        (self.root / "pyproject.toml").write_text(PYPROJECT.replace('command = "guard {crew_path}"', ""))
        with self.assertRaises(ik.InstallError):
            ik.load_spec(self.root)

    def test_keeps_settings_entries_the_user_already_had(self):
        self.claude.mkdir(parents=True)
        hook = {"hooks": [{"type": "command", "command": f"echo {self.root}"}]}
        guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": f"guard {self.root}"}]}
        mine = {"permissions": {"allow": ["Bash(democrew *)"]}, "hooks": {"SessionStart": [hook], "PreToolUse": [guard]}}
        (self.claude / "settings.json").write_text(json.dumps(mine))
        self.run_install()
        man = json.loads((self.home / ".config/democrew/install.json").read_text())
        self.assertEqual(man["settings"], [])
        ik.uninstall(self.spec)
        self.assertEqual(json.loads((self.claude / "settings.json").read_text()), mine)

    def test_leaves_a_repointed_link_and_an_edited_agent(self):
        self.run_install()
        other = self.tmp / "other"
        other.mkdir()
        (self.claude / "skills/demo").unlink()
        (self.claude / "skills/demo").symlink_to(other)
        (self.claude / "agents/a.md").write_text("edited\n")
        said = ik.uninstall(self.spec)
        self.assertEqual(Path(os.path.realpath(self.claude / "skills/demo")), other.resolve())
        self.assertEqual((self.claude / "agents/a.md").read_text(), "edited\n")
        self.assertFalse((self.claude / "skills/demo-extra").is_symlink())
        self.assertTrue(any(l.startswith(f"left {self.claude / 'skills/demo'}") for l in said))
        self.assertTrue(any(l.startswith(f"left {self.claude / 'agents/a.md'}") for l in said))

    def test_names_an_agent_and_a_launcher_that_became_links_to_nothing(self):
        self.run_install()
        agent, launcher = self.claude / "agents/a.md", self.home / ".local/bin/democrew"
        for p in (agent, launcher):
            p.unlink()
            p.symlink_to(self.tmp / "gone" / p.name)
        said = ik.uninstall(self.spec)
        for p in (agent, launcher):
            self.assertTrue(p.is_symlink())
            self.assertTrue(any(l.startswith(f"left {p}") for l in said), said)

    def test_names_and_keeps_an_agent_file_that_is_no_longer_text(self):
        self.run_install()
        agent = self.claude / "agents/a.md"
        agent.write_bytes(b"\xff\xfe")
        said = ik.uninstall(self.spec)
        self.assertEqual(agent.read_bytes(), b"\xff\xfe")
        self.assertTrue(any(l.startswith(f"left {agent}") for l in said), said)
        self.assertFalse((self.home / ".config/democrew/install.json").exists())

    def test_names_what_force_moved_aside(self):
        (self.claude / "skills/demo").mkdir(parents=True)
        self.run_install(force=True)
        said = ik.uninstall(self.spec)
        aside = next((self.claude / "skills").glob("demo.bak-*"))
        self.assertTrue(aside.is_dir())
        self.assertTrue(any(l.startswith(f"left {aside}") for l in said))

    def test_reinstall_after_an_agent_edit_needs_force(self):
        self.run_install()
        (self.claude / "agents/a.md").write_text("edited\n")
        with self.assertRaises(ik.InstallError):
            self.run_install()
        self.assertEqual((self.claude / "agents/a.md").read_text(), "edited\n")

    def test_without_a_record_it_removes_nothing(self):
        (self.claude / "skills").mkdir(parents=True)
        (self.claude / "skills/demo").symlink_to(self.root / "skills/demo")
        self.assertIn("nothing recorded", ik.uninstall(self.spec)[0])
        self.assertTrue((self.claude / "skills/demo").is_symlink())


class LightCore(unittest.TestCase):
    def test_imports_work_with_crewai_blocked(self):
        code = (
            "import sys\n"
            "class B:\n"
            "    def find_spec(self, n, p=None, t=None):\n"
            "        if n.split('.')[0] in ('crewai', 'crewai_tools'): raise ImportError('blocked ' + n)\n"
            "sys.meta_path.insert(0, B())\n"
            "import crewkit, crewkit.workspace, crewkit.migrate, crewkit.install, crewkit.spec\n"
            "import crewkit.builder, crewkit.cli, crewkit.llm\n"
            "print('ok')\n"
        )
        src = str(Path(__file__).resolve().parents[1] / "src")
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env={**os.environ, "PYTHONPATH": src})
        self.assertEqual(r.stdout.strip(), "ok", r.stderr)


if __name__ == "__main__":
    unittest.main()
