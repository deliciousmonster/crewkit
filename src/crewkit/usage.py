"""What a crew run used and what it would cost at API list prices, read from Claude Code's own transcripts.

Claude Code writes each session to ~/.claude/projects/<the folder's path, dashed>/<session>.jsonl and each
subagent to <session>/subagents/agent-<id>.jsonl, with a .meta.json naming its type and the description it
was spawned with. One API call becomes several assistant rows there, one per content block, and every one
of them carries the call's whole `usage`; a call is counted once, by its message id (2026-11-05: a session
of 18 calls held 34 usage rows, and summing rows nearly doubled the total).

For each call:

  context   input + cache writes + cache reads: what the agent held at that call
  tokens    context + output: every token the call processed
  usd       the call at its model's API list price (PRICES): input, 5-minute and 1-hour cache writes,
            cache reads and output each at their own rate, doubled for fast mode

The price is what the same calls would cost on the Claude API, not what a Claude plan bills; a model
PRICES lacks is counted in `unpriced` rather than guessed.

Agents are selected at any depth, one set of call ids spans a whole report, and prices are summed in
integer nano-dollars (`nanos`), so `report` and `by_key` agree on the cents.

A crew calls `report` for a window of the Lead's session (the last turn, every turn, or the cycle since a
command such as /other), `line` for the one line a cycle ends with, and `by_key` to total the subagents
whose label names a key, such as a Linear ticket (`Build: YTC-2976`).
"""
import json
import re
from pathlib import Path

# USD per million tokens: (input, output, cache read). Cache writes are input x 1.25 for the 5-minute TTL
# and x 2 for the 1-hour TTL. Anthropic's first-party rates, as the claude-api skill cached them on
# 2026-10-06; check https://platform.claude.com/docs/en/about-claude/pricing before relying on a figure.
PRICES = {
    "claude-fable-5-1": (10.00, 50.00, 0.25), "claude-mythos-5-1": (10.00, 50.00, 0.25),
    "claude-fable-5": (10.00, 50.00, 1.00), "claude-mythos-5": (10.00, 50.00, 1.00),
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50), "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50), "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20), "claude-sonnet-5": (2.00, 10.00, 0.20),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1.00, 5.00, 0.10),
}
WRITE_5M, WRITE_1H, FAST = 1.25, 2.0, 2.0
# Haiku 5.5 bills a prompt over 100K tokens at five times its base rates ($0.50 / $2.50).
LONG = {"claude-haiku-5-5": (100_000, 5.0)}

TICKET = r"\b[A-Z][A-Z0-9]+-\d+\b"  # a Linear (or Jira) key: YTC-2976
SESSION_ID = re.compile(r"[A-Za-z0-9-]{1,64}")


def family(model: str) -> str | None:
    """The PRICES key for a model id, matched on the longest prefix so a dated id finds its family."""
    model = (model or "").removeprefix("anthropic.")
    hits = [k for k in PRICES if model == k or model.startswith(k + "-") or model.startswith(k + "@")]
    return max(hits, key=len) if hits else None


def rates(model: str) -> tuple | None:
    """The PRICES row for a model id, or None."""
    f = family(model)
    return PRICES[f] if f else None


def price(model: str, u: dict) -> float | None:
    """One call's cost in USD at API list price, or None for a model PRICES lacks."""
    r = rates(model)
    if r is None:
        return None
    base_in, out, read = r
    cc = u.get("cache_creation") or {}
    w1h = int(cc.get("ephemeral_1h_input_tokens") or 0)
    w5m = int(cc.get("ephemeral_5m_input_tokens") or 0)
    writes = int(u.get("cache_creation_input_tokens") or 0)
    if writes > w1h + w5m:  # an older record with no split: count the rest at the 5-minute rate
        w5m += writes - w1h - w5m
    inp, cached, o = (int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "output_tokens"))
    usd = (inp * base_in + w5m * base_in * WRITE_5M + w1h * base_in * WRITE_1H + cached * read + o * out) / 1e6
    over, mult = LONG.get(family(model), (None, 1.0))
    if over is not None and inp + w5m + w1h + cached > over:
        usd *= mult
    return usd * (FAST if u.get("speed") == "fast" else 1.0)


