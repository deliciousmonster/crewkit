"""The README's Quickstart block, run as written against the fake CLI with an exported key."""

import contextlib
import io
import os
import re
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:  # the agent-sdk extra; the core suite runs without it
    import claude_agent_sdk
    from agentsdkprocess import Fake
except ModuleNotFoundError as e:
    if (e.name or "").split(".")[0] != "claude_agent_sdk":
        raise
    Fake = None

README = Path(__file__).resolve().parents[1] / "README.md"


def quickstart() -> str:
    text = README.read_text(encoding="utf-8")
    section = text[text.index("## Quickstart"):]
    return re.search(r"```python\n(.*?)```", section, re.S).group(1)


@unittest.skipIf(Fake is None, "needs the agent-sdk extra: uv run --extra agent-sdk")
class Quickstart(unittest.TestCase):
    def run_block(self, account: str) -> str:
        fake = Fake(self)
        real = claude_agent_sdk.ClaudeAgentOptions

        def options(**kw):
            # the block's own options, plus where the fake lives and which account it reports
            return real(**kw, cli_path=str(fake.cli), env={"FAKE_LOG": str(fake.log), "FAKE_ACCOUNT": account})

        env = {"PATH": os.environ["PATH"], "HOME": tempfile.gettempdir(), "ANTHROPIC_API_KEY": "sk-ant-readme"}
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(claude_agent_sdk, "ClaudeAgentOptions", options), \
                contextlib.redirect_stdout(out):
            exec(compile(quickstart(), str(README), "exec"), {"__name__": "readme"})
        return out.getvalue()

    def test_quickstart_runs(self):
        self.assertEqual(self.run_block("api-key"), "api direct 2.1.326\nfake: Name one prime number.\n")

    def test_quickstart_refusal(self):
        self.assertEqual(self.run_block("subscription"), "not run: subscription_in_api_mode\n")


def sdlc_block() -> list[list[str]]:
    text = README.read_text(encoding="utf-8")
    block = re.search(r"```sh\n(.*?)```", text[text.index("## The agent SDLC"):], re.S).group(1)
    return [shlex.split(line, comments=True) for line in block.splitlines() if line.strip()]


class SdlcBlock(unittest.TestCase):
    """The README's sdlc lines, run as written in an empty folder; core only."""

    def test_the_lines_run(self):
        from crewkit.install import main
        from installcase import scratch

        lines = sdlc_block()
        self.assertEqual([a[:2] for a in lines], [["crewkit", "sdlc"]] * 3)
        with contextlib.chdir(scratch(self)) as _, contextlib.redirect_stdout(io.StringIO()) as out:
            codes = [main(a[1:]) for a in lines]
        self.assertEqual(codes, [0, 0, 0], out.getvalue())
        self.assertIn("stage brief", out.getvalue())


if __name__ == "__main__":
    unittest.main()
