"""The current crew cycle's tokens and API price after every reply, as a Claude Code Stop hook.

A crew declares it under [[tool.crewkit.hooks]] with event "Stop" and the command in COMMAND. Claude Code
names the session's transcript on stdin; the hook totals the cycle that began at the last prompt the owner
typed to run the crew's skill, the Lead's calls and every agent that cycle spawned at any depth, each call
once and priced in integer nano-dollars as `crewkit.usage.report` does, and prints

    {"systemMessage": "Tokens this cycle: 54.1k · API price: $0.02"}

It prints nothing and exits 0 on every error, in a session with no typed cycle, when another crew's skill
was typed after this one's, and when it could not finish inside its budget; the next reply resumes. Its
first run in a session searches the transcript backward for the skill's marker bytes, and every later run
reads only new bytes, from offsets kept under ~/.cache/crewkit/stopline/ with each call id counted and
no prompt, reply or tool text. Standard library only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import stat
import sys
import tempfile
import time
from pathlib import Path

from crewkit import spec, usage

COMMAND = "{crew_path}/.venv/bin/python -I -m crewkit.stopline {skill} || true"
SKILL = re.compile(r"[A-Za-z0-9]+")
STATE = re.compile(r"[A-Za-z0-9-]{1,64}-[A-Za-z0-9]+\.json")
TEMP = re.compile(r"[A-Za-z0-9-]{1,64}-[A-Za-z0-9]+\.json\.\w+\.tmp")
WINDOW = 8 << 20
BUDGET, ALARM, RETRIES = 1.5, 3.0, 3
KEEP, ORPHAN, DAY = 30 * 86400, 86400, 86400


class Late(Exception):
    """The budget ran out between two windows; what was read so far is kept."""


class Lag(Exception):
    """The transcript does not yet hold the reply Claude Code just finished."""


def state_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or spec.home() / ".cache") / "crewkit" / "stopline"


def state_path(session_id, skill) -> Path | None:
    """Where one session's state for one skill lives, or None when either name is not a plain id."""
    if not isinstance(session_id, str) or not usage.SESSION_ID.fullmatch(session_id):
        return None
    if not isinstance(skill, str) or not SKILL.fullmatch(skill):
        return None
    return state_dir() / f"{session_id}-{skill}.json"


def starts(p: str, skill: str) -> bool:
    """The cycle rule `report` uses: a typed prompt that ran /<skill>."""
    return f"<command-name>/{skill}" in p or p.startswith(f"/{skill}")


def _markers(skill: str) -> tuple[bytes, bytes]:
    return f"<command-name>/{skill}".encode(), f'"/{skill}'.encode()


def _open(path: Path) -> int | None:
    """A descriptor on a regular file, opened so a FIFO cannot block, else None."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    if stat.S_ISREG(os.fstat(fd).st_mode):
        return fd
    os.close(fd)
    return None


def _mark(fd: int, off: int, rec: dict | None = None) -> dict:
    """A file's identity at `off`: device, inode, offset and the hash of the 64 bytes before it."""
    st = os.fstat(fd)
    lo = max(0, off - 64)
    out = dict(rec or {})
    out.update(dev=st.st_dev, ino=st.st_ino, off=off, hash=hashlib.sha256(os.pread(fd, off - lo, lo)).hexdigest())
    return out


def _same(fd: int, rec: dict) -> bool:
    st = os.fstat(fd)
    return (st.st_dev, st.st_ino) == (rec.get("dev"), rec.get("ino")) and st.st_size >= rec.get("off", 0) \
        and _mark(fd, rec["off"])["hash"] == rec.get("hash")


def _rows(buf: bytes, at: int):
    """(offset, row) for each row of a window that parses."""
    for line in buf.split(b"\n"):
        r = usage.parse(line) if line.strip() else None
        if r is not None:
            yield at, r
        at += len(line) + 1


def _windows(fd: int, off: int, size: int):
    """(offset, bytes) windows up to `size`, each cut after a newline; a half-written last row waits."""
    while off < size:
        n = WINDOW
        while True:
            buf = os.pread(fd, min(n, size - off), off)
            cut = buf.rfind(b"\n")
            if cut >= 0 or off + len(buf) >= size:
                break
            n *= 2
        if cut < 0:
            return
        yield off, buf[:cut + 1]
        off += cut + 1


def _end(fd: int, size: int) -> int:
    """The offset just past the last newline, so a half-written last row is left for the next run."""
    n = WINDOW
    while True:
        lo = max(0, size - n)
        cut = os.pread(fd, size - lo, lo).rfind(b"\n")
        if cut >= 0 or lo == 0:
            return lo + cut + 1
        n *= 2


