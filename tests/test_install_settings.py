"""settings.json: what install merges and prunes, what uninstall takes out, and the shapes that stop both."""
import json
import shutil
import unittest

from crewkit import install as ik
from installcase import PYPROJECT, THEIRS, Base


class Prune(Base):
    """An install removes what an earlier one recorded and the crew no longer declares."""

    def declare(self, *edits):
        text = PYPROJECT
        for old, new in edits:
            self.assertIn(old, text)
            text = text.replace(old, new)
        (self.root / "pyproject.toml").write_text(text)
        self.spec = ik.load_spec(self.root)

    def settings(self):
        return json.loads((self.claude / "settings.json").read_text())

    def guards(self):
        return [g for g in self.settings()["hooks"]["PreToolUse"] if g["hooks"][0]["command"] == f"guard {self.root}"]

    def test_a_changed_matcher_leaves_one_group_not_two(self):
        self.run_install()
        self.declare(('matcher = "Bash"', 'matcher = "Bash|Edit"'))
        self.run_install()
        self.assertEqual([g["matcher"] for g in self.guards()], ["Bash|Edit"])
        man = json.loads((self.home / ".config/democrew/install.json").read_text())
        self.assertEqual([h["group"]["matcher"] for h in man["settings"][0]["hooks"] if h["event"] == "PreToolUse"],
                         ["Bash|Edit"])
        ik.uninstall(self.spec)
        self.assertEqual(self.settings(), {})

    def test_install_twice_is_byte_identical(self):
        self.run_install()
        self.declare(('matcher = "Bash"', 'matcher = "Bash|Edit"'))
        self.run_install()
        snap = lambda: {str(p): p.read_bytes() for p in sorted(self.claude.rglob("*")) if p.is_file()}
        before = snap()
        self.run_install()
        self.assertEqual(snap(), before)

    def test_a_foreign_group_on_the_same_event_and_matcher_is_untouched(self):
        self.write_settings({"hooks": {"PreToolUse": [THEIRS]}})
        self.run_install()
        self.declare(('matcher = "Bash"', 'matcher = "Bash|Edit"'))
        self.run_install()
        pre = self.settings()["hooks"]["PreToolUse"]
        self.assertEqual(pre[0], THEIRS)
        self.assertEqual([g["matcher"] for g in self.guards()], ["Bash|Edit"])
        ik.uninstall(self.spec)
        self.assertEqual(self.settings(), {"hooks": {"PreToolUse": [THEIRS]}})

    def test_a_removed_permission_is_pruned_and_one_the_user_had_is_not(self):
        self.claude.mkdir(parents=True)
        (self.claude / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Read", "Bash(mine *)"]}}))
        self.declare(('permissions = ["Bash(democrew *)"]', 'permissions = ["Bash(democrew *)", "Bash(old *)", "Bash(mine *)"]'))
        self.run_install()
        self.assertEqual(self.settings()["permissions"]["allow"],
                         ["Read", "Bash(mine *)", "Bash(democrew *)", "Bash(old *)"])
        self.declare()
        self.run_install()
        self.assertEqual(self.settings()["permissions"]["allow"], ["Read", "Bash(mine *)", "Bash(democrew *)"])
        ik.uninstall(self.spec)
        self.assertEqual(self.settings(), {"permissions": {"allow": ["Read", "Bash(mine *)"]}})

    def test_a_crew_that_declares_nothing_any_more_is_cleaned_out(self):
        self.run_install()
        (self.root / "pyproject.toml").write_text('[project]\nname = "democrew"\nversion = "0.1.0"\n\n'
                                                  '[tool.crewkit]\nskills = ["demo"]\nrepo = "owner/democrew"\n')
        self.spec = ik.load_spec(self.root)
        self.run_install()
        self.assertEqual(self.settings(), {})

    def test_a_pre_0_6_0_bare_session_start_record_still_works(self):
        path = self.home / ".config/democrew/install.json"

        def bare():  # the record as 0.5.x wrote it: the SessionStart group alone, with no event
            man = json.loads(path.read_text())
            man["settings"][0]["hooks"] = [h["group"] if h["event"] == "SessionStart" else h
                                           for h in man["settings"][0]["hooks"]]
            path.write_text(json.dumps(man))

        self.run_install()
        bare()
        before = (self.claude / "settings.json").read_bytes()
        self.run_install()  # still declared: kept once, not added again
        self.assertEqual((self.claude / "settings.json").read_bytes(), before)
        bare()
        self.declare(('hook = "echo {crew_path}"', ""))  # no longer declared: the bare group is pruned
        self.run_install()
        self.assertNotIn("SessionStart", self.settings()["hooks"])
        self.assertEqual([g["matcher"] for g in self.guards()], ["Bash"])

    def crew(self, name, hooks=(), perms=()):
        """A second crew's clone, with the hooks (event, matcher, command) and permissions it declares."""
        root = self.tmp / "clone" / name
        (root / "skills" / name).mkdir(parents=True, exist_ok=True)
        (root / "skills" / name / "SKILL.md").write_text("# s\n")
        text = f'[project]\nname = "{name}"\nversion = "0.1.0"\n\n[tool.crewkit]\nskills = ["{name}"]\nrepo = "o/{name}"\n'
        if perms:
            text += "permissions = [" + ", ".join(json.dumps(x) for x in perms) + "]\n"
        for event, matcher, command in hooks:
            text += f'\n[[tool.crewkit.hooks]]\nevent = "{event}"\nmatcher = "{matcher}"\ncommand = "{command}"\n'
        (root / "pyproject.toml").write_text(text)
        return ik.load_spec(root)

    def install_crew(self, spec):
        return ik.install(spec, which=lambda b: "/bin/" + b)

    def test_a_record_without_a_permissions_key_installs(self):
        self.run_install()
        path = self.home / ".config/democrew/install.json"
        man = json.loads(path.read_text())
        del man["settings"][0]["permissions"]
        path.write_text(json.dumps(man))
        self.run_install()
        self.assertEqual(self.settings()["permissions"]["allow"], ["Bash(democrew *)"])
        self.assertEqual(json.loads(path.read_text())["settings"][0]["permissions"], [])  # it adds nothing new

    def test_a_group_and_permission_two_crews_declare_stay_when_one_stops(self):
        shared = [("PreToolUse", "Bash", "shared-guard")]
        alpha = self.crew("alpha", shared, ["Bash(shared *)"])
        beta = self.crew("beta", shared, ["Bash(shared *)"])
        self.install_crew(alpha)
        self.install_crew(beta)
        alpha = self.crew("alpha", [("PreToolUse", "Edit", "other")])  # alpha stops declaring the shared pair
        self.install_crew(alpha)
        pre = [g["hooks"][0]["command"] for g in self.settings()["hooks"]["PreToolUse"]]
        self.assertEqual(pre, ["shared-guard", "other"])
        self.assertEqual(self.settings()["permissions"]["allow"], ["Bash(shared *)"])
        ik.uninstall("alpha")  # alpha's uninstall leaves what beta still records
        self.assertEqual([g["hooks"][0]["command"] for g in self.settings()["hooks"]["PreToolUse"]], ["shared-guard"])
        self.assertEqual(self.settings()["permissions"]["allow"], ["Bash(shared *)"])
        ik.uninstall("beta")  # and the last crew to record it takes it out
        self.assertEqual(self.settings(), {})

    def test_an_entry_the_other_crew_installed_first_is_recorded_by_the_second_crew_too(self):
        shared = [("PreToolUse", "Bash", "shared-guard")]
        self.install_crew(self.crew("alpha", shared, ["Bash(shared *)"]))  # as 0.10.2 left it: beta records nothing
        beta = self.crew("beta", shared, ["Bash(shared *)"])
        self.install_crew(beta)
        man = json.loads((self.home / ".config/beta/install.json").read_text())
        self.assertEqual(man["settings"][0]["permissions"], ["Bash(shared *)"])
        self.assertEqual([h["group"]["hooks"][0]["command"] for h in man["settings"][0]["hooks"]], ["shared-guard"])

    def test_a_crew_that_declares_nothing_fails_over_a_corrupt_settings_json_before_any_change(self):
        # It recorded settings entries once, so it still reads the file; the read comes before the first link.
        self.run_install()
        (self.root / "pyproject.toml").write_text('[project]\nname = "democrew"\nversion = "0.1.0"\n\n'
                                                  '[tool.crewkit]\nskills = ["demo"]\nrepo = "owner/democrew"\n')
        self.spec = ik.load_spec(self.root)
        (self.claude / "settings.json").write_text("{ not json")
        (self.claude / "skills/demo").unlink()  # a link the install would put back
        before = self.snap()
        with self.assertRaises(ik.InstallError):
            self.run_install()
        self.assertEqual(self.snap(), before)
        self.assertEqual((self.claude / "settings.json").read_text(), "{ not json")


class ReadFirst(Base):
    """install and uninstall read settings.json and the other crews' manifests before they change anything."""

    def fresh(self):
        shutil.rmtree(self.home)
        self.home.mkdir()

    def other(self, man):
        """Another crew's install.json holding `man` (text as given, anything else as JSON)."""
        path = self.home / ".config/other/install.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(man if isinstance(man, str) else json.dumps(man))
        return path

    def shapes(self):
        """Malformed manifests: those a refuter wrote against 0.10.3, and a few more of the same kind."""
        f = str(self.claude / "settings.json")
        rec = lambda **kw: {"settings": [{"file": f, "hooks": [], "permissions": [], **kw}]}
        return {
            "settings null": {"settings": None},
            "settings a string": {"settings": "x"},
            "settings holds a number": {"settings": [1]},
            "hooks null": rec(hooks=None),
            "permissions null": rec(permissions=None),
            "hook with no group": rec(hooks=[{"event": "PreToolUse"}]),
            "hook with a null group": rec(hooks=[{"event": "PreToolUse", "group": None}]),
            "hook that is a number": rec(hooks=[5]),
            "a list": [1],
            "null": None,
            "not json": "{ not json",
        }

    def test_a_fresh_install_over_a_corrupt_settings_json_leaves_nothing_behind(self):
        self.write_settings("{ not json")
        before = self.snap()
        with self.assertRaises(ik.InstallError) as c:
            self.run_install()
        self.assertIn("nothing installed", str(c.exception))
        self.assertFalse((self.claude / "skills").exists())
        self.assertFalse((self.claude / "agents/a.md").exists())
        self.assertFalse((self.home / ".local/bin/democrew").exists())
        self.assertFalse((self.home / ".config/democrew/crew-path").exists())
        self.assertFalse((self.home / ".config/democrew/install.json").exists())
        self.assertEqual(self.snap(), before)

    def test_a_reinstall_over_a_corrupt_settings_json_changes_nothing(self):
        self.run_install()
        (self.root / "agents/a.md").write_text("new {crew_path}\n")  # an agent the install would rewrite
        (self.claude / "skills/demo-extra").unlink()  # a link it would put back
        self.write_settings("{ not json")
        before = self.snap()
        with self.assertRaises(ik.InstallError) as c:
            self.run_install()
        self.assertIn("nothing installed", str(c.exception))
        self.assertEqual(self.snap(), before)

    def test_uninstall_over_a_corrupt_settings_json_removes_nothing(self):
        self.run_install()
        self.write_settings("{ not json")
        before = self.snap()
        with self.assertRaises(ik.InstallError) as c:
            ik.uninstall(self.spec)
        self.assertIn("nothing removed", str(c.exception))
        self.assertEqual(self.snap(), before)
        (self.claude / "settings.json").write_text("{}")  # fixed by hand, it finishes
        ik.uninstall(self.spec)
        self.assertFalse((self.claude / "skills/demo").is_symlink())
        self.assertFalse((self.home / ".config/democrew/install.json").exists())

    def test_a_malformed_manifest_of_another_crew_does_not_stop_an_install_and_is_named(self):
        for name, man in self.shapes().items():
            with self.subTest(name):
                self.fresh()
                path = self.other(man)
                said = self.run_install()
                self.assertTrue((self.claude / "skills/demo").is_symlink())
                self.assertTrue((self.home / ".config/democrew/install.json").exists())
                self.assertTrue(any(str(path) in l for l in said), said)

    def test_a_malformed_manifest_of_another_crew_does_not_half_run_an_uninstall(self):
        for name, man in self.shapes().items():
            with self.subTest(name):
                self.fresh()
                self.run_install()
                path = self.other(man)
                said = ik.uninstall(self.spec)
                self.assertFalse((self.claude / "skills/demo").is_symlink())
                self.assertFalse((self.claude / "agents/a.md").exists())
                self.assertFalse((self.home / ".local/bin/democrew").exists())
                self.assertFalse((self.home / ".config/democrew/install.json").exists())
                self.assertEqual(json.loads((self.claude / "settings.json").read_text()), {})
                self.assertTrue(path.exists())
                self.assertTrue(any(str(path) in l for l in said), said)

    def test_a_record_that_names_no_group_protects_nothing_and_its_neighbours_still_do(self):
        self.run_install()
        guard = {"matcher": "Bash", "hooks": [{"type": "command", "command": f"guard {self.root}"}]}
        path = self.other({"settings": [{"file": str(self.claude / "settings.json"), "permissions": None,
                                         "hooks": [{"event": "PreToolUse"}, {"event": "PreToolUse", "group": guard}]}]})
        said = ik.uninstall(self.spec)
        self.assertEqual(json.loads((self.claude / "settings.json").read_text()), {"hooks": {"PreToolUse": [guard]}})
        self.assertTrue(any(str(path) in l and "names no group" in l for l in said), said)


class Shape(Base):
    """A settings.json that is valid JSON but holds a wrong type where install or uninstall reads or writes stops them
    in the doctor's form before anything changes, as one that does not parse does."""

    BAD = {
        "hooks a list": {"hooks": []},
        "hooks null": {"hooks": None},
        "hooks a string": {"hooks": "x"},
        "the event install adds to is null": {"hooks": {"SessionStart": None}},
        "the other event install adds to is an object": {"hooks": {"PreToolUse": {}}},
        "permissions null": {"permissions": None},
        "permissions a list": {"permissions": []},
        "allow null": {"permissions": {"allow": None}},
        "allow a string": {"permissions": {"allow": "Bash(ls)"}},
    }

    def refused(self, fragment, outcome="installed", call=None):
        before = self.snap()
        with self.assertRaises(ik.InstallError) as c:
            (call or self.run_install)()
        msg = str(c.exception)
        self.assertTrue(msg.startswith(f"conflicts found, nothing {outcome}:\n[FAIL] "), msg)
        self.assertIn(fragment, msg)
        self.assertIn(" - fix it by hand", msg)
        self.assertEqual(self.snap(), before)
        return msg

    def test_a_fresh_install_over_each_wrong_shape_changes_nothing(self):
        for name, data in self.BAD.items():
            with self.subTest(name):
                self.write_settings(data)
                self.refused(f"{self.claude / 'settings.json'}: ")
                for gone in ("skills", "agents"):
                    self.assertFalse((self.claude / gone).exists())
                self.assertFalse((self.home / ".config/democrew").exists())

    def test_the_row_names_the_part_and_what_it_is(self):
        self.write_settings({"hooks": {"SessionStart": None}})
        self.refused("hooks.SessionStart is null, not a list")
        self.write_settings({"hooks": []})
        self.refused("hooks is a list, not an object")
        self.write_settings({"permissions": {"allow": "Bash(ls)"}})
        self.refused("permissions.allow is a string, not a list")
        self.write_settings({"permissions": None})
        self.refused("permissions is null, not an object")

    def test_every_wrong_part_is_named_in_one_message_with_the_other_conflicts(self):
        self.write_settings({"hooks": {"SessionStart": None, "PreToolUse": 5}, "permissions": []})
        (self.claude / "skills/demo-extra").mkdir(parents=True)
        msg = self.refused("hooks.SessionStart is null, not a list")
        rows = msg.splitlines()[1:]
        self.assertEqual(len(rows), 4, msg)
        self.assertTrue(all(r.startswith("[FAIL] ") for r in rows), msg)
        self.assertIn("hooks.PreToolUse is a number, not a list", msg)
        self.assertIn("permissions is a list, not an object", msg)
        self.assertIn("demo-extra already exists", msg)

    def test_a_reinstall_over_a_wrong_shape_changes_nothing(self):
        self.run_install()
        (self.root / "agents/a.md").write_text("new {crew_path}\n")  # an agent the install would rewrite
        (self.claude / "skills/demo-extra").unlink()  # a link it would put back
        self.write_settings({"hooks": {"SessionStart": None}})
        self.refused("hooks.SessionStart is null, not a list")

    def test_uninstall_over_a_wrong_shape_removes_nothing_and_finishes_once_fixed(self):
        self.run_install()
        good = json.loads((self.claude / "settings.json").read_text())
        for name, data in self.BAD.items():
            with self.subTest(name):
                self.write_settings(data)
                self.refused(f"{self.claude / 'settings.json'}: ", "removed", lambda: ik.uninstall(self.spec))
        self.write_settings(good)
        ik.uninstall(self.spec)
        self.assertFalse((self.claude / "skills/demo").is_symlink())
        self.assertFalse((self.home / ".config/democrew/install.json").exists())
        self.assertEqual(json.loads((self.claude / "settings.json").read_text()), {})

    def test_a_part_the_crew_never_touches_is_not_a_reason_to_refuse(self):
        self.write_settings({"hooks": {"Stop": None}, "model": "opus"})
        self.run_install()
        data = json.loads((self.claude / "settings.json").read_text())
        self.assertIsNone(data["hooks"]["Stop"])
        self.assertIn("SessionStart", data["hooks"])
        ik.uninstall(self.spec)
        self.assertEqual(json.loads((self.claude / "settings.json").read_text()), {"hooks": {"Stop": None}, "model": "opus"})

    def test_the_command_line_exits_1_and_prints_the_rows(self):
        self.write_settings({"hooks": {"SessionStart": None}})
        before = self.snap()
        out = self.failed_install()
        self.assertIn("conflicts found, nothing installed:", out)
        self.assertIn("[FAIL]", out)
        self.assertEqual(self.snap(), before)


if __name__ == "__main__":
    unittest.main()
