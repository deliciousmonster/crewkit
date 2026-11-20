"""crewkit.databricks_gateway.preflight against a local stand-in for a workspace; no token leaves the test."""

import asyncio
import io
import logging
import os
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from agentsdkcase import MODELS, SERVICES, Gateway, canary_jwt, fake_config, fixture

try:  # the databricks extra; the core suite runs without it
    import databricks.sdk.core  # noqa: F401
except ModuleNotFoundError as e:
    if (e.name or "").split(".")[0] != "databricks":
        raise
    HAS_DATABRICKS = False
else:
    HAS_DATABRICKS = True
needs_databricks = unittest.skipIf(not HAS_DATABRICKS, "needs the databricks extra: uv run --extra databricks")

from crewkit.databricks_gateway import (
    DatabricksGateway,
    DatabricksGatewayUnavailable,
    preflight,
)

SERVICE = "main.default.my_service"


def texts(err: BaseException) -> list[str]:
    """Every text an exception and its chain can print."""
    out = []
    while err is not None:
        out += [str(err), repr(err)]
        err = err.__cause__ or err.__context__
    return out


@needs_databricks
class Preflight(unittest.TestCase):
    def setUp(self):
        self.gw = Gateway(self)
        self.token = canary_jwt(3600)

    def run_preflight(self, config=None, *, timeout_s: float = 2.0) -> dict | str:
        config = config or fake_config(self.gw.host, "Bearer " + self.token)
        settings = DatabricksGateway("example", SERVICE, timeout_s=timeout_s)
        with mock.patch("databricks.sdk.core.Config", config):
            try:
                return asyncio.run(preflight(settings))
            except DatabricksGatewayUnavailable as err:
                for text in texts(err):
                    self.assertNotIn(self.token, text)
                return err.reason

    def test_databricks_token(self):
        host = self.gw.host
        cases = {
            "raises": fake_config(host, raises=RuntimeError("Bearer " + self.token)),
            "slow": fake_config(host, "Bearer " + self.token, delay_s=3.0),
            "not bearer": fake_config(host, "Basic " + self.token),
            "not a jwt": fake_config(host, "Bearer not-a-jwt"),
        }
        for name, config in cases.items():
            with self.subTest(name):
                start = time.monotonic()
                self.assertEqual(self.run_preflight(config, timeout_s=1.0), "databricks_gateway_token")
                self.assertLess(time.monotonic() - start, 2.0)
        short = fake_config(host, "Bearer " + canary_jwt(599))
        self.assertEqual(self.run_preflight(short), "databricks_gateway_token_short")
        env = self.run_preflight()
        self.assertEqual(env["ANTHROPIC_BASE_URL"], host + "/ai-gateway/anthropic")

    def test_databricks_preflight(self):
        relayed = fixture("provider-service-relayed")
        anthropic = relayed["config"]["anthropic"]
        for name, body in {
            "direct only": {**relayed, "config": {**relayed["config"], "anthropic": {"direct": {}}}},
            "both": {**relayed, "config": {**relayed["config"], "anthropic": {**anthropic, "direct": {}}}},
            "no anthropic": {**relayed, "config": {"allow_all_targets": True}},
            "not an object": [],
        }.items():
            with self.subTest(name):
                self.gw.serve_ok(service=body)
                self.assertEqual(self.run_preflight(), "databricks_gateway_not_relayed")
        cases = [
            (SERVICES, 404, "databricks_gateway_service_unknown"),
            (SERVICES, 401, "databricks_gateway_auth"),
            (MODELS, 403, "databricks_gateway_auth"),
            (SERVICES, 503, "databricks_gateway_unreachable"),
            (MODELS, 503, "databricks_gateway_unreachable"),
        ]
        for path, status, reason in cases:
            with self.subTest(path=path, status=status):
                self.gw.serve_ok()
                self.gw.routes[path] = (status, b"{}", 0.0)
                self.assertEqual(self.run_preflight(), reason)
        self.gw.serve_ok(models={"data": []})
        self.assertEqual(self.run_preflight(), "databricks_gateway_service_unknown")
        self.gw.serve_ok(models={})
        self.assertEqual(self.run_preflight(), "databricks_gateway_service_unknown")
        for path in (SERVICES, MODELS):
            self.gw.serve_ok()
            self.gw.routes[path] = (200, b"{}", 5.0)
            start = time.monotonic()
            self.assertEqual(self.run_preflight(timeout_s=1.0), "databricks_gateway_unreachable")
            self.assertLess(time.monotonic() - start, 1.0 * 2 + 1.0)
        self.gw.serve_ok()
        self.assertIsInstance(self.run_preflight(), dict)
        sent = {e["path"]: e["headers"] for e in self.gw.seen[-2:]}
        self.assertEqual(sent[SERVICES + SERVICE]["Authorization"], "Bearer " + self.token)
        self.assertEqual(sent[MODELS]["Databricks-Model-Provider-Service"], SERVICE)

    def test_redirect_not_followed(self):
        elsewhere = Gateway(self)
        for path in (SERVICES, MODELS):
            for status in (301, 302, 307, 308):
                with self.subTest(path=path, status=status):
                    self.gw.serve_ok()
                    self.gw.routes[path] = (status, b"", 0.0)
                    self.gw.locations[path] = elsewhere.host + path + SERVICE
                    self.assertEqual(self.run_preflight(), "databricks_gateway_unreachable")
        self.assertEqual(elsewhere.seen, [])

    def test_service_name_checked(self):
        config = mock.Mock(side_effect=AssertionError("token fetched for a bad service name"))
        for name in ("main.default", "main.default.svc.extra", "main.default.svc\nX-Example: 1",
                     "main.default.svc/../x", "main.default.svc?x=1", "main..svc", "", "main.default.svc "):
            with self.subTest(name=name):
                settings = DatabricksGateway("example", name)
                with mock.patch("databricks.sdk.core.Config", config):
                    with self.assertRaises(DatabricksGatewayUnavailable) as caught:
                        asyncio.run(preflight(settings))
                self.assertEqual(caught.exception.reason, "databricks_gateway_service_invalid")
        self.assertEqual(self.gw.seen, [])
        for name in (SERVICE, "workspace.default.my-service_2"):
            with self.subTest(name=name):
                with mock.patch("databricks.sdk.core.Config", fake_config(self.gw.host, "Bearer " + self.token)):
                    self.assertIsInstance(asyncio.run(preflight(DatabricksGateway("example", name))), dict)

    def test_databricks_redaction(self):
        records = io.StringIO()
        handler = logging.StreamHandler(records)
        root = logging.getLogger()
        old_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.removeHandler, handler)
        self.addCleanup(root.setLevel, old_level)
        with mock.patch.dict(os.environ, {"DATABRICKS_DEBUG_HEADERS": "true"}):
            self.gw.routes[SERVICES] = (401, b"{}", 0.0)
            self.assertEqual(self.run_preflight(), "databricks_gateway_auth")
            self.gw.serve_ok()
            self.gw.routes[MODELS] = (200, b"<html>not json</html>", 0.0)
            self.assertEqual(self.run_preflight(), "databricks_gateway_service_unknown")
        self.assertNotIn(self.token, records.getvalue())
        self.assertNotIn(self.token[-20:], records.getvalue())


PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


@unittest.skipIf(not PYPROJECT.exists(), "runs from the source tree")
class Extras(unittest.TestCase):
    def test_extras_carry_anyio(self):
        # the gateway calls to_thread.run_sync(abandon_on_cancel=...), absent before anyio 4.1;
        # crewkit[databricks] alone must still install anyio
        extras = tomllib.loads(PYPROJECT.read_text())["project"]["optional-dependencies"]
        for name in ("agent-sdk", "databricks"):
            self.assertIn("anyio>=4.5", extras[name], name)


if __name__ == "__main__":
    unittest.main()
