"""The scratch folder every test makes and removes, and the scratch HOME and crew clone every install, uninstall and
doctor test starts from."""
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crewkit import install as ik

PYPROJECT = """\
[project]
name = "democrew"
version = "0.1.0"

[tool.crewkit]
skills = ["demo", "demo-extra"]
repo = "owner/democrew"
hook = "echo {crew_path}"
permissions = ["Bash(democrew *)"]
agents_dir = "agents"
bin = ["democrew"]

[[tool.crewkit.hooks]]
event = "PreToolUse"
matcher = "Bash"
command = "guard {crew_path}"

[[tool.crewkit.prerequisites]]
name = "poppler"
binaries = ["pdftotext", "pdfinfo"]
level = "soft"
hint = "brew install poppler"

[[tool.crewkit.prerequisites]]
name = "Claude in Chrome"
hint = "install the extension by hand"
"""
THEIRS = {"matcher": "Bash", "hooks": [{"type": "command", "command": "their-guard"}]}  # another tool's hook group
ODD = 'my "clone" $HOME `x` it\'s'  # a folder a shell would split, expand or run if it were not quoted


def scratch(test: unittest.TestCase, prefix: str = "tmp") -> Path:
    """A new folder for one test, removed when the test ends."""
    d = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    test.addCleanup(shutil.rmtree, d)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = scratch(self, "home-")
        self.home = self.tmp / "home"
        self.home.mkdir()
        patch = mock.patch.dict(os.environ, {"HOME": str(self.home)})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("DEMOCREW_HOME", None)
        self.root = self.tmp / "clone" / "democrew"
        (self.root / "agents").mkdir(parents=True)
        (self.root / "pyproject.toml").write_text(PYPROJECT)
        for s in ("demo", "demo-extra"):
            (self.root / "skills" / s).mkdir(parents=True)
            (self.root / "skills" / s / "SKILL.md").write_text(f"# {s}\n")
        (self.root / "agents" / "a.md").write_text("see {crew_path}\n")
        self.spec = ik.load_spec(self.root)
        self.claude = self.home / ".claude"

    def run_install(self, **kw):
        return ik.install(self.spec, which=lambda b: "/bin/" + b, **kw)

    def snap(self):
        """Every path under HOME and what it holds, to show a refused command changed nothing."""
        return {str(p): os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else "dir"
                for p in sorted(self.home.rglob("*"))}

    def write_settings(self, data):
        """~/.claude/settings.json holding `data`: text as given, anything else as JSON."""
        self.claude.mkdir(parents=True, exist_ok=True)
        (self.claude / "settings.json").write_text(data if isinstance(data, str) else json.dumps(data))

    def failed_install(self) -> str:
        """`install` from the command line, which must exit 1; what it printed."""
        with mock.patch.object(ik.shutil, "which", lambda b: "/bin/" + b), mock.patch("sys.stderr") as err:
            self.assertEqual(ik.main(["install"], root=self.root), 1)
        return "".join(c.args[0] for c in err.write.call_args_list)

    def assert_linked(self):
        for s in ("demo", "demo-extra"):
            self.assertEqual(Path(os.path.realpath(self.claude / "skills" / s)), (self.root / "skills" / s).resolve())

    def clone_at(self, folder: str) -> Path:
        """Copy the crew clone to self.tmp/<folder>/democrew and point the spec at the copy; its root."""
        self.root = self.tmp / folder / "democrew"
        shutil.copytree(self.tmp / "clone" / "democrew", self.root)
        self.spec = ik.load_spec(self.root)
        return self.root

    def argv_printer(self, name: str) -> dict:
        """An environment whose PATH finds `name` first: a stand-in that prints each argument as [arg], one a line."""
        fake = self.tmp / "fakebin"
        fake.mkdir(exist_ok=True)
        (fake / name).write_text('#!/bin/sh\nfor a in "$@"; do printf "[%s]\\n" "$a"; done\n')
        (fake / name).chmod(0o755)
        return {**os.environ, "PATH": f"{fake}{os.pathsep}/usr/bin{os.pathsep}/bin"}

    def dangle(self, path, into="gone"):
        """Make `path` a link to a file in self.tmp/<into> that is not there."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(self.tmp / into / path.name)
