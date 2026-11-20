"""crewkit.agent_sdk without the SDK: resolve, route, check and the environment window."""

import asyncio
import importlib.util
import itertools
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from agentsdkcase import ACCOUNTS, ALLOWLIST, CLI_VERSION, PARENT_ENV, account

from crewkit import agent_sdk, llm
from crewkit.agent_sdk import Refused, Unavailable, check, resolve, resolve_route

NO_FILE = Path("/nonexistent/crewkit-key")
DB = {"CREW_DATABRICKS_PROFILE": "example", "CREW_DATABRICKS_SERVICE": "main.default.my_service"}


def key_file(test: unittest.TestCase, text: str) -> Path:
    with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as f:
        f.write(text)
    test.addCleanup(os.unlink, f.name)
    return Path(f.name)


class Resolve(unittest.TestCase):
    def resolve(self, env: dict, file: Path = NO_FILE, **kw) -> str:
        with mock.patch.dict(os.environ, env, clear=True):
            return resolve(file, **kw)

    def refused(self, env: dict, file: Path = NO_FILE, **kw) -> str:
        with self.assertRaises(Refused) as caught:
            self.resolve(env, file, **kw)
        return caught.exception.reason

    def test_resolve(self):
        self.assertEqual(self.resolve({"ANTHROPIC_API_KEY": "k"}), "api")
        self.assertEqual(self.resolve({}, key_file(self, "k")), "api")
        for env in ({}, {"CREW_LLM_BACKEND": "auto"}):
            self.assertEqual(self.refused(env), "plan_not_pinned")
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertEqual(llm.backend(NO_FILE), "plan")
        self.assertEqual(self.resolve({"CREW_LLM_BACKEND": "plan", "ANTHROPIC_API_KEY": "k"}), "plan")
        self.assertEqual(self.resolve({}, backend="plan"), "plan")
        self.assertEqual(self.refused({"ANTHROPIC_API_KEY": ""}), "plan_not_pinned")
        self.assertEqual(self.resolve({}, key_file(self, "  \n")), "api")
        with mock.patch.dict(os.environ, {}, clear=True), self.assertRaises(Refused) as caught:
            agent_sdk.read_key(key_file(self, "  \n"))
        self.assertEqual(caught.exception.reason, "empty_key")

    def test_pick_is_shared(self):
        files = (NO_FILE, key_file(self, "  \n"))
        for asked, env_key, file in itertools.product(("auto", "api", "plan"), (None, "", "k"), files):
            env = {"CREW_LLM_BACKEND": asked, **({} if env_key is None else {"ANTHROPIC_API_KEY": env_key})}
            with mock.patch.dict(os.environ, env, clear=True):
                picked = llm.backend(file)
                try:
                    got = resolve(file)
                except Refused as err:
                    self.assertEqual((err.reason, picked, asked), ("plan_not_pinned", "plan", "auto"))
                    continue
            self.assertEqual(got, picked, (asked, env_key, file))
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=True):
            self.assertEqual(llm.backend(NO_FILE), "plan")

    def test_resolve_error(self):
        try:
            self.resolve({"CREW_LLM_BACKEND": "cloud"})
        except Exception as err:  # noqa: BLE001 - the point: an ordinary Exception, never SystemExit
            self.assertIsInstance(err, Refused)
            self.assertEqual(err.reason, "backend_invalid")
        else:
            self.fail("a bad CREW_LLM_BACKEND resolved")


