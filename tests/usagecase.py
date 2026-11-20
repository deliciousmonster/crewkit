"""U1's fixture: one session with two cycles, nested and forked agents and id-less rows, its totals worked by hand.

Every call is claude-opus-5-5 with input and output only: 4,000 nano-dollars an input token, 20,000 an output token.
"""
import json
from pathlib import Path

LEAD_TOKENS = 660  # mL1 110, mL2 220, mL3 330
AGENT_TOKENS = {"A": 3100, "B": 5500, "F": 771, "N1": 22, "N2": 22}  # F: the pre-cycle replay mP0 (1) and mF1 (770)
TOKENS = LEAD_TOKENS + sum(AGENT_TOKENS.values())  # 10,075
NANOS = 3_600_000 + 14_000_000 + 30_000_000 + 4_204_000 + 240_000  # 52,044,000
LINE = "Tokens this cycle: 10.1k · API price: $0.05"
SEP = " "


def call(mid, inp, out=0, *, tools=(), text="t", rows=1, model="claude-opus-5-5", uuid=True):
    """One API call as Claude Code writes it: `rows` assistant rows, each carrying the whole usage."""
    u = {"input_tokens": inp, "output_tokens": out}
    content = [{"type": "text", "text": text}] + [{"type": "tool_use", "id": t, "name": "Agent", "input": {}} for t in tools]
    content += [{"type": "text", "text": ""}] * (rows - len(content))
    parts = [[b] for b in content[:rows - 1]] + [content[rows - 1:]]
    out_rows = []
    for i in range(rows):
        m = {"model": model, "usage": u, "content": parts[i]}
        r = {"type": "assistant", "message": m}
        if mid:
            m["id"] = mid
            if uuid:
                r["uuid"] = f"{mid}-{i}"
        out_rows.append(r)
    return out_rows


def typed(text):
    return {"type": "user", "message": {"content": text}, "timestamp": "t"}


def dump(rows) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def lead_rows(skill="example"):
    return ([typed("hello")] + call("mP0", 1)
            + [typed(f"<command-name>/{skill}</command-name>")] + call("m1a", 1000, tools=["tuZ"])
            + [typed(f"/{skill} go")]
            + call("mL1", 100, 10, tools=["tuA", "tuF", "tuN1", "tuN2"], rows=3)
            + call("mL2", 200, 20, text=f"a{SEP}b")
            + call("mL3", 300, 30, text="done"))


def agents():
    """agent id -> (meta, rows)."""
    def idless(n):
        return [{"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 10, "output_tokens": 1},
                                                  "content": []}} for _ in range(n)]
    return {
        "A": ({"agentType": "engineer", "description": "Build: ABC-1", "toolUseId": "tuA", "spawnDepth": 1},
              call("mA1", 1000, 100, tools=["tuB"]) + call("mA2", 2000)),
        "B": ({"agentType": "engineer", "description": "Build: ABC-1 child", "toolUseId": "tuB", "parentAgentId": "A",
               "spawnDepth": 2}, call("mB1", 5000, 500)),
        "F": ({"agentType": "fork", "description": "fork", "toolUseId": "tuF", "spawnDepth": 1, "isFork": True},
              call("mP0", 1) + call("mL1", 100, 10, rows=2) + call("mL2", 200, 20, text=f"a{SEP}b") + call("mF1", 700, 70)),
        "N1": ({"agentType": "x", "description": "n1", "toolUseId": "tuN1", "spawnDepth": 1}, idless(2)),
        "N2": ({"agentType": "x", "description": "n2", "toolUseId": "tuN2", "spawnDepth": 1}, idless(2)),
        "Z": ({"agentType": "x", "description": "old", "toolUseId": "tuZ", "spawnDepth": 1}, call("mZ1", 9000)),
    }


def write_agents(session: Path, which=None) -> Path:
    sub = session.with_suffix("") / "subagents"
    sub.mkdir(parents=True, exist_ok=True)
    for aid, (meta, rows) in agents().items():
        if which is None or aid in which:
            (sub / f"agent-{aid}.meta.json").write_text(json.dumps(meta))
            (sub / f"agent-{aid}.jsonl").write_text(dump(rows), encoding="utf-8")
    return sub


def build(d: Path, name="sess", skill="example") -> Path:
    """Write U1's session under `d` and return the transcript's path."""
    s = Path(d) / f"{name}.jsonl"
    s.write_text(dump(lead_rows(skill)), encoding="utf-8")
    write_agents(s)
    return s


# review-design-r4.md S-A: (model, input, output, cache_read, cache_creation); 404,950,000 nano-dollars, $0.40.
PRICED = [("claude-haiku-4-5", 50, 1921, 5000, 900), ("claude-opus-5", 14, 1725, 26400, 4900),
          ("claude-opus-4-8", 650, 1251, 6600, 2800), ("claude-opus-4-8", 34, 7, 23800, 2900),
          ("claude-haiku-4-5", 300, 1486, 25900, 200), ("claude-opus-5", 36, 1857, 14700, 1600),
          ("claude-opus-4-8", 1650, 639, 21900, 2100), ("claude-haiku-4-5", 200, 1446, 3000, 900),
          ("claude-opus-4-8", 55, 1183, 1800, 2700), ("claude-opus-5", 170, 1095, 25600, 100)]
PRICED_NANOS = 404_950_000


def priced_call(i, c):
    model, inp, out, read, write = c
    u = {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read, "cache_creation_input_tokens": write}
    return {"type": "assistant", "uuid": f"u{i}", "message": {"id": f"p{i}", "model": model, "usage": u,
                                                               "content": [{"type": "text", "text": "t"}]}}


def event(transcript, session="sess-1", last=None):
    ev = {"hook_event_name": "Stop", "session_id": session, "transcript_path": str(transcript), "stop_hook_active": False}
    if last is not None:
        ev["last_assistant_message"] = last
    return json.dumps(ev)


def wrapper(crew: Path, python: str, flags: str = "") -> Path:
    """A crew's .venv/bin/python as a two-line sh script; a symlink would find no pyvenv.cfg and lose crewkit."""
    py = crew / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text(f'#!/bin/sh\nexec "{python}" {flags} "$@"\n')
    py.chmod(0o755)
    return py
