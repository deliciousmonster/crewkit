"""Shared fixtures for the agent-sdk and databricks-gateway tests."""

import base64
import json
import time
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"
CLI_VERSION = "2.1.326"
ACCOUNTS = FIXTURES / "accounts" / CLI_VERSION
DATABRICKS = FIXTURES / "databricks" / "2026-11-17"

# A written list, not names recorded from a machine: shell, toolchain, cloud, proxy and every provider switch.
PARENT_ENV = {
    "HOME": "/home/example", "USER": "example", "LOGNAME": "example", "PATH": "/usr/bin:/bin",
    "TMPDIR": "/tmp/example", "LANG": "en_US.UTF-8", "TZ": "UTC", "SHELL": "/bin/zsh", "TERM": "xterm",
    "PWD": "/work", "EDITOR": "vi", "PYTHONPATH": "/work/src", "VIRTUAL_ENV": "/work/.venv",
    "UV_CACHE_DIR": "/cache/uv", "GOPATH": "/go", "JAVA_HOME": "/jdk", "SSH_AUTH_SOCK": "/tmp/ssh",
    "DATABRICKS_HOST": "https://example.cloud.databricks.com", "MLFLOW_TRACKING_URI": "databricks",
    "ANTHROPIC_API_KEY": "sk-ant-parent-canary", "ANTHROPIC_AUTH_TOKEN": "parent-token-canary",
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9", "ANTHROPIC_MODEL": "claude-example",
    "ANTHROPIC_CUSTOM_HEADERS": "X-Example: 1", "CLAUDE_CONFIG_DIR": "/tmp/empty",
    "CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_VERTEX": "1", "CLAUDE_CODE_USE_FOUNDRY": "1",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS": "1", "CLAUDECODE": "1", "AWS_PROFILE": "example",
    "AWS_REGION": "us-east-1", "CLOUD_ML_REGION": "us-east5", "GOOGLE_APPLICATION_CREDENTIALS": "/k.json",
    "GCLOUD_PROJECT": "example", "VERTEX_REGION_CLAUDE_4_5_HAIKU": "us-east5", "AZURE_API_KEY": "az",
    "HTTP_PROXY": "http://proxy:1", "https_proxy": "http://proxy:2", "ALL_PROXY": "socks5://proxy:3",
    "NODE_OPTIONS": "--require /tmp/x.js", "NODE_EXTRA_CA_CERTS": "/tmp/ca.pem",
}
SERVICES = "/api/2.1/unity-catalog/model-provider-services/"
MODELS = "/ai-gateway/anthropic/v1/models"
ALLOWLIST = {"HOME", "USER", "LOGNAME", "PATH", "TMPDIR", "LANG", "TZ"}


def account(name: str) -> object:
    return json.loads((ACCOUNTS / f"{name}.json").read_text(encoding="utf-8"))["account"]


def canary_jwt(ttl_s: float) -> str:
    """A JWT-shaped canary; its signature is junk, as only `exp` is read."""
    enc = lambda o: base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
    return f"{enc({'alg': 'none'})}.{enc({'exp': int(time.time() + ttl_s), 'sub': 'canary'})}.c2lnLWNhbmFyeQ"


class Gateway:
    """A local stand-in for a workspace: `routes` maps a path prefix to (status, body, delay_s), and
    `locations` a path prefix to the Location header its answer carries."""

    def __init__(self, test) -> None:
        import http.server
        import threading

        routes: dict[str, tuple[int, bytes, float]] = {}
        locations: dict[str, str] = {}
        seen: list[dict] = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append({"path": self.path, "headers": dict(self.headers)})
                prefix = next((p for p in routes if self.path.startswith(p)), None)
                status, body, delay = routes.get(prefix, (404, b"{}", 0.0))
                time.sleep(delay)
                self.send_response(status)
                if prefix in locations:
                    self.send_header("Location", locations[prefix])
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        self.routes, self.locations, self.seen = routes, locations, seen
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.host = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)
        self.serve_ok()

    def serve_ok(self, service: object = None, models: object = None) -> None:
        self.routes.clear()
        self.locations.clear()
        for path, body, name in ((SERVICES, service, "provider-service-relayed"), (MODELS, models, "models")):
            self.routes[path] = (200, json.dumps(fixture(name) if body is None else body).encode(), 0.0)

def fixture(name: str) -> dict:
    return json.loads((DATABRICKS / f"{name}.json").read_text(encoding="utf-8"))


def fake_config(host: str, authorization: object = None, *, raises: BaseException | None = None, delay_s: float = 0.0):
    """A stand-in for databricks.sdk.core.Config bound to `host`."""

    class Config:
        def __init__(self, profile: str):
            self.profile, self.host = profile, host

        def authenticate(self) -> dict:
            time.sleep(delay_s)
            if raises is not None:
                raise raises
            return {"Authorization": authorization}

    return Config