def _back(fd: int, hi: int):
    """(offset, bytes) windows from `hi` backward, each starting and ending at a row boundary."""
    while hi > 0:
        lo = max(0, hi - WINDOW)
        buf = os.pread(fd, hi - lo, lo)
        while lo > 0 and buf.find(b"\n", 0, len(buf) - 1) < 0:  # the row ending at hi starts further back
            lo = max(0, lo - WINDOW)
            buf = os.pread(fd, hi - lo, lo)
        if lo > 0:
            nl = buf.find(b"\n", 0, len(buf) - 1) + 1
            buf, lo = buf[nl:], lo + nl
        yield lo, buf
        hi = lo


def _find_start(fd: int, size: int, skill: str) -> tuple[int, int | None]:
    """(end of the last whole row, offset of the last typed start of `skill` or None)."""
    end = _end(fd, size)
    for lo, buf in _back(fd, end):
        if any(m in buf for m in _markers(skill)):
            hits = [at for at, r in _rows(buf, lo) if (p := usage.prompt(r)) is not None and starts(p, skill)]
            if hits:
                return end, hits[-1]
    return end, None


def _fresh(st: dict, at: int) -> None:
    keep = {k: st[k] for k in ("v", "lead", "other_at") if k in st}
    st.clear()
    st.update(keep, start_at=at, seen=set(), sum={}, tools=set(), spawned=set(), metas={}, agents={})


def _add(sums: dict, r: dict, key, seen: set) -> bool:
    acc: dict = {}
    if not usage.tally(r, key, seen, acc):
        return False
    sums["tokens"] = sums.get("tokens", 0) + sum(acc["calls"]) + acc.get("output", 0)
    sums["nanos"] = sums.get("nanos", 0) + sum(acc.get("models", {}).values())
    sums["unpriced"] = sums.get("unpriced", 0) + acc.get("unpriced", 0)
    return True


def _text(r: dict) -> list[str]:
    return [b.get("text") or "" for b in (r.get("message") or {}).get("content") or []
            if isinstance(b, dict) and b.get("type") == "text"]