def project_dir(folder: Path) -> Path:
    return Path.home() / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(folder).resolve()))


def sessions(folder: Path) -> list[Path]:
    """The folder's Claude Code sessions, oldest first by last write."""
    d = project_dir(folder)
    return sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if d.exists() else []


def session_path(session_id: str) -> Path | None:
    """The one transcript `~/.claude/projects/*/<session_id>.jsonl`, or None for a bad id, none or several."""
    if not SESSION_ID.fullmatch(session_id or ""):
        return None
    found = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    return found[0] if len(found) == 1 else None


def parse(line: bytes) -> dict | None:
    """One transcript row, or None. Rows split on newline only; JSON leaves U+2028 and kin unescaped."""
    try:
        r = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return r if isinstance(r, dict) else None


def rows_at(path: Path) -> list[tuple[int, dict]]:
    """Each row of a transcript with the byte offset it starts at."""
    out, at = [], 0
    for line in Path(path).read_bytes().split(b"\n"):
        r = parse(line)
        if r is not None:
            out.append((at, r))
        at += len(line) + 1
    return out


def rows(path: Path) -> list:
    return [r for _, r in rows_at(path)]


def nanos(p: float) -> int:
    """A price in integer nano-dollars, so sums do not depend on order or grouping."""
    return round(p * 1e9)


def cycle_usd(n: int) -> float:
    return round(n / 1e9, 4)


def call_key(r: dict, where: str):
    """A call's identity: message id, else requestId, else uuid, else where its row sits."""
    return (r.get("message") or {}).get("id") or r.get("requestId") or r.get("uuid") or where


def meta(path: Path) -> dict | None:
    """An agent's .meta.json, or None while it is absent, empty or not an object."""
    try:
        m = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return m if isinstance(m, dict) else None


def _context(u: dict) -> int:
    return sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))


def prompt(r: dict) -> str | None:
    """The text of a prompt the owner typed, else None (tool results, reminders, notifications)."""
    if r.get("type") != "user" or r.get("isSidechain") or r.get("isMeta"):
        return None
    c = (r.get("message") or {}).get("content")
    if isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
    c = str(c or "").strip()
    if not c or c.startswith(("<task-notification", "<system-reminder")) or "<task-notification>" in c[:400]:
        return None
    return c


def _result_text(b: dict) -> str:
    c = b.get("content")
    if isinstance(c, list):
        return " ".join(x.get("text", "") for x in c if isinstance(x, dict))
    return str(c or "")


def _hint(inp: dict) -> str:
    for k in ("file_path", "command", "query", "url", "description"):
        if inp.get(k):
            return f" {str(inp[k]).splitlines()[0][:70]}"
    return ""


def tally(r: dict, key, seen: set, acc: dict) -> bool:
    """Add one assistant row's call to `acc` unless `seen` holds its key; True when it counted."""
    m = r.get("message") or {}
    if r.get("type") != "assistant" or not m.get("usage") or key in seen:
        return False
    seen.add(key)
    u = m["usage"]
    acc.setdefault("calls", []).append(_context(u))
    acc["output"] = acc.get("output", 0) + int(u.get("output_tokens") or 0)
    acc["cached"] = acc.get("cached", 0) + int(u.get("cache_read_input_tokens") or 0)
    p = price(m.get("model", ""), u)
    if p is None:
        acc["unpriced"] = acc.get("unpriced", 0) + (m.get("model") != "<synthetic>")
    else:
        models = acc.setdefault("models", {})
        models[m.get("model")] = models.get(m.get("model"), 0) + nanos(p)
    return True


