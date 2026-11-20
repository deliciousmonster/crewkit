"""crewkit.agent_sdk.connect against a fake CLI: the gate, the key handoff and the child's environment."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from agentsdkcase import ACCOUNTS, ALLOWLIST, PARENT_ENV

try:  # the agent-sdk extra; the core suite runs without it
    from agentsdkprocess import Fake, children
    from claude_agent_sdk import HookMatcher
except ModuleNotFoundError as e:
    if (e.name or "").split(".")[0] != "claude_agent_sdk":
        raise
    Fake = None
needs_sdk = unittest.skipIf(Fake is None, "needs the agent-sdk extra: uv run --extra agent-sdk")

from crewkit.agent_sdk import Refused, Unavailable, connect

SDK_OWN = {"CLAUDE_CODE_ENTRYPOINT", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_SDK_READS_SESSION_STATE"}
FAKE_VARS = {"FAKE_LOG", "FAKE_ACCOUNT", "FAKE_MODE"}
# Set by the SDK only when it can name the calling app, and by darwin at every exec.
OPTIONAL = {"CLAUDE_AGENT_SDK_DERIVED_CLIENT_APP", "__CF_USER_TEXT_ENCODING"}


def parent_env(**extra: str) -> dict:
    return {**PARENT_ENV, "PATH": os.environ["PATH"], "TMPDIR": tempfile.gettempdir(), **extra}


@needs_sdk
class Gate(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = Fake(self)

    async def refused(self, options, **kw) -> str:
        ran = False
        with self.assertRaises(Refused) as caught:
            async with connect(options, **kw):
                ran = True
        self.assertFalse(ran, "the caller's body ran")
        self.assertNotIn("user", self.fake.frames())
        return caught.exception.reason

    async def test_gate_before_prompt(self):
        reason = await self.refused(self.fake.options("bearer"), backend="plan")
        self.assertEqual(reason, "token_present")
        self.assertIn("control_request", self.fake.frames())

    async def test_gate_reverse(self):
        reason = await self.refused(self.fake.options("subscription", max_budget_usd=1.0), backend="api",
                                    key_file=self.key("sk-ant-test"))
        self.assertEqual(reason, "subscription_in_api_mode")

    async def test_gate_none(self):
        reason = await self.refused(self.fake.options(mode="noaccount"), backend="plan")
        self.assertEqual(reason, "account_missing")

    async def test_fake_contract(self):
        recorded = json.loads((ACCOUNTS / "initialize.json").read_text(encoding="utf-8"))
        read_by_sdk_or_gate = {"account", "claude_code_version", "hook_on_failure_supported"}
        self.assertLessEqual(read_by_sdk_or_gate, set(recorded["keys"]))

        async def hook(*_args):
            return {}

        options = self.fake.options(hooks={"PreToolUse": [HookMatcher(hooks=[hook], on_failure="block")]})
        async with connect(options, backend="plan") as (_client, verdict):
            self.assertTrue(verdict.allowed)
        answered = next(e["initialize"] for e in self.fake.entries() if "initialize" in e)
        self.assertLessEqual(read_by_sdk_or_gate, set(answered))
        self.assertLessEqual(set(answered), set(recorded["keys"]))

    def key(self, text: str) -> Path:
        path = self.fake.dir / "key"
        path.write_text(text, encoding="utf-8")
        return path


@needs_sdk
class KeyAndOptions(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = Fake(self)
        self.key = self.fake.dir / "key"

    async def api(self, env_key: str | None, file_text: str | None) -> dict:
        """Connect in `api` with the key exported, filed, or both; the parent's environment after."""
        env = {k: v for k, v in parent_env().items() if k != "ANTHROPIC_API_KEY"}
        if env_key is not None:
            env["ANTHROPIC_API_KEY"] = env_key
        if file_text is not None:
            self.key.write_text(file_text, encoding="utf-8")
        with mock.patch.dict(os.environ, env, clear=True):
            async with connect(self.fake.options("api-key", max_budget_usd=1.0), backend="api", key_file=self.key):
                self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
            return dict(os.environ)

    async def test_key_handoff(self):
        after = await self.api("sk-ant-exported", None)
        self.assertNotIn("ANTHROPIC_API_KEY", after)
        self.assertEqual(self.fake.child_env()["ANTHROPIC_API_KEY"], "sk-ant-exported")
        self.fake.log.unlink()
        after = await self.api(None, "sk-ant-from-file\n")
        self.assertNotIn("ANTHROPIC_API_KEY", after)
        self.assertEqual(self.fake.child_env()["ANTHROPIC_API_KEY"], "sk-ant-from-file")
        for env_key, text in ((None, "  \n"), ("", "sk-ant-unused")):
            with self.subTest(env_key=env_key), self.assertRaises(Refused) as caught:
                await self.api(env_key, text)
            self.assertEqual(caught.exception.reason, "empty_key")

    async def test_key_kept_when_unavailable(self):
        env = {**parent_env(), "ANTHROPIC_API_KEY": "sk-ant-exported"}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(Unavailable):
                async with connect(self.fake.options("api-key", mode="exit1", max_budget_usd=1.0), backend="api",
                                   timeout_s=10):
                    pass
            self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), "sk-ant-exported")
            with self.assertRaises(Refused) as caught:
                async with connect(self.fake.options("subscription", max_budget_usd=1.0), backend="api"):
                    pass
            self.assertEqual(caught.exception.reason, "subscription_in_api_mode")
            self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), "sk-ant-exported")
            async with connect(self.fake.options("api-key", max_budget_usd=1.0), backend="api"):
                pass
            self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
        self.assertEqual(children(), [])

    async def test_options_refused(self):
        cases = {
            "tools": {"tools": None},
            "setting_sources": {"setting_sources": None},
            "strict_mcp_config": {"strict_mcp_config": False},
            "permission_mode": {"permission_mode": "bypassPermissions"},
            "env": {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9"}},
            "settings": {"settings": "/tmp/settings.json"},
            "extra_args": {"extra_args": {"settings": '{"env": {}}'}},
        }
        cases_more = [("settings", {"settings": '{"env": {"ANTHROPIC_BASE_URL": "x"}}'}),
                      ("extra_args", {"extra_args": {"debug": None}}),
                      ("tools", {"tools": {"type": "preset", "preset": "claude_code"}})]
        for field, kw in [*cases.items(), *cases_more]:
            with self.subTest(field=field, kw=kw), self.assertRaises(Refused) as caught:
                async with connect(self.fake.options(**kw), backend="plan"):
                    pass
            self.assertEqual(caught.exception.reason, f"options_unsafe:{field}")
        self.key.write_text("sk-ant-test", encoding="utf-8")
        with self.assertRaises(Refused) as caught:
            async with connect(self.fake.options("api-key"), backend="api", key_file=self.key):
                pass
        self.assertEqual(caught.exception.reason, "options_unsafe:max_budget_usd")
        self.assertFalse(self.fake.log.exists(), "a refused option spawned the CLI")


@needs_sdk
class ChildProcess(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = Fake(self)

    async def test_child_sees_allowlist(self):
        seeded = parent_env()
        with mock.patch.dict(os.environ, seeded, clear=True):
            async with connect(self.fake.options(), backend="plan"):
                pass
            after = dict(os.environ)
        child = set(self.fake.child_env())
        expected = ALLOWLIST | SDK_OWN | FAKE_VARS | {"CLAUDE_CODE_SKIP_PROMPT_HISTORY"}
        self.assertEqual(child - OPTIONAL, expected)
        self.assertFalse([n for n in child if n.startswith("ANTHROPIC_")])
        self.assertEqual(after, {k: v for k, v in seeded.items() if k != "ANTHROPIC_API_KEY"})
        self.fake.log.unlink()
        with mock.patch.dict(os.environ, seeded, clear=True):
            async with connect(self.fake.options("api-key", max_budget_usd=1.0), backend="api"):
                self.assertNotIn("ANTHROPIC_API_KEY", os.environ)
        child = self.fake.child_env()
        self.assertEqual(set(child) - OPTIONAL, expected | {"ANTHROPIC_API_KEY"})
        self.assertEqual(child["ANTHROPIC_API_KEY"], "sk-ant-parent-canary")

    async def test_retry_count_reaches_child(self):
        with mock.patch.dict(os.environ, parent_env(CLAUDE_CODE_MAX_RETRIES="3"), clear=True):
            options = self.fake.options(env={"CLAUDE_CODE_MAX_RETRIES": "2"})
            async with connect(options, backend="plan"):
                pass
        self.assertEqual(self.fake.child_env()["CLAUDE_CODE_MAX_RETRIES"], "2")
        self.fake.log.unlink()
        with mock.patch.dict(os.environ, parent_env(CLAUDE_CODE_MAX_RETRIES="3"), clear=True):
            async with connect(self.fake.options(), backend="plan", keep_env=["CLAUDE_CODE_MAX_RETRIES"]):
                pass
        self.assertEqual(self.fake.child_env()["CLAUDE_CODE_MAX_RETRIES"], "3")

    async def test_connect_timeout(self):
        start = time.monotonic()
        with self.assertRaises(Unavailable) as caught:
            async with connect(self.fake.options(mode="hang"), backend="plan", timeout_s=1):
                pass
        self.assertEqual(caught.exception.reason, "connect_timeout")
        self.assertLess(time.monotonic() - start, 7)
        self.assertEqual(children(), [])

    async def test_cli_exits(self):
        for mode in ("exit1", "kill"):
            with self.subTest(mode=mode), self.assertRaises(Unavailable) as caught:
                async with connect(self.fake.options(mode=mode), backend="plan", timeout_s=10):
                    pass
            self.assertIn(caught.exception.reason, ("cli_failed:ProcessError", "cli_failed:CLIConnectionError"))
            self.assertEqual(children(), [])


if __name__ == "__main__":
    unittest.main()
