"""crewkit.spec: reading [tool.crewkit], finding the clone, and doctor's check of the box."""
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest import mock

from crewkit import install as ik
from installcase import ODD, PYPROJECT, Base


class Doctor(Base):
    def test_hard_miss_exits_nonzero_and_blocks_install(self):
        code, lines = ik.doctor(self.spec, which=lambda b: None if b == "uv" else "/bin/" + b)
        self.assertEqual(code, 1)
        self.assertTrue(any(l.startswith("[FAIL] uv") for l in lines))
        with mock.patch.object(ik.shutil, "which", lambda b: None):
            with self.assertRaises(ik.InstallError):
                ik.install(self.spec)
        self.assertFalse((self.claude / "skills").exists())

    def test_soft_miss_warns_and_exits_zero(self):
        code, lines = ik.doctor(self.spec, which=lambda b: None if b.startswith("pdf") else "/bin/" + b)
        self.assertEqual(code, 0)
        self.assertTrue(any(l.startswith("[warn] poppler") and "brew install" in l for l in lines))
        self.assertTrue(any(l.startswith("[note] Claude in Chrome") for l in lines))

    def test_cli_exit_codes(self):
        with mock.patch.object(ik.shutil, "which", lambda b: "/bin/" + b):
            self.assertEqual(ik.main(["doctor"], root=self.root), 0)
        with mock.patch.object(ik.shutil, "which", lambda b: None):
            self.assertEqual(ik.main(["doctor"], root=self.root), 1)

    def test_the_off_path_hint_shell_quotes_the_clone_path(self):
        self.clone_at(ODD)
        with mock.patch.dict(os.environ, {"PATH": "/usr/bin"}):
            _, lines = ik.doctor(self.spec, which=lambda b: "/bin/" + b)
        quoted = f"'{self.tmp}/my \"clone\" $HOME `x` it'\"'\"'s/democrew'"  # a ' closes, "'" adds one, ' reopens
        self.assertIn(f"[warn] {self.home}/.local/bin on PATH - add it to PATH, or run `uv run --project {quoted} "
                      "<command>`", lines)

    def test_error_names_the_repo(self):
        with mock.patch.object(ik.shutil, "which", lambda b: None), mock.patch("sys.stderr") as err:
            self.assertEqual(ik.main(["install"], root=self.root), 1)
        self.assertIn("owner/democrew", "".join(c.args[0] for c in err.write.call_args_list))


class Resolution(Base):
    def test_order_env_then_config_then_skill_path(self):
        skill = self.root / "skills/demo"
        self.assertEqual(ik.resolve_crew_path("democrew", skill), self.root.resolve())
        cfg = self.tmp / "cfgroot"
        cfg.mkdir()
        (self.home / ".config/democrew").mkdir(parents=True)
        (self.home / ".config/democrew/crew-path").write_text(str(cfg))
        self.assertEqual(ik.resolve_crew_path("democrew", skill), cfg)
        env = self.tmp / "envroot"
        env.mkdir()
        self.assertEqual(ik.resolve_crew_path("democrew", skill, {"DEMOCREW_HOME": str(env)}), env)

    def test_stale_values_fall_through_and_none_resolves(self):
        (self.home / ".config/democrew").mkdir(parents=True)
        (self.home / ".config/democrew/crew-path").write_text("/nonexistent/x")
        self.assertEqual(ik.resolve_crew_path("democrew", self.root / "skills/demo", {"DEMOCREW_HOME": "/nope"}), self.root.resolve())
        self.assertIsNone(ik.resolve_crew_path("democrew", None, {}))

    def test_skill_symlink_resolves_through_real_path(self):
        self.run_install()
        link = self.claude / "skills/demo"
        self.assertEqual(ik.resolve_crew_path("democrew", link, {}), self.root.resolve())

    def test_find_root_walks_up(self):
        self.assertEqual(ik.find_root(self.root / "skills/demo"), self.root)

    def test_bad_tables_are_clear_errors(self):
        (self.root / "pyproject.toml").write_text('[project]\nname="x"\n')
        with self.assertRaises(ik.InstallError):
            ik.load_spec(self.root)
        (self.root / "pyproject.toml").write_text('[project]\nname="x"\n[tool.crewkit]\nskills=["a"]\nbogus=1\n')
        with self.assertRaises(ik.InstallError) as c:
            ik.load_spec(self.root)
        self.assertIn("bogus", str(c.exception))



class StoplineRow(Base):
    """D1 and D2: doctor runs the interpreter a declared crewkit.stopline hook names."""

    def setUp(self):
        super().setUp()
        import sys
        import usagecase as uc
        self.python, self.uc = sys.executable, uc
        text = (self.root / "pyproject.toml").read_text()
        (self.root / "pyproject.toml").write_text(text + '\n[[tool.crewkit.hooks]]\nevent = "Stop"\ncommand = "'
                                                  + "{crew_path}/.venv/bin/python -I -m crewkit.stopline demo || true" + '"\n')
        self.spec = ik.load_spec(self.root)

    def rows(self, spec=None):
        code, lines = ik.doctor(spec or self.spec, which=lambda b: "/bin/" + b)
        return code, [l for l in lines if "stopline" in l]

    def test_d1_ok_warn_and_absent(self):
        self.uc.wrapper(self.root, self.python)
        self.assertEqual(self.rows(), (0, ["[ok  ] stopline hook"]))
        self.uc.wrapper(self.root, self.python, "-S")
        code, rows = self.rows()
        self.assertEqual(code, 0)
        self.assertEqual(rows, [f"[warn] stopline hook - run `uv sync` in {self.root}"])
        (self.root / "pyproject.toml").write_text(PYPROJECT)
        self.assertEqual(self.rows(ik.load_spec(self.root)), (0, []))

    def test_d1_a_missing_venv_warns(self):
        self.assertEqual(self.rows()[1], [f"[warn] stopline hook - run `uv sync` in {self.root}"])

    def test_d2_doctor_from_outside_the_launcher_and_install_print_the_row(self):
        self.uc.wrapper(self.root, self.python, "-S")
        out = io.StringIO()
        with mock.patch.object(ik.shutil, "which", lambda b: "/bin/" + b), redirect_stdout(out):
            self.assertEqual(ik.main(["doctor", "--root", str(self.root)]), 0)
        warn = f"[warn] stopline hook - run `uv sync` in {self.root}"
        self.assertIn(warn, out.getvalue().splitlines())
        self.assertIn(warn, self.run_install())


if __name__ == "__main__":
    unittest.main()
