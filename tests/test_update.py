import datetime
import json
import os
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from crewkit import install, update
from installcase import scratch

UNFETCHED = ("the remote has moved on since the last fetch, so behind or diverged; `git -C {} fetch`, then "
             "`somecrew update --force` tells which")
PYPROJECT = '[tool.uv.sources]\ncrewkit = { git = "https://example.com/crewkit", tag = "v0.10.1" }\n'


def fake_git(remote_head="a" * 40, head="a" * 40, known=True, ancestor=False, tags=("v0.9.0", "v0.10.1", "v0.10.4"),
             branch=True):
    calls = []

    def git(*args, cwd=None):
        calls.append(args)
        if args[0] == "rev-parse" and "@{u}" in args:
            return "origin/main\n"
        if args[0] == "rev-parse":
            return head + "\n"
        if args[:2] == ("ls-remote", "--tags"):
            return "".join(f"{i:040x}\trefs/tags/{t}\n" for i, t in enumerate(tags))
        if args[0] == "ls-remote":
            return f"{remote_head}\trefs/heads/main\n" if branch else ""
        if args[0] == "cat-file" and not known:
            raise OSError("missing")
        if args[0] == "merge-base":
            if (args[2] == remote_head) == ancestor:
                return ""
            raise OSError("not ancestor")
        return ""
    git.calls = calls
    return git


def remote(git) -> list:
    """The calls a fake git got that would have reached the remote."""
    return [c for c in git.calls if c[0] == "ls-remote"]


