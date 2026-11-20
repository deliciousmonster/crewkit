"""The preflight for CREW_LLM_ROUTE=databricks-gateway: a Databricks token and a model provider service
that answers relayed and serves models, all before any Claude Code child starts.

The token is fetched through the profile's own auth, held in a local and returned only inside the
child's ANTHROPIC_CUSTOM_HEADERS; no exception, log line or argv carries it.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from crewkit.agent_sdk import Unavailable

SERVICES_PATH = "/api/2.1/unity-catalog/model-provider-services/"
MODELS_PATH = "/ai-gateway/anthropic/v1/models"
# <catalog>.<schema>.<name>; it goes into a URL path and a header line, so nothing that could change either
SERVICE_NAME = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")


@dataclass(frozen=True)
class DatabricksGateway:
    profile: str
    service: str
    min_token_ttl_s: int = 600
    timeout_s: float = 10.0


class DatabricksGatewayUnavailable(Unavailable):
    """The gateway can't be used: nothing was spawned, sent or billed."""


def from_env() -> DatabricksGateway:
    return DatabricksGateway(os.environ["CREW_DATABRICKS_PROFILE"], os.environ["CREW_DATABRICKS_SERVICE"])


def _fetch(profile: str) -> tuple[object, str]:
    from databricks.sdk.core import Config

    config = Config(profile=profile)
    return config.authenticate().get("Authorization"), str(config.host or "").rstrip("/")


def _jwt_exp(token: str) -> float | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (binascii.Error, ValueError):
        return None
    exp = claims.get("exp") if isinstance(claims, dict) else None
    return float(exp) if isinstance(exp, int | float) and not isinstance(exp, bool) else None


async def _token(gw: DatabricksGateway) -> tuple[str, str]:
    import anyio

    got: tuple[object, str] | None = None
    try:
        with anyio.fail_after(gw.timeout_s):
            got = await anyio.to_thread.run_sync(_fetch, gw.profile, abandon_on_cancel=True)
    except Exception:  # noqa: BLE001 - any auth failure or timeout is one code, raised without context
        got = None
    header, host = got if got else (None, "")
    token = header[len("Bearer ") :] if isinstance(header, str) and header.startswith("Bearer ") else ""
    exp = _jwt_exp(token) if token and host else None
    if exp is None:
        raise DatabricksGatewayUnavailable("databricks_gateway_token")
    if exp - time.time() < gw.min_token_ttl_s:
        raise DatabricksGatewayUnavailable("databricks_gateway_token_short")
    return token, host


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx comes back as its own status; following it would copy the token's headers to the new host."""

    def redirect_request(self, *_args: object, **_kwargs: object) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _get(url: str, headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
    """One GET; any transport failure is status 0. Headers are built by the caller and never echoed."""
    try:
        with _OPENER.open(urllib.request.Request(url, headers=headers), timeout=timeout_s) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as err:
        err.close()
        return err.code, b""
    except (OSError, ValueError):
        return 0, b""


async def _bounded_get(url: str, headers: dict[str, str], timeout_s: float) -> tuple[int, bytes]:
    import anyio

    with anyio.move_on_after(timeout_s):
        return await anyio.to_thread.run_sync(_get, url, headers, timeout_s, abandon_on_cancel=True)
    return 0, b""


def _status_code(status: int) -> str | None:
    if status in (401, 403):
        return "databricks_gateway_auth"
    if status == 404:
        return "databricks_gateway_service_unknown"
    if status != 200:
        return "databricks_gateway_unreachable"
    return None


def _json(body: bytes) -> dict:
    try:
        parsed = json.loads(body)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


async def _relayed(gw: DatabricksGateway, host: str, token: str) -> None:
    status, body = await _bounded_get(host + SERVICES_PATH + gw.service, {"Authorization": "Bearer " + token}, gw.timeout_s)
    code = _status_code(status)
    if code:
        raise DatabricksGatewayUnavailable(code)
    config = _json(body).get("config")
    anthropic = config.get("anthropic") if isinstance(config, dict) else None
    if not isinstance(anthropic, dict) or "relayed" not in anthropic or "direct" in anthropic:
        raise DatabricksGatewayUnavailable("databricks_gateway_not_relayed")


async def _serving(gw: DatabricksGateway, host: str, token: str) -> None:
    headers = {"X-Databricks-AI-Gateway-Token": "Bearer " + token, "Databricks-Model-Provider-Service": gw.service}
    status, body = await _bounded_get(host + MODELS_PATH, headers, gw.timeout_s)
    code = _status_code(status)
    if code:
        raise DatabricksGatewayUnavailable(code)
    data = _json(body).get("data")
    if not (isinstance(data, list) and data):
        raise DatabricksGatewayUnavailable("databricks_gateway_service_unknown")


async def preflight(gw: DatabricksGateway) -> dict[str, str]:
    """The child's two env entries, once the token, the relayed read and the models list all pass."""
    if not SERVICE_NAME.fullmatch(gw.service):
        raise DatabricksGatewayUnavailable("databricks_gateway_service_invalid")
    token, host = await _token(gw)
    await _relayed(gw, host, token)
    await _serving(gw, host, token)
    return {
        "ANTHROPIC_BASE_URL": host + "/ai-gateway/anthropic",
        "ANTHROPIC_CUSTOM_HEADERS": (
            f"X-Databricks-AI-Gateway-Token: Bearer {token}\nDatabricks-Model-Provider-Service: {gw.service}"
        ),
    }
