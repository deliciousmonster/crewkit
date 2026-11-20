"""connect on CREW_LLM_ROUTE=databricks-gateway against a fake CLI and a local stand-in for a workspace."""

import os
import tempfile
import unittest
from unittest import mock

from agentsdkcase import MODELS, SERVICES, Gateway, canary_jwt, fake_config

try:  # the agent-sdk and databricks extras; the core suite runs without them
    import databricks.sdk.core  # noqa: F401
    from agentsdkprocess import Fake
except ModuleNotFoundError as e:
    if (e.name or "").split(".")[0] not in ("claude_agent_sdk", "databricks"):
        raise
    Fake = None
needs_extras = unittest.skipIf(Fake is None, "needs the agent-sdk and databricks extras")

from crewkit.agent_sdk import Refused, connect
from crewkit.databricks_gateway import DatabricksGatewayUnavailable

SERVICE = "main.default.my_service"
GATEWAY_NAMES = ("ANTHROPIC_BASE_URL", "ANTHROPIC_CUSTOM_HEADERS")


@needs_extras
class GatewayRoute(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fake = Fake(self)
        self.gw = Gateway(self)
        self.token = canary_jwt(3600)
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CREW_"))}
        env.update({"CREW_LLM_BACKEND": "plan", "CREW_LLM_ROUTE": "databricks-gateway",
                    "CREW_DATABRICKS_PROFILE": "example", "CREW_DATABRICKS_SERVICE": SERVICE,
                    "TMPDIR": tempfile.gettempdir()})
        for patch in (mock.patch.dict(os.environ, env, clear=True),
                      mock.patch("databricks.sdk.core.Config", fake_config(self.gw.host, "Bearer " + self.token))):
            patch.start()
            self.addCleanup(patch.stop)

    def parent_clean(self):
        for name in GATEWAY_NAMES:
            self.assertNotIn(name, os.environ)
        self.assertNotIn(self.token, repr(dict(os.environ)))

    async def test_databricks_child_env(self):
        self.parent_clean()
        async with connect(self.fake.options()) as (_client, verdict):
            self.parent_clean()
        self.parent_clean()
        child = self.fake.child_env()
        self.assertEqual(child["ANTHROPIC_BASE_URL"], self.gw.host + "/ai-gateway/anthropic")
        self.assertEqual(child["ANTHROPIC_CUSTOM_HEADERS"].split("\n"),
                         [f"X-Databricks-AI-Gateway-Token: Bearer {self.token}",
                          f"Databricks-Model-Provider-Service: {SERVICE}"])
        self.assertEqual((verdict.route, verdict.databricks_service, verdict.backend), ("databricks-gateway", SERVICE, "plan"))
        for name in GATEWAY_NAMES:
            for kw in ({"keep_env": [name]}, {"options": self.fake.options(env={name: "http://127.0.0.1:9"})}):
                with self.subTest(name=name, kw=list(kw)), self.assertRaises(Refused):
                    async with connect(kw.pop("options", self.fake.options()), **kw):
                        pass

    async def test_databricks_before_spawn(self):
        failures = [
            ((SERVICES, 404), "databricks_gateway_service_unknown"),
            ((SERVICES, 401), "databricks_gateway_auth"),
            ((SERVICES, 503), "databricks_gateway_unreachable"),
            ((MODELS, 403), "databricks_gateway_auth"),
        ]
        for (path, status), reason in failures:
            with self.subTest(reason=reason, path=path):
                self.gw.serve_ok()
                self.gw.routes[path] = (status, b"{}", 0.0)
                with self.assertRaises(DatabricksGatewayUnavailable) as caught:
                    async with connect(self.fake.options()):
                        pass
                self.assertEqual(caught.exception.reason, reason)
                self.assertFalse(self.fake.log.exists(), "a child started before the preflight passed")
        self.gw.serve_ok()
        short = fake_config(self.gw.host, "Bearer " + canary_jwt(60))
        with mock.patch("databricks.sdk.core.Config", short), self.assertRaises(DatabricksGatewayUnavailable) as caught:
            async with connect(self.fake.options()):
                pass
        self.assertEqual(caught.exception.reason, "databricks_gateway_token_short")
        self.assertFalse(self.fake.log.exists())
        with self.assertRaises(Refused) as caught:
            async with connect(self.fake.options("bearer")):
                pass
        self.assertNotIsInstance(caught.exception, DatabricksGatewayUnavailable)
        self.assertEqual(caught.exception.reason, "token_present")

    async def test_databricks_key_shape(self):
        ran = False
        with self.assertRaises(Refused) as caught:
            async with connect(self.fake.options("api-key")):
                ran = True
        self.assertFalse(ran)
        self.assertEqual(caught.exception.reason, "api_key_present")
        self.assertNotIn("user", self.fake.frames())


if __name__ == "__main__":
    unittest.main()
