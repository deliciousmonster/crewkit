"""The fake CLI and options shared by the process tests; imports the SDK, so only those tests load it."""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

FAKE = Path(__file__).parent / "fakeclaude.py"


def fake_cli(folder: Path) -> Path:
    """An executable that runs the fake under this interpreter, whatever PATH the child gets."""
    cli = folder / "claude"
    cli.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(FAKE)!r}, run_name='__main__')\n",
                   encoding="utf-8")
    cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
    return cli


class Fake:
    def __init__(self, test: unittest.TestCase) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="crewkit-fake-"))
        test.addCleanup(shutil.rmtree, self.dir, True)
        self.cli = fake_cli(self.dir)
        self.log = self.dir / "fake.log"

    def options(self, account: str = "subscription", mode: str = "", **kw) -> ClaudeAgentOptions:
        env = {"FAKE_LOG": str(self.log), "FAKE_ACCOUNT": account, "FAKE_MODE": mode, **kw.pop("env", {})}
        base = {"tools": [], "setting_sources": [], "strict_mcp_config": True, "permission_mode": "dontAsk",
                "cli_path": str(self.cli), "env": env}
        return ClaudeAgentOptions(**{**base, **kw})

    def entries(self) -> list[dict]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def child_env(self) -> dict:
        return next(e["env"] for e in self.entries() if "env" in e)

    def frames(self) -> list[str]:
        return [e["frame"] for e in self.entries() if "frame" in e]


def children() -> list[str]:
    """This process's live children, by `pgrep -P`."""
    out = subprocess.run(["pgrep", "-P", str(os.getpid())], capture_output=True, text=True, check=False)
    return out.stdout.split()