class ResolveRoute(unittest.TestCase):
    def route(self, env: dict, backend: str = "plan", *, asked: bool = True, **kw) -> str:
        with mock.patch.dict(os.environ, env, clear=True):
            try:
                return resolve_route(backend, asked_backend=asked, **kw)
            except Refused as err:
                return "refused:" + err.reason

    def test_resolve_route(self):
        gw = {"CREW_LLM_ROUTE": "databricks-gateway", **DB}
        self.assertEqual(self.route({}), "direct")
        self.assertEqual(self.route(gw), "databricks-gateway")
        self.assertEqual(self.route(gw, asked=False), "refused:route_needs_backend")
        self.assertEqual(self.route(gw, "api"), "refused:route_not_built:api")
        for name in DB:
            self.assertEqual(self.route({k: v for k, v in gw.items() if k != name}),
                             "refused:databricks_gateway_unconfigured")
        self.assertEqual(self.route({"CREW_LLM_ROUTE": "gateway"}), "refused:route_invalid")
        self.assertEqual(self.route({"CREW_LLM_ROUTE": ""}), "refused:route_invalid")
        self.assertEqual(self.route(gw, route="direct"), "direct")
        backend_env = {"CREW_LLM_BACKEND": "gateway"}
        with mock.patch.dict(os.environ, backend_env, clear=True), self.assertRaises(Refused) as caught:
            resolve(NO_FILE)
        self.assertEqual(caught.exception.reason, "backend_invalid")
        self.assertEqual(llm.BACKENDS, ("auto", "api", "plan"))
        for env in ({}, {"ANTHROPIC_API_KEY": "k"}, {"CREW_LLM_BACKEND": "plan"}):
            with mock.patch.dict(os.environ, env, clear=True):
                plain = llm.backend(NO_FILE)
            with mock.patch.dict(os.environ, {**env, **gw}, clear=True):
                self.assertEqual(llm.backend(NO_FILE), plain)


def verdict(name: str, backend: str, version: object = CLI_VERSION):
    return check(account(name), backend, version)


class CheckPlan(unittest.TestCase):
    def test_check_plan(self):
        self.assertTrue(verdict("subscription", "plan").allowed)
        refusals = {
            "api-key": "api_key_present", "key-and-token": "api_key_present", "no-signin-key": "api_key_present",
            "bearer": "token_present", "bedrock": "provider:bedrock", "vertex": "provider:vertex",
            "foundry": "provider:foundry", "anthropic-aws": "provider:anthropicAws",
            "empty-config-dir": "account_shape", "no-signin": "account_shape",
        }
        self.assertEqual({p.stem for p in ACCOUNTS.glob("*.json")} - {"initialize"}, {"subscription", *refusals})
        for name, reason in refusals.items():
            with self.subTest(name):
                got = verdict(name, "plan")
                self.assertEqual((got.allowed, got.reason), (False, reason))
        sub = account("subscription")
        for bad, reason in ((None, "account_missing"), ({}, "account_shape"), ([], "account_shape"),
                            ({**sub, "subscriptionType": ""}, "not_subscription"),
                            ({**sub, "tokenSource": "claude.ai"}, "token_present"),
                            ({**sub, "extra": 1}, "account_shape"),
                            ({k: v for k, v in sub.items() if k != "organization"}, "account_shape")):
            with self.subTest(bad=bad):
                got = check(bad, "plan", CLI_VERSION)
                self.assertEqual((got.allowed, got.reason), (False, reason))
        shaped = check({**sub, "extra": "value-canary"}, "plan", CLI_VERSION)
        self.assertEqual(shaped.account_keys, ("apiProvider", "email", "extra", "organization", "subscriptionType"))
        self.assertNotIn("value-canary", repr(shaped))
        self.assertNotIn("user@example.com", repr(verdict("subscription", "plan")))


class CheckApi(unittest.TestCase):
    def test_check_api(self):
        for name in ("api-key", "no-signin-key"):
            self.assertTrue(verdict(name, "api").allowed, name)
        self.assertTrue(check({"apiKeySource": "ANTHROPIC_API_KEY", "apiProvider": "firstParty"}, "api",
                              CLI_VERSION).allowed)
        self.assertEqual(verdict("subscription", "api").reason, "subscription_in_api_mode")
        self.assertEqual(verdict("key-and-token", "api").reason, "token_present")
        self.assertEqual(verdict("bearer", "api").reason, "account_shape")
        self.assertEqual(verdict("no-signin", "api").reason, "account_shape")
        self.assertEqual(verdict("bedrock", "api").reason, "provider:bedrock")
        key = account("api-key")
        for token in ("", "None", "ANTHROPIC_AUTH_TOKEN"):
            self.assertEqual(check({**key, "tokenSource": token}, "api", CLI_VERSION).reason, "token_present")
        self.assertEqual(check({**key, "apiKeySource": "apiKeyHelper"}, "api", CLI_VERSION).reason, "account_shape")

    def test_check_version(self):
        for bad in (None, "", 2):
            got = verdict("subscription", "plan", bad)
            self.assertEqual((got.allowed, got.reason, got.cli_version), (False, "cli_version_unknown", None))
        self.assertEqual(verdict("subscription", "plan").cli_version, CLI_VERSION)