def walk(rs: list, top: int = 5, *, seen: set | None = None, where: str = "", at: list | None = None) -> dict:
    """Context, tokens and price by API call over a list of transcript rows, each call once, and the
    largest tool results. `seen` spans several walks; an id-less row is keyed by `where` and its offset in `at`."""
    seen = set() if seen is None else seen
    acc: dict = {}
    names, results = {}, []
    for i, r in enumerate(rs):
        m = r.get("message") or {}
        if r.get("type") == "assistant" and m.get("usage"):
            for b in m.get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    names[b.get("id")] = (b.get("name") or "") + _hint(b.get("input") or {})
            tally(r, call_key(r, f"{where}:{at[i] if at else i}"), seen, acc)
        elif r.get("type") == "user" and isinstance(m.get("content"), list):
            for b in m["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    results.append((len(_result_text(b)), names.get(b.get("tool_use_id"), "?")))
    results.sort(reverse=True)
    calls, models = acc.get("calls", []), acc.get("models", {})
    n = sum(models.values())
    return {"calls": len(calls), "start": calls[0] if calls else 0, "end": calls[-1] if calls else 0,
            "peak": max(calls, default=0), "output": acc.get("output", 0), "cached": acc.get("cached", 0),
            "tokens": sum(calls) + acc.get("output", 0), "usd": cycle_usd(n), "nanos": n,
            "unpriced": acc.get("unpriced", 0), "models": {k: cycle_usd(v) for k, v in models.items()},
            "top": [{"chars": c, "tool": t} for c, t in results[:top]]}


def selected(metas: dict, tool_ids: set, spawned: set) -> list[str]:
    """Agent ids spawned in a window: by an Agent call or result in it, or by a selected parent at any depth."""
    out: list[str] = []
    for aid, info in sorted(metas.items(), key=lambda kv: (kv[1].get("spawnDepth") or 1, kv[0])):
        if info.get("toolUseId") in tool_ids or aid in spawned or info.get("parentAgentId") in out:
            out.append(aid)
    return out


def report(session: Path, turn: str = "last", top: int = 5, command: str | None = None,
           budgets: dict | None = None) -> dict:
    """The Lead's turns in a window of one session and every subagent spawned in them.

    turn: "last" (from the last prompt typed), "all", a 1-based turn number, or "cycle": from the last
    prompt that ran `command` (such as "/other") to the end, across the owner's replies in between.
    budgets: the peak context each agent type should stay under, and under "lead" what one turn may add."""
    budgets = budgets or {}
    pairs = rows_at(session)
    rs, offsets = [r for _, r in pairs], [o for o, _ in pairs]
    starts = [i for i, r in enumerate(rs) if prompt(r) is not None] or [0]
    bounds = list(zip(starts, starts[1:] + [len(rs)]))
    if turn == "cycle":
        cmd = (command or "").lstrip("/")
        runs = [i for i in starts if cmd and (f"<command-name>/{cmd}" in (prompt(rs[i]) or "")
                                              or (prompt(rs[i]) or "").startswith(f"/{cmd}"))]
        pick = [(runs[-1] if runs else starts[-1], len(rs))]
    else:
        pick = bounds if turn == "all" else [bounds[-1]] if turn == "last" else [bounds[int(turn) - 1]]
    subdir = Path(session).with_suffix("") / "subagents"
    found = sorted(subdir.glob("agent-*.meta.json")) if subdir.exists() else []
    metas = {p.name[len("agent-"):-len(".meta.json")]: m for p in found if (m := meta(p)) is not None}
    turns, seen = [], set()
    for a, b in pick:
        chunk = rs[a:b]
        lead = walk(chunk, top, seen=seen, where=Path(session).name, at=offsets[a:b])
        spawned = {str(r.get("toolUseResult", {}).get("agentId") or "") for r in chunk if isinstance(r.get("toolUseResult"), dict)}
        tool_ids = {blk.get("id") for r in chunk if r.get("type") == "assistant"
                    for blk in (r.get("message") or {}).get("content") or [] if isinstance(blk, dict) and blk.get("name") == "Agent"}
        agents = []
        for aid in selected(metas, tool_ids, spawned):
            info, f = metas[aid], subdir / f"agent-{aid}.jsonl"
            pairs = rows_at(f) if f.is_file() else []
            w = walk([r for _, r in pairs], top, seen=seen, where=f.name, at=[o for o, _ in pairs])
            budget = budgets.get(info.get("agentType", ""))
            agents.append({"agent": info.get("description", aid), "type": info.get("agentType"), **w,
                           "budget": budget, "over": bool(budget and w["peak"] > budget)})
        added = lead["end"] - lead["start"]
        lb = budgets.get("lead")
        turns.append({"prompt": (prompt(rs[a]) or "")[:80], "at": rs[a].get("timestamp"),
                      "lead": {**lead, "added": added, "budget": lb, "over": bool(lb and added > lb)},
                      "agents": agents})
    every = [t["lead"] for t in turns] + [a for t in turns for a in t["agents"]]
    total = {k: sum(x[k] for x in every) for k in ("tokens", "output", "cached", "unpriced")}
    n = sum(x["nanos"] for x in every)
    total.update({"usd": cycle_usd(n), "nanos": n,
                  "lead": sum(t["lead"]["tokens"] for t in turns),
                  "lead_usd": cycle_usd(sum(t["lead"]["nanos"] for t in turns)),
                  "agents": sum(a["tokens"] for t in turns for a in t["agents"]),
                  "agents_usd": cycle_usd(sum(a["nanos"] for t in turns for a in t["agents"])),
                  "n_agents": sum(len(t["agents"]) for t in turns),
                  "lead_start": turns[0]["lead"]["start"] if turns else 0,
                  "lead_end": turns[-1]["lead"]["end"] if turns else 0,
                  "peak_agent": max((a["peak"] for t in turns for a in t["agents"]), default=0)})
    return {"session": Path(session).stem, "turns": turns, "total": total}


def by_key(reps: list[dict], pattern: str = TICKET) -> dict:
    """Total the subagents of one or more reports by the first key their label names, such as the Linear
    ticket in `Build: YTC-2976`. Agents whose label names no key are left out; the Lead's own calls are not
    split, since one turn of the Lead serves several tickets."""
    out: dict = {}
    for rep in reps:
        for t in rep["turns"]:
            for a in t["agents"]:
                m = re.search(pattern, a.get("agent") or "")
                if not m:
                    continue
                k = out.setdefault(m.group(0), {"tokens": 0, "usd": 0.0, "nanos": 0, "agents": 0, "unpriced": 0})
                k["tokens"] += a["tokens"]
                k["nanos"] += a["nanos"]
                k["usd"] = cycle_usd(k["nanos"])
                k["agents"] += 1
                k["unpriced"] += a["unpriced"]
    return dict(sorted(out.items()))


def tokens_text(n: int) -> str:
    return f"{n / 1000:,.1f}k"


def usd_text(x: float) -> str:
    return f"${x:,.2f}"


def line(rep: dict, label: str = "this cycle") -> str:
    """The line a cycle ends with: total tokens and their API list price."""
    t = rep["total"]
    note = f" ({t['unpriced']} calls unpriced)" if t.get("unpriced") else ""
    return f"Tokens {label}: {tokens_text(t['tokens'])} · API price: {usd_text(t['usd'])}{note}"


def text(rep: dict, label: str = "this cycle") -> str:
    k = tokens_text
    out = [f"session {rep['session']}"]
    for t in rep["turns"]:
        L = t["lead"]
        out.append(f"\nturn {t['at']}: {t['prompt']!r}")
        out.append(f"  lead: {k(L['start'])} -> {k(L['end'])} (+{k(L['added'])}, peak {k(L['peak'])}, {L['calls']} calls, "
                   f"{k(L['tokens'])} tokens, {usd_text(L['usd'])})" + (f"  OVER budget {k(L['budget'])}" if L["over"] else ""))
        for x in L["top"]:
            out.append(f"      {x['chars']:>8,} chars  {x['tool']}")
        for a in t["agents"]:
            out.append(f"  {a['agent']} [{a['type']}]: peak {k(a['peak'])}, {a['calls']} calls, {k(a['tokens'])} tokens, "
                       f"{usd_text(a['usd'])}" + (f"  OVER budget {k(a['budget'])}" if a["over"] else ""))
            for x in a["top"]:
                out.append(f"      {x['chars']:>8,} chars  {x['tool']}")
    return "\n".join(out + ["", line(rep, label)])