def _lead_row(st: dict, at: int, r: dict, skill: str, others: set, seen_last: dict, name: str) -> None:
    p = usage.prompt(r)
    if p is not None:
        if starts(p, skill):
            _fresh(st, at)
        if any(starts(p, o) for o in others):
            st["other_at"] = at
    if "start_at" not in st:
        return
    m = r.get("message") or {}
    if r.get("type") == "assistant" and m.get("usage"):
        key = usage.call_key(r, f"{name}:{at}")
        _add(st["sum"], r, key, st["seen"])
        if seen_last.get("key") != key:
            seen_last.update(key=key, text=[])
        seen_last["text"] += _text(r)
        st["tools"].update(b.get("id") for b in m.get("content") or []
                           if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Agent")
    res = r.get("toolUseResult")
    if isinstance(res, dict) and res.get("agentId"):
        st["spawned"].add(str(res["agentId"]))


def _feed_lead(fd: int, path: Path, st: dict, skill: str, others: set, deadline: float) -> dict:
    """Read the Lead's new rows; returns the last call's key and text blocks seen in them."""
    size, last = os.fstat(fd).st_size, {}
    if "lead" not in st:
        end, start = _find_start(fd, size, skill)
        st["lead"] = _mark(fd, end if start is None else start)
        st["new"] = True
    for at, buf in _windows(fd, st["lead"]["off"], size):
        if "start_at" in st or any(m in buf for m in _markers(skill)):
            for row_at, r in _rows(buf, at):
                _lead_row(st, row_at, r, skill, others, last, path.name)
        st["lead"], st["new"] = _mark(fd, at + len(buf)), True
        if time.monotonic() > deadline:
            raise Late
    return last


def _feed_agents(sub: Path, st: dict, deadline: float) -> None:
    try:
        names = [e.name for e in os.scandir(sub)]
    except OSError:
        return
    for n in names:
        aid = n[len("agent-"):-len(".meta.json")] if n.startswith("agent-") and n.endswith(".meta.json") else None
        if aid and aid not in st["metas"] and (m := usage.meta(sub / n)) is not None:
            st["metas"][aid] = {k: m.get(k) for k in ("toolUseId", "parentAgentId", "spawnDepth")}
    for aid in usage.selected(st["metas"], st["tools"], st["spawned"]):
        rec = st["agents"].get(aid) or {"off": 0, "tokens": 0, "nanos": 0, "unpriced": 0}
        try:
            fd = _open(sub / f"agent-{aid}.jsonl")
        except FileNotFoundError:
            continue
        if fd is None:
            continue
        try:
            for at, buf in _windows(fd, rec["off"], os.fstat(fd).st_size):
                for row_at, r in _rows(buf, at):
                    _add(rec, r, usage.call_key(r, f"agent-{aid}.jsonl:{row_at}"), st["seen"])
                st["agents"][aid] = rec = _mark(fd, at + len(buf), rec)
                st["new"] = True
                if time.monotonic() > deadline:
                    raise Late
        finally:
            os.close(fd)


def _valid(transcript: Path, st: dict) -> bool:
    """Every file the state has read is still the file it read, up to its offset."""
    pairs = [(transcript, st["lead"])] + [(transcript.with_suffix("") / "subagents" / f"agent-{a}.jsonl", rec)
                                           for a, rec in st.get("agents", {}).items()]
    for path, rec in pairs:
        try:
            fd = _open(path)
        except OSError:
            return False
        if fd is None:
            return False
        try:
            if not _same(fd, rec):
                return False
        finally:
            os.close(fd)
    return True


def _norm(texts) -> str:
    return " ".join(" ".join(texts).split())


def total(transcript: Path, skill: str, state: dict, others: set[str], deadline: float,
          last: str | None = None) -> dict | None:
    """Bring `state` up to the transcript's end and return the cycle's {tokens, usd, nanos, unpriced}, or None
    when this reply shows no line. `last` is the reply Claude Code reports; Lag when the transcript lacks it."""
    fd = _open(transcript)
    if fd is None or not transcript.name.endswith(".jsonl"):
        return None
    try:
        if "lead" in state and not _valid(transcript, state):
            state.clear()
            state["new"] = True
        before = state.get("lead", {}).get("off")
        try:
            seen_last = _feed_lead(fd, transcript, state, skill, others, deadline)
            if "start_at" in state:
                _feed_agents(transcript.with_suffix("") / "subagents", state, deadline)
        except Late:
            return None
    finally:
        os.close(fd)
    if "start_at" not in state or (state.get("other_at") is not None and state["other_at"] >= state["start_at"]):
        return None
    off = state["lead"]["off"]
    if last is not None and not (off == before and state.get("checked_at") == off):
        if not seen_last or _norm(seen_last["text"]) != _norm([last]):
            raise Lag
        state["checked_at"] = off
    parts = [state["sum"]] + list(state["agents"].values())
    n = sum(p.get("nanos", 0) for p in parts)
    return {"tokens": sum(p.get("tokens", 0) for p in parts), "nanos": n, "usd": usage.cycle_usd(n),
            "unpriced": sum(p.get("unpriced", 0) for p in parts)}


def load(path: Path) -> dict:
    """The saved state, or {} when absent or unreadable; sets come back as sets."""
    try:
        st = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return {}
    if not isinstance(st, dict) or st.get("v") != 1 or not isinstance(st.get("lead"), dict):
        return {}
    for k in ("seen", "tools", "spawned"):
        if k in st:
            st[k] = set(st[k])
    return st


def save(path: Path, st: dict) -> None:
    """Write the state through a unique temp file and a rename; a failed write leaves the old state."""
    data = {k: sorted(v) if isinstance(v, set) else v for k, v in st.items() if k != "new"}
    data["v"] = 1
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        pass


def prune(folder: Path, now: float | None = None) -> None:
    """Once a day, remove state 30 days old and temp files a day old, matched by name; nothing else."""
    now = time.time() if now is None else now
    marker = folder / ".pruned"
    try:
        if now - marker.stat().st_mtime < DAY:
            return
    except OSError:
        pass
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return
    for e in entries:
        try:
            age = now - e.stat(follow_symlinks=False).st_mtime
            if (STATE.fullmatch(e.name) and age > KEEP) or (TEMP.fullmatch(e.name) and age > ORPHAN):
                os.unlink(e.path)
        except OSError:
            continue
    try:
        marker.touch()
        os.utime(marker, (now, now))
    except OSError:
        pass


def _run(skill: str, raw: str) -> str | None:
    ev = json.loads(raw)
    if not isinstance(ev, dict) or ev.get("hook_event_name") != "Stop":
        return None
    path, t = state_path(ev.get("session_id"), skill), ev.get("transcript_path")
    if path is None or not isinstance(t, str) or not t.endswith(".jsonl"):
        return None
    deadline = time.monotonic() + BUDGET
    st = load(path)
    others = spec.installed_skills() - {skill}
    last = ev.get("last_assistant_message")
    res = None
    for attempt in range(RETRIES + 1):
        try:
            res = total(Path(t), skill, st, others, deadline, last if isinstance(last, str) else None)
            break
        except Lag:
            if attempt < RETRIES:
                time.sleep(0.1)
    if st.pop("new", False):
        save(path, st)
    prune(path.parent)
    return usage.line({"total": res}) if res else None


def check() -> int:
    """`-m crewkit.stopline --check`, run by `crewkit doctor` through a crew's interpreter."""
    print("stopline ok" if usage.line else "")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--check"]:
        return check()
    try:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
        signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
        signal.setitimer(signal.ITIMER_REAL, ALARM)
        text = _run(argv[0] if argv else "", sys.stdin.read())
        signal.setitimer(signal.ITIMER_REAL, 0)
        if text:
            sys.stdout.write(json.dumps({"systemMessage": text}) + "\n")
            sys.stdout.flush()
    except BaseException:  # noqa: BLE001, S110  # a hook that fails says nothing and lets the reply stand
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