class ChildEnv(unittest.TestCase):
    def test_child_env(self):
        with mock.patch.dict(os.environ, PARENT_ENV, clear=True):
            with agent_sdk._ReducedEnv(["EDITOR"]):
                inside = set(os.environ)
            after = dict(os.environ)
        self.assertEqual(inside, ALLOWLIST | {"EDITOR"})
        self.assertEqual(after, {k: v for k, v in PARENT_ENV.items() if k != "ANTHROPIC_API_KEY"})

    def test_keep_env_forbidden(self):
        for name in ("ANTHROPIC_BASE_URL", "CLAUDE_CONFIG_DIR", "HTTPS_PROXY", "https_proxy", "NODE_OPTIONS"):
            with self.assertRaises(Refused) as caught:
                agent_sdk.check_keep_env([name])
            self.assertEqual(caught.exception.reason, f"keep_env_forbidden:{name}")
        agent_sdk.check_keep_env(["EDITOR"])

    def test_retry_count_allowed(self):
        agent_sdk.check_keep_env(["CLAUDE_CODE_MAX_RETRIES"])
        self.assertFalse(agent_sdk.forbidden("CLAUDE_CODE_MAX_RETRIES"))
        for name in ("CLAUDE_CODE_MAX_RETRIES_X", "CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_MAX_RETRIES"):
            self.assertTrue(agent_sdk.forbidden(name), name)

    # _close imports anyio and the SDK; connect reaches it only after importing ClaudeSDKClient
    @unittest.skipIf(importlib.util.find_spec("claude_agent_sdk") is None,
                     "needs the agent-sdk extra: uv run --extra agent-sdk")
    def test_close_interrupts_first(self):
        calls: list[str] = []

        class Client:
            async def interrupt(self):
                calls.append("interrupt")
                raise OSError("pipe closed")

            async def disconnect(self):
                calls.append("disconnect")

        asyncio.run(agent_sdk._close(Client(), interrupt=True))
        self.assertEqual(calls, ["interrupt", "disconnect"])
        calls.clear()
        asyncio.run(agent_sdk._close(Client(), interrupt=False))
        self.assertEqual(calls, ["disconnect"])

    def test_errors_redacted(self):
        secrets = ("sk-ant-parent-canary", "user@example.com", "example-org")
        errors = [Refused(v.reason, v) for v in (verdict(n, "api") for n in ("subscription", "bedrock"))]
        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "   "}, clear=True):
            try:
                agent_sdk.read_key(None)
            except Refused as err:
                errors.append(err)
        errors.append(Unavailable("connect_timeout"))
        self.assertEqual(errors[-2].reason, "empty_key")
        for err in errors:
            for text in (str(err), repr(err), repr(err.verdict)):
                for secret in secrets:
                    self.assertNotIn(secret, text)
        self.assertEqual(str(errors[0]), "subscription_in_api_mode")


class KeyKeptOnRefusal(unittest.TestCase):
    def test_exported_key_survives_a_refusal(self):
        # every option check_options reads, set to pass, so the refusal comes from keep_env
        options = SimpleNamespace(tools=[], setting_sources=[], strict_mcp_config=True, permission_mode="dontAsk",
                                  env={}, settings=None, extra_args={}, max_budget_usd=1.0)

        async def attempt() -> str:
            try:
                async with agent_sdk.connect(options, backend="api", keep_env=["CLAUDE_CONFIG_DIR"]):
                    pass
            except Refused as err:
                return err.reason
            return "connected"

        with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-ant-exported"}, clear=True):
            self.assertEqual(asyncio.run(attempt()), "keep_env_forbidden:CLAUDE_CONFIG_DIR")
            self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), "sk-ant-exported")
            self.assertEqual(asyncio.run(attempt()), "keep_env_forbidden:CLAUDE_CONFIG_DIR")


if __name__ == "__main__":
    unittest.main()
