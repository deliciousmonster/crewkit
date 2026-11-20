"""A CrewAI LLM that answers through Claude Code's print mode, so a run bills the signed-in Claude plan.

Claude Code's own tools are off: CrewAI drives its tools through the text of the reply, as it does for any model
without native function calling.
"""
import atexit
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from typing import Any

from crewai.llms.base_llm import BaseLLM

CLAUDE = os.environ.get("CREW_CLAUDE_BIN", "claude")


INVOKE = re.compile(r'<invoke name="([^"]+)">(.*?)(?:</invoke>|$)', re.S)
PARAM = re.compile(r'<parameter name="([^"]+)">(.*?)</parameter>', re.S)


def _as_action(text: str) -> str:
    """A reply in Claude's own tool-call markup, rewritten as the Action lines CrewAI parses. Without
    this CrewAI took a Scout's <invoke name="write_file"> as its final answer on 2026-10-15, nothing was
    written, and the Lead read the markup as a write done."""
    if re.search(r"^(Action|Final Answer):", text, re.M):
        return text
    call = INVOKE.search(text)
    if not call:
        return text
    args = {}
    for key, value in PARAM.findall(call.group(2)):
        try:
            args[key] = json.loads(value)
        except ValueError:
            args[key] = value
    thought = text[:call.start()].strip()
    thought = re.sub(r"</?[\w:]*function_calls>", "", thought).strip() or "I call the tool."
    if not thought.startswith("Thought:"):
        thought = f"Thought: {thought}"
    return f"{thought}\nAction: {call.group(1)}\nAction Input: {json.dumps(args)}"


UNREAD_ASK = ("You have called no tool in this task, so nothing you would report has been read or run. "
              "Call a tool first, in the Action format.")
UNREAD = f"\n\n[user]\n{UNREAD_ASK}\n\n[assistant]\n"


def _unread(text: str, prompt: str, agent) -> bool:
    """A worker holding tools that answers before any tool result reports reads it never made: the go
    Engineer quoted a framing.md with two rounds and a commit that did not exist on 2026-10-15, and the
    Lead accepted it. A delegating agent may answer from its brief, so only workers are held to this."""
    worker = bool(getattr(agent, "tools", None)) and not getattr(agent, "allow_delegation", False)
    return worker and "Observation:" not in prompt and "Final Answer:" in text and "Action:" not in text


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(part.get("text", "") for part in content if isinstance(part, dict))
    return str(content)


PERSIST_ARGS = ["--input-format", "stream-json", "--output-format", "stream-json", "--verbose"]
POOL_MAX = 8


class Session:
    """One `claude -p` process per conversation, in stream-json mode, sent only what is new.

    CrewAI resends an agent's whole history on every call; a Session saves the process start (1.1 to 1.9 s a call,
    measured 2026-10-27) and the cache writes of a fresh one. It is reusable only while the reply it remembers is the
    one CrewAI kept: a reply that wrote past a stop word or was rewritten from tool-call markup diverges, and the next
    call starts a new process. CREW_PLAN_PERSIST=0 turns it off.
    """

    def __init__(self, argv: list, scratch, env: dict, model: str, system: str):
        self.scratch, self.model, self.system = scratch, model, system
        self.seen: list = []      # CrewAI's turns as of the last call
        self.returned = ""        # the reply that call returned; reusable only if the process holds it verbatim
        self.lock = threading.Lock()
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, cwd=scratch.name, env=env, bufsize=1)
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            self.lines.put(line)
        self.lines.put(None)

    def ask(self, text: str, timeout_s: int) -> dict:
        self.proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                line = self.lines.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                self.close()
                raise RuntimeError(f"claude -p gave no reply in {timeout_s} s")
            if line is None:
                first = (self.proc.stderr.read() or "").strip().splitlines()[:1]
                raise RuntimeError(f"claude -p exited {self.proc.wait()}: {first[0] if first else ''}")
            event = json.loads(line)
            if event.get("type") == "result":
                return event

    def delta(self, model: str, system: str, turns: list) -> str | None:
        """What the process has not yet read, or None when CrewAI's history no longer extends its own."""
        n = len(self.seen)
        if not self.alive() or (model, system) != (self.model, self.system) or not self.returned or turns[:n] != self.seen or len(turns) <= n:
            return None
        head = turns[n]
        if head.get("role") != "assistant" or not _text(head["content"]).startswith(self.returned):
            return None
        rest = [_text(head["content"])[len(self.returned):].strip()]
        for m in turns[n + 1:]:
            if m.get("role") == "assistant":
                return None
            rest.append(_text(m["content"]))
        text = "\n\n".join(r for r in rest if r)
        return text or None

    def alive(self) -> bool:
        return self.proc.poll() is None

    def close(self):
        if self.alive():
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if pipe and not pipe.closed:
                try:
                    pipe.close()
                except ValueError:  # the reader thread may hold stdout mid-read
                    pass
        self.scratch.cleanup()


_POOL: list = []
_POOL_LOCK = threading.Lock()


@atexit.register
def _close_pool():
    with _POOL_LOCK:
        while _POOL:
            _POOL.pop().close()


def _persist() -> bool:
    return os.environ.get("CREW_PLAN_PERSIST", "1") != "0"


