"""A conflict, a link to nothing or a record in a shape install never writes stops it with nothing changed."""
import json
import shutil
import subprocess
import unittest

from crewkit import install as ik
from installcase import Base


class Preflight(Base):
    """A conflict anywhere stops the install with nothing changed, however late in the install it would have hit."""

    def fingerprint(self):
        """The scratch HOME as `find` and `shasum` see it: every path, every file's hash, every link's target."""
        sh = lambda cmd: subprocess.run(cmd, shell=True, cwd=self.home, check=True, capture_output=True, text=True).stdout
        return (sh("find . | sort"), sh("find . -type f -exec shasum {} + | sort"),
                sh("find . -type l -exec sh -c 'for f; do echo \"$f $(readlink \"$f\")\"; done' _ {} + | sort"))

    def refused(self, fragment, **kw):
        """Run the install, which must stop with nothing changed and a message holding `fragment`."""
        before, snap = self.fingerprint(), self.snap()
        with self.assertRaises(ik.InstallError) as c:
            self.run_install(**kw)
        msg = str(c.exception)
        self.assertTrue(msg.startswith("conflicts found, nothing installed:\n"), msg)
        self.assertIn(fragment, msg)
        self.assertEqual(self.fingerprint(), before)
        self.assertEqual(self.snap(), snap)
        return msg

    def test_a_link_that_is_not_ours_on_the_second_skill_changes_nothing(self):
        (self.claude / "skills/demo-extra").mkdir(parents=True)  # demo would have been linked first
        (self.claude / "skills/demo-extra/mine.txt").write_text("keep\n")
        msg = self.refused(f"{self.claude / 'skills/demo-extra'} already exists and is not a link to")
        self.assertIn("use --force", msg)
        self.assertFalse((self.claude / "skills/demo").exists())

    def test_a_link_to_elsewhere_changes_nothing(self):
        other = self.tmp / "other"
        other.mkdir()
        (self.claude / "skills").mkdir(parents=True)
        (self.claude / "skills/demo-extra").symlink_to(other)
        self.refused("is not a link to")
        self.assertFalse((self.claude / "skills/demo").exists())

    def test_an_agent_file_that_is_not_ours_changes_nothing(self):
        (self.claude / "agents").mkdir(parents=True)
        (self.claude / "agents/a.md").write_text("someone else's\n")
        msg = self.refused(f"{self.claude / 'agents/a.md'} already exists and is not democrew's as installed")
        self.assertIn("use --force", msg)
        self.assertFalse((self.claude / "skills").exists())

    def test_a_folder_where_an_agent_file_goes_changes_nothing(self):
        (self.claude / "agents/a.md").mkdir(parents=True)
        self.refused("already exists and is not democrew's as installed")
        self.assertFalse((self.claude / "skills").exists())

    def test_an_agent_file_that_is_not_text_changes_nothing(self):
        (self.claude / "agents").mkdir(parents=True)
        (self.claude / "agents/a.md").write_bytes(b"\xff\xfe\x00not utf-8")
        self.refused("already exists and is not democrew's as installed")

    def test_an_agent_file_edited_after_the_install_changes_nothing_on_a_reinstall(self):
        self.run_install()
        (self.claude / "agents/a.md").write_text("edited by hand\n")
        (self.claude / "skills/demo").unlink()  # a link the install would put back
        self.refused("is not democrew's as installed")
        self.assertFalse((self.claude / "skills/demo").exists())

    def test_a_launcher_that_is_not_ours_changes_nothing(self):
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".local/bin/democrew").write_text("someone else's\n")
        msg = self.refused(f"{self.home / '.local/bin/democrew'} already exists and is not democrew's as installed")
        self.assertIn("use --force", msg)
        self.assertFalse((self.claude / "skills").exists())
        self.assertFalse((self.claude / "agents").exists())

    def test_a_missing_agents_dir_changes_nothing(self):
        shutil.rmtree(self.root / "agents")
        msg = self.refused(f"agents_dir {self.root / 'agents'} does not exist")
        self.assertIn("[tool.crewkit] agents_dir", msg)
        self.assertFalse((self.claude / "skills").exists())

    def test_a_file_where_a_folder_must_be_made_changes_nothing(self):
        (self.claude).mkdir(parents=True)
        (self.claude / "agents").write_text("not a folder\n")
        msg = self.refused(f"{self.claude / 'agents/a.md'} cannot be made, {self.claude / 'agents'} is not a folder")
        self.assertFalse((self.claude / "skills").exists())
        self.assertIn("[FAIL]", msg)

    def test_a_file_where_the_launcher_folder_must_be_made_changes_nothing(self):
        (self.home / ".local").write_text("not a folder\n")
        self.refused(f"{self.home / '.local/bin/democrew'} cannot be made")
        self.assertFalse((self.claude / "skills").exists())

    def test_every_conflict_is_named_in_one_message_in_the_doctors_form(self):
        (self.claude / "skills/demo").mkdir(parents=True)
        (self.claude / "agents").mkdir(parents=True)
        (self.claude / "agents/a.md").write_text("someone else's\n")
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".local/bin/democrew").write_text("someone else's\n")
        msg = self.refused("demo already exists")
        rows = msg.splitlines()[1:]
        self.assertEqual(len(rows), 3, msg)
        self.assertTrue(all(r.startswith("[FAIL] ") and r.endswith(" - remove it or use --force") for r in rows), msg)

    def test_a_settings_json_that_will_not_parse_still_comes_first(self):
        self.claude.mkdir(parents=True)
        (self.claude / "settings.json").write_text("{ not json")
        (self.claude / "skills/demo").mkdir(parents=True)
        with self.assertRaises(ik.InstallError) as c:
            self.run_install()
        self.assertIn("not valid JSON", str(c.exception))

    def test_the_command_line_exits_1_and_prints_the_rows_and_the_repo(self):
        (self.claude / "skills/demo-extra").mkdir(parents=True)
        before = self.fingerprint()
        out = self.failed_install()
        self.assertIn("conflicts found, nothing installed:", out)
        self.assertIn("[FAIL]", out)
        self.assertIn("owner/democrew", out)
        self.assertEqual(self.fingerprint(), before)

    def test_force_still_moves_each_kind_of_conflict_aside(self):
        other = self.tmp / "other"
        other.mkdir()
        (self.claude / "skills").mkdir(parents=True)
        (self.claude / "skills/demo-extra").symlink_to(other)
        (self.claude / "skills/demo").mkdir()
        (self.claude / "agents/a.md").mkdir(parents=True)  # a folder: moved aside too
        (self.home / ".local/bin").mkdir(parents=True)
        (self.home / ".local/bin/democrew").write_text("someone else's\n")
        self.run_install(force=True)
        self.assert_linked()
        self.assertEqual((self.claude / "agents/a.md").read_text(), f"see {self.root}\n")
        self.assertIn("exec uv run", (self.home / ".local/bin/democrew").read_text())
        asides = [*self.claude.glob("skills/*.bak-*"), *self.claude.glob("agents/*.bak-*"),
                  *(self.home / ".local/bin").glob("*.bak-*")]
        self.assertEqual(len(asides), 4, asides)
        man = json.loads((self.home / ".config/democrew/install.json").read_text())
        self.assertEqual(sorted(man["backups"]), sorted(str(a) for a in asides))

    def test_force_does_not_move_a_file_aside_to_make_a_folder(self):
        (self.claude).mkdir(parents=True)
        (self.claude / "agents").write_text("not a folder\n")
        self.refused("is not a folder", force=True)

    def test_a_dangling_link_where_an_agent_file_goes_changes_nothing(self):
        self.dangle(self.claude / "agents/a.md")  # its folder is missing too
        msg = self.refused(f"{self.claude / 'agents/a.md'} already exists and is not democrew's as installed")
        self.assertIn("use --force", msg)
        self.assertFalse((self.claude / "skills").exists())

    def test_a_dangling_link_into_a_folder_that_exists_is_not_written_through(self):
        (self.tmp / "elsewhere").mkdir()
        self.dangle(self.claude / "agents/a.md", "elsewhere")
        self.dangle(self.home / ".local/bin/democrew", "elsewhere")
        msg = self.refused(f"{self.claude / 'agents/a.md'} already exists and is not democrew's as installed")
        self.assertIn(f"{self.home / '.local/bin/democrew'} already exists and is not democrew's as installed", msg)
        self.assertEqual(list((self.tmp / "elsewhere").iterdir()), [])

    def test_a_dangling_link_where_the_launcher_goes_changes_nothing(self):
        self.dangle(self.home / ".local/bin/democrew")
        self.refused(f"{self.home / '.local/bin/democrew'} already exists and is not democrew's as installed")
        self.assertFalse((self.claude / "skills").exists())
        self.assertFalse((self.claude / "agents").exists())

    def test_force_moves_a_dangling_link_aside_and_writes_nothing_through_it(self):
        (self.tmp / "elsewhere").mkdir()
        self.dangle(self.claude / "agents/a.md", "elsewhere")
        self.dangle(self.home / ".local/bin/democrew")
        self.run_install(force=True)
        self.assertFalse((self.claude / "agents/a.md").is_symlink())
        self.assertEqual((self.claude / "agents/a.md").read_text(), f"see {self.root}\n")
        self.assertFalse((self.home / ".local/bin/democrew").is_symlink())
        self.assertEqual(list((self.tmp / "elsewhere").iterdir()), [])
        asides = [*self.claude.glob("agents/*.bak-*"), *(self.home / ".local/bin").glob("*.bak-*")]
        self.assertEqual(len(asides), 2, asides)
        self.assertTrue(all(a.is_symlink() for a in asides))
        man = json.loads((self.home / ".config/democrew/install.json").read_text())
        self.assertEqual(sorted(man["backups"]), sorted(str(a) for a in asides))


