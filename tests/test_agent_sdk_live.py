"""The real bundled CLI and, when configured, the real gateway: zero prompts, nothing billed.

Runs only under CREWKIT_LIVE=1; the Databricks leg also needs CREW_DATABRICKS_PROFILE and _SERVICE.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from agentsdkcase import CLI_VERSION, account, fixture

LIVE = os.environ.get("CREWKIT_LIVE") == "1"
try:
    from claude_agent_sdk import ClaudeAgentOptions
except ModuleNotFoundError as e:
    if (e.name or "").split(".")[0] != "claude_agent_sdk":
        raise
    LIVE = False
live = unittest.skipUnless(LIVE, "set CREWKIT_LIVE=1 with the agent-sdk extra")

from crewkit import agent_sdk

RERECORD = (
    "the CLI's account shape changed: record the shapes this CLI reports under tests/fixtures/accounts/<version>/ "
    "(see tests/fixtures/README.md) and rerun CREWKIT_LIVE=1 uv run --extra agent-sdk python -m unittest "
    "discover -s tests -p test_agent_sdk_live.py"
)
KEY_PROBE = """
import asyncio, json, os, tempfile
from claude_agent_sdk import ClaudeAgentOptions
from crewkit import agent_sdk
async def main():
    options = ClaudeAgentOptions(tools=[], setting_sources=[], strict_mcp_config=True, permission_mode="dontAsk",
                                 max_budget_usd=0.01, cwd=tempfile.mkdtemp())
    try:
        async with agent_sdk.connect(options, backend="api", route="direct") as (_c, v):
            print(json.dumps({"reason": v.reason, "keys": v.account_keys}))
    except agent_sdk.AgentSdkError as err:
        print(json.dumps({"reason": err.reason, "keys": err.verdict.account_keys if err.verdict else None}))
asyncio.run(main())
"""


def options(**kw) -> "ClaudeAgentOptions":
    return ClaudeAgentOptions(tools=[], setting_sources=[], strict_mcp_config=True, permission_mode="dontAsk",
                              cwd=tempfile.mkdtemp(prefix="crewkit-live-"), **kw)


async def connect_once(**kw) -> agent_sdk.Verdict:
    try:
        async with agent_sdk.connect(options(max_budget_usd=0.01), **kw) as (_client, verdict):
            return verdict
    except agent_sdk.Refused as err:
        return err.verdict


@live
class LiveAccount(unittest.TestCase):
    def test_live_account(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CREW_"))}
        with mock.patch.dict(os.environ, env, clear=True):
            plan = asyncio.run(connect_once(backend="plan", route="direct"))
            os.environ["ANTHROPIC_API_KEY"] = "sk-ant-dummy-never-used"
            api = asyncio.run(connect_once(backend="api", route="direct"))
        self.assertEqual(plan.cli_version, CLI_VERSION, RERECORD)
        self.assertTrue(plan.allowed, f"{plan.reason} {plan.account_keys}: {RERECORD}")
        self.assertEqual(plan.account_keys, tuple(sorted(account("subscription"))), RERECORD)
        self.assertTrue(api.allowed, f"{api.reason} {api.account_keys}: {RERECORD}")
        self.assertEqual(api.account_keys, tuple(sorted(account("api-key"))), RERECORD)
        probe_env = {**env, "ANTHROPIC_API_KEY": "sk-ant-dummy-never-used"}
        for run in range(10):
            out = subprocess.run([sys.executable, "-c", KEY_PROBE], env=probe_env, capture_output=True, text=True,
                                 timeout=60, check=False)
            got = json.loads(out.stdout.strip().splitlines()[-1])
            self.assertEqual(got["reason"], "ok", f"cold key probe {run}: {got}")


@live
@unittest.skipUnless(os.environ.get("CREW_DATABRICKS_PROFILE") and os.environ.get("CREW_DATABRICKS_SERVICE"),
                     "set CREW_DATABRICKS_PROFILE and CREW_DATABRICKS_SERVICE")
class LiveDatabricks(unittest.TestCase):
    def test_live_databricks(self):
        from crewkit import databricks_gateway as dg

        env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_")}
        env.update(CREW_LLM_BACKEND="plan", CREW_LLM_ROUTE="databricks-gateway")
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(dg, "_get", wraps=dg._get) as get:
            verdict = asyncio.run(connect_once())
        self.assertEqual(get.call_count, 2, "the relayed read and the models read")
        self.assertTrue(verdict.allowed, verdict.reason)
        self.assertEqual((verdict.route, verdict.databricks_service), ("databricks-gateway", env["CREW_DATABRICKS_SERVICE"]))
        self.assertEqual(verdict.account_keys, tuple(sorted(account("subscription"))), RERECORD)
        header, host = dg._fetch(env["CREW_DATABRICKS_PROFILE"])
        status, body = dg._get(host + dg.SERVICES_PATH + env["CREW_DATABRICKS_SERVICE"], {"Authorization": str(header)}, 10.0)
        self.assertEqual(status, 200)
        recorded = paths(fixture("provider-service-relayed"))
        missing = recorded - paths(json.loads(body))
        self.assertFalse(missing, f"the provider service body lost {sorted(missing)}; re-record tests/fixtures/databricks/")


def paths(found: object, prefix: str = "") -> set[str]:
    if not isinstance(found, dict):
        return set()
    out = set()
    for key, value in found.items():
        out.add(prefix + key)
        out |= paths(value, prefix + key + ".")
    return out


if __name__ == "__main__":
    unittest.main()
