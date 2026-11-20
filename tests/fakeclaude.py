"""A stand-in for the Claude Code CLI on the SDK's stream-json control protocol.

FAKE_ACCOUNT names a fixture under fixtures/accounts/<version>/; FAKE_LOG receives this process's
environment, its initialize answer and every frame type it reads. FAKE_MODE: hang, exit1, kill, noaccount.
A user message is answered with one result message that echoes it; no model is reached.
"""

import json
import os
import signal
import sys
from pathlib import Path

VERSION = "2.1.326"
ACCOUNTS = Path(__file__).parent / "fixtures" / "accounts" / VERSION
MODE = os.environ.get("FAKE_MODE", "")


def log(entry: dict) -> None:
    with open(os.environ["FAKE_LOG"], "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def initialize_answer() -> dict:
    answer = {"claude_code_version": VERSION, "hook_on_failure_supported": True, "commands": [],
              "output_style": "default", "available_output_styles": [], "models": [], "pid": os.getpid()}
    if MODE != "noaccount":
        name = os.environ.get("FAKE_ACCOUNT", "subscription")
        answer["account"] = json.loads((ACCOUNTS / f"{name}.json").read_text(encoding="utf-8"))["account"]
    return answer


def send(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def reply(request_id: str, response: dict) -> None:
    send({"type": "control_response", "response": {"subtype": "success", "request_id": request_id, "response": response}})


def answer_user(frame: dict) -> None:
    content = (frame.get("message") or {}).get("content")
    send({"type": "result", "subtype": "success", "duration_ms": 1, "duration_api_ms": 0, "is_error": False,
          "num_turns": 1, "session_id": frame.get("session_id", "default"), "result": f"fake: {content}"})


def main() -> None:
    log({"env": dict(os.environ)})
    for line in sys.stdin:
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        subtype = (frame.get("request") or {}).get("subtype")
        log({"frame": frame.get("type"), "subtype": subtype})
        if frame.get("type") == "user":
            answer_user(frame)
        if frame.get("type") != "control_request":
            continue
        if subtype == "initialize":
            if MODE == "hang":
                continue
            if MODE == "exit1":
                sys.exit(1)
            if MODE == "kill":
                os.kill(os.getpid(), signal.SIGKILL)
            answer = initialize_answer()
            log({"initialize": sorted(answer)})
            reply(frame["request_id"], answer)
        else:
            reply(frame["request_id"], {})


if __name__ == "__main__":
    main()