def real_clone(d: Path):
    """A git runner, and a bare remote under `d` with one commit on main and a clone of it in a folder with a space."""
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    run = lambda *a, cwd: subprocess.run(["git", *a], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    remote, clone = d / "remote.git", d / "my clone"
    run("init", "-q", "--bare", "-b", "main", str(remote), cwd=d)
    run("clone", "-q", str(remote), str(clone), cwd=d)
    run("checkout", "-q", "-b", "main", cwd=clone)
    run("commit", "-q", "--allow-empty", "-m", "one", cwd=clone)
    run("push", "-q", "-u", "origin", "main", cwd=clone)
    (clone / "pyproject.toml").write_text("[project]\nname = 'x'\n")  # pins no crewkit, so nothing leaves the box
    return run, remote, clone


def push_elsewhere(run, remote: Path, *, rewrite: bool = False) -> None:
    """A commit on `remote` from a second clone, which the clone under test has not fetched; with `rewrite`, one that
    replaces the remote's last commit, force-pushed."""
    other = remote.parent / "other"
    run("clone", "-q", str(remote), str(other), cwd=remote.parent)
    run("commit", "-q", "--allow-empty", *(["--amend"] if rewrite else []), "-m", "theirs", cwd=other)
    run("push", "-q", *(["-f"] if rewrite else []), "origin", "main", cwd=other)


class Update(unittest.TestCase):
    def setUp(self):
        self.d = scratch(self)
        (self.d / "pyproject.toml").write_text(PYPROJECT)

    def test_reports_a_pin_behind_the_newest_tag(self):
        lines = update.check(self.d, "somecrew", git=fake_git())
        self.assertEqual(lines[0], "update: something is behind")
        self.assertIn("pins v0.10.1, newest is v0.10.4", lines[2])
        self.assertEqual(lines[1], "somecrew clone: current")

    def test_newest_compares_versions_not_strings(self):
        self.assertEqual(update.newest_tag("u", fake_git(tags=("v0.9.0", "v0.10.0", "v0.2.0"))), "v0.10.0")

    def test_a_remote_commit_never_fetched_names_the_fetch(self):
        lines = update.check(self.d, "somecrew", git=fake_git(remote_head="b" * 40, known=False, tags=("v0.10.1",)))
        self.assertEqual(lines, ["update: something is behind", f"somecrew clone: {UNFETCHED.format(self.d)}",
                                 "crewkit: v0.10.1 is the newest"])

    def test_an_unfetched_remote_tip_names_the_fetch_and_a_fetch_tells_which_in_a_real_clone(self):
        """Without the remote's commit the check can't tell behind from diverged, so it says so and names the fetch;
        after one it reads what git does. A push to origin's URL by URL moves @{u} under git 2.56.1 and not under
        2.55, so @{u} is read after the steps."""
        cases = ((["theirs"], "behind"), (["ours", "theirs"], "diverged"),
                 (["ours", "ours by URL", "theirs"], "behind"), (["rewritten"], "diverged"))
        for i, (steps, truth) in enumerate(cases):
            with self.subTest(steps):
                (self.d / str(i)).mkdir()
                run, remote, clone = real_clone(self.d / str(i))
                step = {"ours": lambda: run("commit", "-q", "--allow-empty", "-m", "ours", cwd=clone),
                        "ours by URL": lambda: run("push", "-q", str(remote), "main", cwd=clone),
                        "theirs": lambda: push_elsewhere(run, remote),
                        "rewritten": lambda: push_elsewhere(run, remote, rewrite=True)}
                for s in steps:
                    step[s]()
                fetched = run("rev-parse", "@{u}", cwd=clone).stdout
                q = f"'{clone}'"  # the clone's folder has a space, so the hint quotes it
                self.assertEqual(update.check(clone, "somecrew"),
                                 ["update: something is behind", f"somecrew clone: {UNFETCHED.format(q)}"])
                self.assertEqual(run("rev-parse", "@{u}", cwd=clone).stdout, fetched)  # nothing fetched, by either
                run("fetch", "-q", cwd=clone)
                mine = run("rev-list", "--left-right", "--count", "HEAD...@{u}", cwd=clone).stdout.split()[0]
                self.assertEqual("diverged" if mine != "0" else "behind", truth)  # what git says, once fetched
                self.assertEqual(update.check(clone, "somecrew"), [  # the headline too: diverged is behind
                    "update: something is behind", "somecrew clone: diverged" if truth == "diverged"
                    else f"somecrew clone: behind; `git -C {q} pull --ff-only`, then `somecrew install`"])

    def test_a_local_commit_the_remote_lacks_is_ahead_in_a_real_clone(self):
        run, _, clone = real_clone(self.d)
        run("commit", "-q", "--allow-empty", "-m", "ours", cwd=clone)
        self.assertEqual(update.check(clone, "somecrew"), ["update: up to date", "somecrew clone: ahead"])

    def test_offline_steps_aside_and_is_not_cached(self):
        def down(*a, cwd=None):
            raise OSError("Could not resolve host")
        cache = self.d / "c.json"
        lines = update.check(self.d, "somecrew", cache=cache, git=down)
        self.assertEqual(lines[0], "update: not checked: the clone and the crewkit pin")
        self.assertTrue(all("not checked" in l for l in lines[1:]))
        self.assertFalse(cache.exists())

    def test_a_pull_or_a_new_pin_asks_again_the_same_day(self):
        cache, today = self.d / "c.json", "2026-10-28"
        lines = update.check(self.d, "somecrew", cache=cache, today=today, git=fake_git(remote_head="b" * 40))
        self.assertIn("somecrew clone: behind", lines[1])
        pulled = fake_git(remote_head="b" * 40, head="b" * 40, tags=("v0.10.1", "v0.10.4"))
        lines = update.check(self.d, "somecrew", cache=cache, today=today, git=pulled)
        self.assertEqual(lines[1], "somecrew clone: current")
        self.assertNotIn("checked earlier today", lines[-1])
        (self.d / "pyproject.toml").write_text(PYPROJECT.replace("v0.10.1", "v0.10.4"))
        lines = update.check(self.d, "somecrew", cache=cache, today=today, git=pulled)
        self.assertEqual(lines, ["update: up to date", "somecrew clone: current", "crewkit: v0.10.4 is the newest"])

    def test_asks_github_once_a_day(self):
        cache, git = self.d / "c.json", fake_git()
        update.check(self.d, "somecrew", cache=cache, today="2026-10-28", git=git)
        n = len(remote(git))
        again = update.check(self.d, "somecrew", cache=cache, today="2026-10-28", git=git)
        self.assertEqual(len(remote(git)), n)
        self.assertIn("checked earlier today", again[-1])
        update.check(self.d, "somecrew", cache=cache, today="2026-10-29", git=git)
        self.assertGreater(len(remote(git)), n)
        self.assertEqual(json.loads(cache.read_text())["date"], "2026-10-29")


class Headline(unittest.TestCase):
    """The first line says what was checked: it never says "up to date" over a part that was not."""

    def setUp(self):
        self.d = scratch(self)
        (self.d / "pyproject.toml").write_text(PYPROJECT)

    def test_everything_checked_and_current_is_up_to_date(self):
        self.assertEqual(update.check(self.d, "somecrew", git=fake_git(tags=("v0.10.1",)))[0], "update: up to date")

    def test_a_clone_that_could_not_be_checked_is_named_not_called_up_to_date(self):
        def git(*args, cwd=None):
            if cwd is not None:
                raise OSError("fatal: no upstream configured")
            return fake_git(tags=("v0.10.1",))(*args)
        lines = update.check(self.d, "somecrew", git=git)
        self.assertEqual(lines[0], "update: the crewkit pin not behind; the clone not checked")
        self.assertIn("somecrew clone: not checked (fatal: no upstream configured)", lines)

    def test_a_pin_that_could_not_be_checked_is_named_not_called_up_to_date(self):
        def git(*args, cwd=None):
            if cwd is None:
                raise OSError("Could not resolve host")
            return fake_git()(*args, cwd=cwd)
        lines = update.check(self.d, "somecrew", git=git)
        self.assertEqual(lines[0], "update: the clone not behind; the crewkit pin not checked")

    def test_something_behind_still_says_so_whatever_was_not_checked(self):
        def git(*args, cwd=None):
            if cwd is None:
                raise OSError("Could not resolve host")
            return fake_git(remote_head="b" * 40, known=False)(*args, cwd=cwd)
        lines = update.check(self.d, "somecrew", git=git)
        self.assertEqual(lines[0], "update: something is behind")
        self.assertIn("crewkit: not checked (Could not resolve host)", lines)

    def test_a_crew_that_pins_no_crewkit_is_up_to_date_on_its_clone_alone(self):
        (self.d / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        self.assertEqual(update.check(self.d, "somecrew", git=fake_git())[0], "update: up to date")

    def test_a_pin_that_is_not_x_y_z_is_not_checked_and_says_why(self):
        for tag in ("main", "v0.12.0-rc1"):
            with self.subTest(tag):
                (self.d / "pyproject.toml").write_text(PYPROJECT.replace("v0.10.1", tag))
                cache, git = self.d / f"c-{tag}.json", fake_git()
                lines = update.check(self.d, "somecrew", cache=cache, git=git)
                self.assertEqual(lines[0], "update: the clone not behind; the crewkit pin not checked")
                self.assertIn(f"crewkit: not checked (the pin {tag} is not an x.y.z tag)", lines)
                self.assertNotIn(("ls-remote", "--tags", "--refs", "https://example.com/crewkit"), git.calls)
                self.assertFalse(cache.exists())

    def test_a_remote_with_no_x_y_z_tag_is_not_called_the_newest(self):
        lines = update.check(self.d, "somecrew", git=fake_git(tags=("latest", "nightly")))
        self.assertEqual(lines[0], "update: the clone not behind; the crewkit pin not checked")
        self.assertIn("crewkit: not checked (https://example.com/crewkit has no x.y.z tag)", lines)
        self.assertFalse(any("is the newest" in l for l in lines), lines)


class Refused(unittest.TestCase):
    """An answer kept in a shape this code did not write, and a branch the remote no longer has, are each one clear
    line and a nonzero exit; nothing is written."""

    def setUp(self):
        self.d = scratch(self)
        (self.d / "pyproject.toml").write_text(PYPROJECT)
        self.cache = self.d / "c.json"
        self.today = "2026-10-28"
        self.key = {"date": self.today, "root": str(self.d), "head": "a" * 40,
                    "pin": ["https://example.com/crewkit", "v0.10.1"]}

    def keep(self, data):
        self.cache.write_text(data if isinstance(data, str) else json.dumps(data))
        return self.cache.read_bytes()

    def refuses(self, fragment, git=None, **kw):
        git = git or fake_git()
        with self.assertRaises(install.InstallError) as c:
            update.check(self.d, "somecrew", cache=self.cache, today=self.today, git=git, **kw)
        msg = str(c.exception)
        self.assertEqual(len(msg.splitlines()), 1, msg)
        self.assertIn(fragment, msg)
        return msg, git

    def asks_again(self, **kw):
        """check() asks git and replaces the kept answer with today's; the lines it gave."""
        lines = update.check(self.d, "somecrew", cache=self.cache, today=self.today, git=fake_git(tags=("v0.10.1",)),
                             **kw)
        self.assertEqual(json.loads(self.cache.read_text())["date"], self.today)
        return lines

    def cli_update(self, home, root=None):
        """`update` from the command line with HOME at `home`, which must exit 1; what it printed."""
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch("sys.stderr") as err:
            self.assertEqual(install.main(["update"], root=root or self.d), 1)
        return "".join(c.args[0] for c in err.write.call_args_list)

    def test_a_kept_answer_of_the_wrong_shape_stops_with_one_line(self):
        shapes = {
            "a list": [],
            "a string": '"x"',
            "null": None,
            "lines null": {**self.key, "lines": None},
            "lines a string": {**self.key, "lines": "update: up to date"},
            "lines a number": {**self.key, "lines": 5},
            "a line that is not text": {**self.key, "lines": ["update: up to date", None]},
        }
        for name, data in shapes.items():
            with self.subTest(name):
                kept = self.keep(data)
                msg, git = self.refuses(f"{self.cache} ")
                self.assertIn("somecrew update --force", msg)
                self.assertEqual(remote(git), [])
                self.assertEqual(self.cache.read_bytes(), kept)

    def test_a_stale_answer_of_the_wrong_shape_is_still_replaced_as_before(self):
        self.keep({"date": "2020-01-01", "root": str(self.d), "lines": None})
        self.asks_again()

    def test_force_asks_again_over_a_wrong_shape_and_replaces_it(self):
        self.keep([])
        lines = self.asks_again(force=True)
        self.assertEqual(lines[0], "update: up to date")
        self.assertEqual(json.loads(self.cache.read_text())["lines"], lines)

    def test_a_kept_answer_that_will_not_parse_is_asked_again_and_the_readme_says_so(self):
        for text in ("{ not json", ""):  # a write cut short, or one that never started
            with self.subTest(text):
                self.keep(text)
                self.asks_again()
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text()
        para = next(p for p in readme.split("\n\n") if p.startswith("`update` reports"))
        self.assertIn("text that is not JSON", para)
        self.assertIn("asked again and replaced", para)

    def test_every_hint_names_the_command_the_owner_types(self):
        self.keep([])
        msg, _ = self.refuses("`example update --force`", command="example")
        self.assertNotIn("somecrew update", msg)
        self.cache.unlink()
        self.keep({**self.key, "lines": ["update: up to date"]})
        again = update.check(self.d, "somecrew", cache=self.cache, today=self.today, git=fake_git(),
                             command="example")
        self.assertIn("`example update --force` asks again", again[-1])
        lines = update.check(self.d, "somecrew", git=fake_git(remote_head="b" * 40), command="example")
        self.assertIn("then `example install`", lines[1])
        self.assertIn("run the tests, `example install`", lines[2])
        unfetched = update.check(self.d, "somecrew", git=fake_git(remote_head="b" * 40, known=False),
                                 command="example")
        self.assertIn("then `example update --force` tells which", unfetched[1])
        self.assertNotIn("somecrew install", "\n".join(lines))
        self.assertNotIn("somecrew update", "\n".join(unfetched))

    def test_the_command_line_names_the_crews_launcher_or_crewkit_in_its_clone(self):
        home = self.d / "home"
        kept = home / ".config/examplecrew/update-check.json"
        kept.parent.mkdir(parents=True)
        base = '[project]\nname = "examplecrew"\n[tool.crewkit]\nskills = ["s"]\n'
        for extra, hint in (('bin = ["example"]\n', "`example update --force`"),
                            ("", f"`uv run --directory {self.d.resolve()} crewkit update --force`")):
            with self.subTest(hint):
                kept.write_text("[]")
                (self.d / "pyproject.toml").write_text(base + extra)
                out = self.cli_update(home)
                self.assertIn(hint, out)
                self.assertNotIn("examplecrew update", out)

    def test_a_clone_path_with_braces_or_a_space_gives_the_line_and_a_hint_a_shell_reads_back(self):
        home = self.d / "home"
        kept = home / ".config/somecrew/update-check.json"
        kept.parent.mkdir(parents=True)
        for folder in ("c{x}", "my clone"):
            with self.subTest(folder):
                root = self.d / folder
                root.mkdir()
                (root / "pyproject.toml").write_text('[project]\nname = "somecrew"\n[tool.crewkit]\nskills = ["s"]\n')
                kept.write_text("[]")
                out = self.cli_update(home, root)
                self.assertEqual(len(out.strip().splitlines()), 1, out)
                hint = re.search(r"run `([^`]+)`", out)[1]
                self.assertEqual(shlex.split(hint), ["uv", "run", "--directory", str(root.resolve()), "crewkit",
                                                     "update", "--force"])

    def test_a_pull_asks_again_in_a_real_clone(self):
        run, remote, clone = real_clone(self.d)
        push_elsewhere(run, remote)
        run("fetch", "-q", cwd=clone)
        cache = self.d / "kept.json"
        behind = update.check(clone, "somecrew", cache=cache, today=self.today)[1]
        self.assertEqual(behind, f"somecrew clone: behind; `git -C '{clone}' pull --ff-only`, then `somecrew install`")
        run("pull", "-q", "--ff-only", cwd=clone)
        self.assertEqual(update.check(clone, "somecrew", cache=cache, today=self.today),
                         ["update: up to date", "somecrew clone: current"])

    def test_python_m_crewkit_install_stops_with_the_same_line_not_a_traceback(self):
        home = self.d / "home"
        kept = home / ".config/somecrew/update-check.json"
        kept.parent.mkdir(parents=True)
        kept.write_text("[]")
        (self.d / "pyproject.toml").write_text('[project]\nname = "somecrew"\n[tool.crewkit]\nskills = ["s"]\n')
        src = Path(install.__file__).resolve().parents[1]  # the copy under test, not one installed elsewhere
        r = subprocess.run([sys.executable, "-m", "crewkit.install", "update"], cwd=self.d, capture_output=True,
                           text=True, env={**os.environ, "HOME": str(home), "PYTHONPATH": str(src)})
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1, r.stderr)
        self.assertTrue(r.stderr.startswith(f"crewkit: {kept} holds a list"), r.stderr)
        self.assertEqual(kept.read_text(), "[]")

    def test_a_kept_answer_that_cannot_be_read_stops_with_one_line(self):
        self.keep({**self.key, "lines": ["update: up to date"]})
        self.cache.chmod(0)
        self.addCleanup(self.cache.chmod, 0o600)
        msg, git = self.refuses("cannot be read (")
        self.assertIn("Permission denied", msg)
        self.assertEqual(remote(git), [])

    def test_an_answer_that_cannot_be_kept_still_gives_the_report(self):
        with mock.patch.object(Path, "write_text", side_effect=PermissionError(13, "Permission denied")):
            lines = update.check(self.d, "somecrew", cache=self.cache, today=self.today, git=fake_git(tags=("v0.10.1",)))
        self.assertEqual(lines[0], "update: up to date")
        self.assertIn("not kept", lines[-1])
        self.assertFalse(self.cache.exists())

    def test_a_remote_without_the_tracked_branch_stops_with_one_line(self):
        self.refuses("origin has no branch main", git=fake_git(branch=False))
        self.assertFalse(self.cache.exists())

    def test_a_deleted_upstream_branch_in_a_real_clone(self):
        run, remote, clone = real_clone(self.d)
        run("branch", "-q", "feature", cwd=clone)
        run("push", "-q", "-u", "origin", "feature", cwd=clone)
        run("checkout", "-q", "feature", cwd=clone)
        run("update-ref", "-d", "refs/heads/feature", cwd=remote)  # the remote branch goes; the tracking ref stays
        self.assertIn("origin/feature", run("branch", "-r", cwd=clone).stdout)
        (clone / "pyproject.toml").write_text(PYPROJECT)
        cache = self.d / "kept.json"
        with self.assertRaises(install.InstallError) as c:
            update.check(clone, "somecrew", cache=cache, today=self.today)
        self.assertEqual(str(c.exception), f"origin has no branch feature, which {clone} tracks; point the clone at "
                         f"another (`git -C '{clone}' branch --set-upstream-to=<remote>/<branch>`) and run update again")
        self.assertFalse(cache.exists())

    def test_the_command_line_exits_1_with_that_line_and_writes_nothing(self):
        home = self.d / "home"
        kept = home / ".config/somecrew/update-check.json"
        kept.parent.mkdir(parents=True)
        (self.d / "pyproject.toml").write_text('[project]\nname = "somecrew"\n[tool.crewkit]\nskills = ["s"]\n')
        kept.write_text(json.dumps({"date": datetime.date.today().isoformat(), "root": str(self.d.resolve()),
                                    "lines": None}))
        before = kept.read_bytes()
        out = self.cli_update(home)
        self.assertEqual(len(out.strip().splitlines()), 1, out)
        self.assertIn("crewkit: ", out)
        self.assertIn(str(kept), out)
        self.assertEqual(kept.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