class LinkedRecord(Base):
    """A settings.json or install.json that is a link to nothing, or a record that is not a JSON object or holds a list
    in a shape install never writes, stops install and uninstall before they change anything."""

    def stops(self, run, fragment):
        before = self.snap()
        with self.assertRaises(ik.InstallError) as c:
            run()
        msg = str(c.exception)
        self.assertEqual(len(msg.splitlines()), 1, msg)
        self.assertIn(fragment, msg)
        self.assertEqual(self.snap(), before)
        return msg

    def test_install_over_a_settings_json_that_links_to_nothing_changes_nothing(self):
        (self.tmp / "elsewhere").mkdir()
        settings = self.claude / "settings.json"
        for into in ("gone", "elsewhere"):  # the link's folder missing, then there
            with self.subTest(into):
                settings.unlink(missing_ok=True)
                self.dangle(settings, into)
                msg = self.stops(self.run_install, f"{settings} is a link to {self.tmp / into / 'settings.json'}")
                self.assertIn("nothing installed", msg)
                self.assertFalse((self.claude / "skills").exists())
                self.assertEqual(list((self.tmp / "elsewhere").iterdir()), [])

    def test_install_over_an_install_json_that_links_to_nothing_changes_nothing(self):
        man = self.home / ".config/democrew/install.json"
        self.dangle(man)
        self.stops(self.run_install, f"{man} is a link to")
        self.assertFalse((self.claude / "skills").exists())
        self.assertFalse((self.home / ".local/bin/democrew").exists())

    def test_install_over_an_install_json_that_is_not_an_object_changes_nothing(self):
        man = self.home / ".config/democrew/install.json"
        man.parent.mkdir(parents=True)
        for text in ("[]", "null", "{ not json"):
            with self.subTest(text):
                man.write_text(text)
                self.stops(self.run_install, str(man))
                self.assertFalse((self.claude / "skills").exists())

    def test_uninstall_stops_on_a_settings_json_or_install_json_that_links_to_nothing(self):
        self.run_install()
        for path in (self.claude / "settings.json", self.home / ".config/democrew/install.json"):
            with self.subTest(path.name):
                kept = self.tmp / f"kept-{path.name}"
                path.rename(kept)
                self.dangle(path)
                msg = self.stops(lambda: ik.uninstall(self.spec), f"{path} is a link to")
                self.assertIn("nothing removed", msg)
                self.assertTrue((self.claude / "skills/demo").is_symlink())
                path.unlink()
                kept.rename(path)
        ik.uninstall(self.spec)
        self.assertFalse((self.claude / "skills/demo").is_symlink())
        self.assertNotIn("hooks", json.loads((self.claude / "settings.json").read_text()))

    def test_a_record_holding_a_list_in_a_shape_install_never_writes_stops_both(self):
        self.run_install()
        man = self.home / ".config/democrew/install.json"
        good = json.loads(man.read_text())
        bad = {"links": [None, "x", {}, [1], [{"path": "p"}]], "files": [None, [1], [{"path": "p", "sha256": 1}]],
               "settings": [None, "x", [{"file": 1}], [{"file": "f", "hooks": [1]}],
                            [{"file": "f", "hooks": [{"event": "E"}]}], [{"file": "f", "permissions": [None]}]],
               "backups": [None, {}, [1]]}
        for key, values in bad.items():
            for value in values:
                man.write_text(json.dumps({**good, key: value}))
                for run, outcome in ((self.run_install, "nothing installed"),
                                     (lambda: ik.uninstall(self.spec), "nothing removed")):
                    with self.subTest(key=key, value=value, outcome=outcome):
                        self.assertIn(outcome, self.stops(run, f"{man} holds {key} in a shape install never writes"))
        man.write_text(json.dumps(good))
        ik.uninstall(self.spec)
        self.assertFalse((self.claude / "skills/demo").is_symlink())


if __name__ == "__main__":
    unittest.main()