class ClaudePlanLLM(BaseLLM):
    llm_type: str = "claude-plan"
    provider: str = "anthropic"
    timeout_s: int = 1800

    def supports_function_calling(self) -> bool:
        return False

    def supports_stop_words(self) -> bool:
        # Print mode takes no stop list, so call() cuts the reply at each one. Saying yes matters: CrewAI
        # adds "Observation:" only for a model that stops, and one that did not wrote its own tool results.
        return True

    def get_context_window_size(self) -> int:
        return 200_000

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None) -> str:
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]
        system = "\n\n".join(_text(m["content"]) for m in messages if m.get("role") == "system")
        turns = [m for m in messages if m.get("role") != "system"]
        if len(turns) == 1:
            prompt = _text(turns[0]["content"])
        else:
            prompt = "\n\n".join(f"[{m.get('role', 'user')}]\n{_text(m['content'])}" for m in turns)
            prompt += "\n\n[assistant]\n"

        system += (f"\n\nThe working directory and any scratchpad directory you are told about belong to this "
                   f"conversation and vanish after it; never name them to a tool or a coworker. Your tools run in "
                   f"the crew's process, whose working directory is {os.getcwd()}; give them absolute paths from "
                   f"the task.")
        if not _persist():
            text = self._ask(system, prompt, from_agent, from_task)
            if _unread(text, prompt, from_agent):
                text = self._ask(system, prompt + UNREAD, from_agent, from_task)
            return text
        session, sent, kind = self._session(system, turns, prompt)
        try:
            raw, text = self._ask_session(session, sent, kind, system, from_agent, from_task)
            if _unread(text, prompt, from_agent):
                raw, text = self._ask_session(session, UNREAD_ASK, "retry", system, from_agent, from_task)
        except Exception:
            session.close()
            raise
        # A reply that ends on a bare stop word ("Observation:" and nothing after) wrote nothing CrewAI dropped.
        tail = raw[len(text):].strip() if raw.startswith(text) else None
        kept = tail == "" or tail in {stop.strip() for stop in self.stop_sequences or []}
        session.seen, session.returned = [dict(m) for m in turns], (text if kept else "")
        with _POOL_LOCK:
            _POOL.append(session)
            while len(_POOL) > POOL_MAX:
                _POOL.pop(0).close()
        return text

    def _argv(self, path: str, extra: list) -> list:
        return [CLAUDE, "-p", *extra, "--model", self.model, "--tools", "", "--no-session-persistence",
                "--strict-mcp-config", "--setting-sources", "", "--system-prompt-file", path]

    @staticmethod
    def _env() -> dict:
        # The plan, not a key: an API key in the environment would take precedence over the sign-in.
        return {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}

    @staticmethod
    def _system_file(scratch: str, system: str) -> str:
        # Claude Code tells the model its working directory, which is this empty scratch one; the system
        # note call() adds says so. An empty directory also keeps any project's CLAUDE.md out.
        path = os.path.join(scratch, "system.md")
        with open(path, "w") as f:
            f.write(system.lstrip())
        return path

    def _session(self, system: str, turns: list, prompt: str) -> tuple:
        """(session, text to send, "reused" or "new"): a live process whose history this call extends, taken
        out of the pool, or a new one sent the whole prompt."""
        with _POOL_LOCK:
            for session in list(_POOL):
                sent = session.delta(self.model, system, turns)
                if sent is not None:
                    _POOL.remove(session)
                    return session, sent, "reused"
        scratch = tempfile.TemporaryDirectory(prefix="crew-plan-")
        argv = self._argv(self._system_file(scratch.name, system), PERSIST_ARGS)
        return Session(argv, scratch, self._env(), self.model, system), prompt, "new"

    def _ask_session(self, session: Session, sent: str, kind: str, system: str, from_agent, from_task) -> tuple:
        started = time.monotonic()
        reply = session.ask(sent, self.timeout_s)
        return self._finish(reply, time.monotonic() - started, system, sent, from_agent, from_task, kind)

    def _ask(self, system: str, prompt: str, from_agent=None, from_task=None) -> str:
        with tempfile.TemporaryDirectory(prefix="crew-plan-") as scratch:
            argv = self._argv(self._system_file(scratch, system), ["--output-format", "json"])
            started = time.monotonic()
            done = subprocess.run(argv, input=prompt, capture_output=True, text=True, cwd=scratch,
                                  env=self._env(), timeout=self.timeout_s)
            wall = time.monotonic() - started
        if done.returncode != 0:
            # The argv holds no secret; report the exit and the first stderr line only.
            first = (done.stderr or done.stdout).strip().splitlines()[:1]
            raise RuntimeError(f"claude -p exited {done.returncode}: {first[0] if first else ''}")
        return self._finish(json.loads(done.stdout), wall, system, prompt, from_agent, from_task, "oneshot")[1]

    def _finish(self, reply: dict, wall: float, system: str, sent: str, from_agent, from_task, kind: str) -> tuple:
        """(the reply as the model wrote it, the reply CrewAI gets)."""
        if reply.get("is_error"):
            raise RuntimeError(f"claude -p reported an error: {str(reply.get('result'))[:200]}")
        raw = text = reply.get("result", "")
        if os.environ.get("CREW_PLAN_DEBUG"):
            # Wall time against claude -p's own durations shows what process startup costs each call; `session`
            # says whether a live process was reused, and `sent` how much of the history went to it.
            with open(os.environ["CREW_PLAN_DEBUG"], "a") as log:
                log.write(json.dumps({"agent": getattr(from_agent, "role", "").strip() or None,
                                      "task": getattr(from_task, "name", None), "model": self.model,
                                      "session": kind, "sent": len(sent),
                                      "wall_s": round(wall, 2), "duration_ms": reply.get("duration_ms"),
                                      "duration_api_ms": reply.get("duration_api_ms"),
                                      "system": system, "prompt": sent, "reply": text,
                                      "stop": self.stop_sequences}) + "\n")
        # Stop sequences are applied here, since print mode takes none.
        for stop in self.stop_sequences or []:
            cut = text.find(stop)
            if cut != -1:
                text = text[:cut]
        return raw, _as_action(text)
