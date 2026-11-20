"""One gated entry to the Claude Agent SDK: `connect()` yields a client only once the CLI's account
report matches the backend crewkit resolved.

CREW_LLM_BACKEND says who pays (`api` an Anthropic API key, `plan` the Claude sign-in, named; `auto`
means `api` here and refuses without a key). CREW_LLM_ROUTE says where the request goes: `direct`, or
`databricks-gateway`, a Databricks model provider service in relayed mode (crewkit.databricks_gateway).
Call `connect` before starting threads that read or write `os.environ`: it reduces the environment
for the span of the SDK's own `connect()` and then restores it, less `ANTHROPIC_API_KEY` once the gate
has passed.
"""

from __future__ import annotations

import dataclasses
import errno
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from crewkit import llm

if TYPE_CHECKING:
    from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

Backend = Literal["plan", "api"]
Route = Literal["direct", "databricks-gateway"]
ROUTES = ("direct", "databricks-gateway")

ALLOWED_ENV = ("HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "TZ")
FORBIDDEN_PREFIXES = ("ANTHROPIC_", "CLAUDE", "AWS_", "CLOUD_ML_", "GOOGLE_", "GCLOUD_", "VERTEX_", "AZURE_")
FORBIDDEN_NAMES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NODE_OPTIONS", "NODE_EXTRA_CA_CERTS")
SKIP_HISTORY = "CLAUDE_CODE_SKIP_PROMPT_HISTORY"
# Names under a forbidden prefix that change no credential, provider or route.
ALLOWED_NAMES = ("CLAUDE_CODE_MAX_RETRIES",)

PLAN_KEYS = frozenset({"apiProvider", "email", "organization", "subscriptionType"})
API_KEYS = (frozenset({"apiKeySource", "apiProvider"}), frozenset({"apiKeySource", "apiProvider", "tokenSource"}))
API_TOKEN_SOURCES = ("claude.ai", "none")
CLOSE_BOUND_S = 5.0


@dataclasses.dataclass(frozen=True)
class Verdict:
    backend: Backend
    route: Route
    allowed: bool
    reason: str
    cli_version: str | None
    api_provider: str | None
    subscription: str | None
    account_keys: tuple[str, ...]
    databricks_service: str | None = None


class AgentSdkError(Exception):
    """A refusal or an unavailable CLI; its text is the reason code alone."""

    def __init__(self, reason: str, verdict: Verdict | None = None):
        super().__init__(reason)
        self.reason = reason
        self.verdict = verdict


class Refused(AgentSdkError):
    """Policy: the run must not reach the model."""


class Unavailable(AgentSdkError):
    """The CLI did not answer in time, or exited."""


def forbidden(name: str) -> bool:
    if name in ALLOWED_NAMES:
        return False
    return name.startswith(FORBIDDEN_PREFIXES) or name.upper() in FORBIDDEN_NAMES


def resolve(key_file: Path | None, *, backend: str | None = None) -> Backend:
    """The backend this path bills; `plan` only when named, by CREW_LLM_BACKEND or `backend=`."""
    asked = backend if backend is not None else os.environ.get("CREW_LLM_BACKEND", "auto")
    if asked not in llm.BACKENDS:
        raise Refused("backend_invalid")
    picked = llm._pick(asked, llm.key_present(key_file))
    if picked == "plan" and asked != "plan":
        raise Refused("plan_not_pinned")
    return "plan" if picked == "plan" else "api"


def resolve_route(backend: Backend, *, asked_backend: bool, route: str | None = None) -> Route:
    """Where the request goes; `databricks-gateway` only under a named `plan`."""
    taken = route if route is not None else os.environ.get("CREW_LLM_ROUTE", "direct")
    if taken == "direct":
        return "direct"
    if taken != "databricks-gateway":
        raise Refused("route_invalid")
    if not asked_backend:
        raise Refused("route_needs_backend")
    if backend == "api":
        raise Refused("route_not_built:api")
    if not (os.environ.get("CREW_DATABRICKS_PROFILE") and os.environ.get("CREW_DATABRICKS_SERVICE")):
        raise Refused("databricks_gateway_unconfigured")
    return "databricks-gateway"


def _plan_reason(account: dict) -> str:
    if "apiKeySource" in account:
        return "api_key_present"
    if account.get("tokenSource", "none") != "none":
        return "token_present"
    if set(account) != PLAN_KEYS:
        return "account_shape"
    sub = account.get("subscriptionType")
    return "ok" if isinstance(sub, str) and sub else "not_subscription"


def _api_reason(account: dict) -> str:
    if "apiKeySource" not in account:
        return "subscription_in_api_mode" if "subscriptionType" in account else "account_shape"
    if "tokenSource" in account and account["tokenSource"] not in API_TOKEN_SOURCES:
        return "token_present"
    if set(account) not in API_KEYS or account["apiKeySource"] != "ANTHROPIC_API_KEY":
        return "account_shape"
    return "ok"


def _reason(account: object, backend: Backend, cli_version: object) -> str:
    if account is None:
        return "account_missing"
    if not isinstance(account, dict) or not all(isinstance(k, str) for k in account):
        return "account_shape"
    provider = account.get("apiProvider")
    if provider != "firstParty":
        return f"provider:{provider}" if isinstance(provider, str) and provider.isalnum() else "account_shape"
    reason = _plan_reason(account) if backend == "plan" else _api_reason(account)
    if reason == "ok" and not (isinstance(cli_version, str) and cli_version):
        return "cli_version_unknown"
    return reason


def check(account: object, backend: Backend, cli_version: object) -> Verdict:
    """Whether the CLI's `account` report admits `backend`; total, matched on the exact key set."""
    reason = _reason(account, backend, cli_version)
    fields = account if isinstance(account, dict) else {}
    provider, sub = fields.get("apiProvider"), fields.get("subscriptionType")
    return Verdict(
        backend=backend,
        route="direct",
        allowed=reason == "ok",
        reason=reason,
        cli_version=cli_version if isinstance(cli_version, str) and cli_version else None,
        api_provider=provider if isinstance(provider, str) else None,
        subscription=sub if isinstance(sub, str) and sub else None,
        account_keys=tuple(sorted(str(k) for k in fields)),
    )


def check_options(options: ClaudeAgentOptions, backend: Backend) -> None:
    """Refuse an options object that would bring back built-ins, settings or a redirected route."""
    unsafe = {
        "tools": not isinstance(options.tools, list),
        "setting_sources": options.setting_sources != [],
        "strict_mcp_config": options.strict_mcp_config is not True,
        "permission_mode": options.permission_mode != "dontAsk",
        "env": any(forbidden(name) for name in options.env),
        "settings": options.settings is not None,
        "extra_args": bool(options.extra_args),
        "max_budget_usd": backend == "api" and options.max_budget_usd is None,
    }
    for name, bad in unsafe.items():
        if bad:
            raise Refused(f"options_unsafe:{name}")


def read_key(key_file: Path | None) -> str:
    """The API key, read at use and removed from this process's environment for good."""
    if "ANTHROPIC_API_KEY" in os.environ:
        return _nonempty(os.environ.pop("ANTHROPIC_API_KEY"))
    if key_file is None or not key_file.exists():
        raise Refused("no_key")
    unreadable = ""
    try:
        return _nonempty(key_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as err:
        unreadable = errno.errorcode.get(getattr(err, "errno", None) or 0, "decode")
    raise Refused(f"key_unreadable:{unreadable}")


def _nonempty(key: str) -> str:
    key = key.strip()
    if not key:
        raise Refused("empty_key")
    return key


def check_keep_env(keep_env: Sequence[str]) -> None:
    for name in keep_env:
        if forbidden(name):
            raise Refused(f"keep_env_forbidden:{name}")


class _ReducedEnv:
    """`os.environ` holds only the allowlist inside; on exit every saved name but the API key returns."""

    def __init__(self, keep_env: Sequence[str]):
        self.keep = tuple(ALLOWED_ENV) + tuple(keep_env)
        self.saved: dict[str, str] = {}

    def __enter__(self) -> None:
        self.saved = dict(os.environ)
        os.environ.clear()
        os.environ.update({k: v for k, v in self.saved.items() if k in self.keep})

    def __exit__(self, *exc: object) -> None:
        self.saved.pop("ANTHROPIC_API_KEY", None)
        os.environ.clear()
        os.environ.update(self.saved)


async def _start(client: ClaudeSDKClient, keep_env: Sequence[str], timeout_s: float) -> None:
    import anyio
    from claude_agent_sdk import CLIConnectionError, ProcessError

    failed = ""
    try:
        with _ReducedEnv(keep_env), anyio.fail_after(timeout_s):
            await client.connect()
    except TimeoutError:
        failed = "connect_timeout"
    except (CLIConnectionError, ProcessError) as err:
        failed = f"cli_failed:{type(err).__name__}"
    if failed:
        raise Unavailable(failed)


async def _close(client: ClaudeSDKClient, *, interrupt: bool) -> None:
    """Bounded; a close that fails leaves the child to the SDK's atexit reaper and stdin EOF."""
    import anyio
    from claude_agent_sdk import ClaudeSDKError

    steps = (client.interrupt, client.disconnect) if interrupt else (client.disconnect,)
    for step in steps:
        with anyio.move_on_after(CLOSE_BOUND_S, shield=True):
            try:
                await step()
            except (ClaudeSDKError, OSError):
                pass


def _child_env(options: ClaudeAgentOptions, key: str | None, gateway: dict[str, str]) -> dict[str, str]:
    env = {**options.env, SKIP_HISTORY: "1", **gateway}
    if key is not None:
        env["ANTHROPIC_API_KEY"] = key
    return env


@asynccontextmanager
async def connect(
    options: ClaudeAgentOptions,
    *,
    key_file: Path | None = None,
    keep_env: Sequence[str] = (),
    timeout_s: float = 30.0,
    backend: Backend | None = None,
    route: Route | None = None,
) -> AsyncIterator[tuple[ClaudeSDKClient, Verdict]]:
    """Yield `(client, verdict)` once the CLI's account report matches the resolved backend.

    Raises `Refused` on policy, `Unavailable` when the CLI doesn't answer, and, on the
    `databricks-gateway` route, `DatabricksGatewayUnavailable` before any child starts.
    """
    asked = backend is not None or os.environ.get("CREW_LLM_BACKEND") in ("api", "plan")
    chosen = resolve(key_file, backend=backend)
    taken = resolve_route(chosen, asked_backend=asked, route=route)
    check_options(options, chosen)
    check_keep_env(keep_env)
    exported = os.environ.get("ANTHROPIC_API_KEY") if chosen == "api" else None
    passed = False
    try:
        key = read_key(key_file) if chosen == "api" else None
        gateway, service = await _gateway(taken)

        from claude_agent_sdk import ClaudeSDKClient

        client = ClaudeSDKClient(dataclasses.replace(options, env=_child_env(options, key, gateway)))
        try:
            verdict = await _gate(client, keep_env, timeout_s, chosen)
            verdict = dataclasses.replace(verdict, route=taken, databricks_service=service)
            if not verdict.allowed:
                raise Refused(verdict.reason, verdict)
            passed = True
            yield client, verdict
        finally:
            await _close(client, interrupt=passed)
    finally:
        # an exported key leaves this process only once the gate has passed, so a refusal can be retried
        if not passed and exported is not None:
            os.environ.setdefault("ANTHROPIC_API_KEY", exported)


async def _gateway(taken: Route) -> tuple[dict[str, str], str | None]:
    if taken != "databricks-gateway":
        return {}, None
    from crewkit import databricks_gateway

    settings = databricks_gateway.from_env()
    return await databricks_gateway.preflight(settings), settings.service


async def _gate(client: ClaudeSDKClient, keep_env: Sequence[str], timeout_s: float, chosen: Backend) -> Verdict:
    await _start(client, keep_env, timeout_s)
    info = await client.get_server_info()
    info = info if isinstance(info, dict) else {}
    return check(info.get("account"), chosen, info.get("claude_code_version"))
