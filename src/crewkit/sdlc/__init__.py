"""The agent SDLC every crew follows: five deliverables per project, three owner gates, and a lint over them.

A project is a folder holding `brief.md`: the workspace root, or `projects/<slug>/` under it. Everything here reads
only the deliverables, never `journal/`, `evidence/` or `archive/`, so it costs the same in any size of workspace.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from importlib import resources
from pathlib import Path

from crewkit.workspace import SKIP
from crewkit.sdlc.doc import REQ_ID, Doc, add_signoff, as_int, digest, set_meta

STAGES = ("brief", "interview", "requirements", "design", "development", "acceptance", "done")
GATES = {"requirements": "G1", "design": "G2", "acceptance": "G3"}
DELIVERABLES = ("brief.md", "interview.md", "requirements.md", "design/index.md", "design/feedback.md", "development.md")
HISTORY = ("journal", "evidence", "archive")
SDLC_MAP = {
    "brief.md": "The owner's narrative of what to deliver: informal requirements, sources, outputs, constraints.",
    "interview.md": "The agent's questions and the owner's quoted answers, one Q-n entry each.",
    "requirements.md": "REQ-n entries with success and acceptance, the owner's sign-off, and change requests.",
    "design/": "The design the owner reviews as a set: index.md, one doc per component, feedback.md by round.",
    "development.md": "TASK-n rows tracked to done, acceptance per requirement, and the delivery sign-off.",
    "projects/<slug>/": "The same five deliverables for each project when a workspace holds several.",
}


@dataclass
class Finding:
    level: str  # error | warning
    code: str
    path: str
    message: str


@dataclass
class Status:
    project: Path
    stage: str
    gates: dict[str, int | None]  # gate -> the rev last signed, or None
    next: str
    findings: list[Finding] = field(default_factory=list)


class Refused(Exception):
    """An approval or change the files do not allow; nothing was written."""


def template_text(rel: str) -> str:
    return resources.files("crewkit").joinpath("templates/sdlc", rel).read_text(encoding="utf-8")


def guide() -> str:
    return template_text("GUIDE.md")


def projects(ws: Path) -> list[Path]:
    """Each folder holding a brief.md: the workspace itself, then projects/* in name order."""
    ws = Path(ws)
    found = [ws] if (ws / "brief.md").is_file() else []
    sub = ws / "projects"
    if sub.is_dir():
        found += sorted(p for p in sub.iterdir() if p.is_dir() and (p / "brief.md").is_file())
    return found


class Set:
    """One project's deliverables, each read once."""

    def __init__(self, project: Path):
        self.root = Path(project)
        read = lambda rel: Doc.read(self.root / rel)  # noqa: E731
        self.brief, self.interview, self.requirements = read("brief.md"), read("interview.md"), read("requirements.md")
        self.index, self.feedback, self.development = read("design/index.md"), read("design/feedback.md"), read("development.md")
        design = self.root / "design"
        names = sorted(p.name for p in design.iterdir() if p.suffix == ".md") if design.is_dir() else []
        self.components = [d for n in names if n not in ("index.md", "feedback.md") and (d := Doc.read(design / n))]

    def profile(self) -> str:
        return str(self.brief.meta.get("profile") or "full") if self.brief else "full"

    def reqs(self) -> dict[str, dict[str, str]]:
        return {e.id: e.fields for e in self.requirements.entries("Requirements")} if self.requirements else {}

    def req_digests(self) -> dict[str, str]:
        """A digest per REQ entry, and `*` for the rest of the Requirements section."""
        if not self.requirements:
            return {}
        rest, out = self.requirements.section("Requirements") or "", {}
        for e in self.requirements.entries("Requirements"):
            out[e.id], rest = digest(e.text), rest.replace(e.text, "")
        return {**out, "*": digest(rest)}

    def changed_reqs(self) -> set[str] | None:
        """The REQs (and `*`) whose text differs from the last approval, or None when it recorded no digests."""
        raw = str(self.requirements.meta.get("signed_reqs") or "") if self.requirements else ""
        signed = dict(p.split("=", 1) for p in raw.split() if "=" in p)
        now = self.req_digests()
        return {k for k in signed.keys() | now.keys() if signed.get(k) != now.get(k)} if signed else None

    def proposed(self) -> dict[str, set[str]]:
        doc = self.requirements
        return {e.id: set(REQ_ID.findall(e.fields.get("affects", ""))) for e in doc.entries("Changes")
                if e.fields.get("status") == "proposed"} if doc else {}

    def tasks(self) -> list[dict[str, str]]:
        return self.development.table("Tasks") if self.development else []

    def hash(self, gate: str) -> str:
        if gate == "requirements":
            return digest(self.requirements.section("Requirements") or "") if self.requirements else ""
        if gate == "design":
            return digest("\n".join(d.without("Sign-off") for d in [self.index, *self.components] if d))
        dev = self.development
        return digest((dev.section("Tasks") or "") + (dev.section("Acceptance") or "")) if dev else ""

    def doc(self, gate: str) -> Doc | None:
        return {"requirements": self.requirements, "design": self.index, "acceptance": self.development}[gate]

    def signoffs(self, gate: str) -> list[dict[str, str]]:
        d = self.doc(gate)
        return d.table("Sign-off") if d else []

    def signed(self, gate: str) -> int | None:
        rows = [r for r in self.signoffs(gate) if r.get("words", "").strip('" ')]
        return (as_int(rows[-1].get("rev")) or len(rows)) if rows else None

    def open_questions(self) -> list[str]:
        return [e.id for e in self.interview.entries() if e.fields.get("state", "open").split()[0:1] == ["open"]] if self.interview else []

    def unaccepted(self) -> list[str]:
        """The must requirements with no Acceptance row whose Result starts with pass."""
        rows = self.development.table("Acceptance") if self.development else []
        passed = {r.get("req", "") for r in rows if r.get("result", "").lower().startswith("pass")}
        return [i for i, f in self.reqs().items() if f.get("priority", "").startswith("must") and i not in passed]

    def delivered(self) -> bool:
        tasks = self.tasks()
        return bool(tasks) and all(t.get("status") in ("done", "dropped") for t in tasks) and not self.unaccepted()

    def stage(self) -> str:
        if not self.brief or self.brief.status() != "ready":
            return "brief"
        if not self.interview or self.interview.status() != "complete" or self.open_questions():
            return "interview"
        if not self.signed("requirements"):
            return "requirements"
        if not self.signed("design"):
            return "design"
        if not self.delivered():
            return "development"
        return "acceptance" if not self.signed("acceptance") else "done"


def next_gate(project: Path) -> str:
    """One sentence: what is missing before the next gate, and who acts."""
    s = Set(project)
    stage = s.stage()
    if stage == "brief":
        return "Brief: the owner and the agent write brief.md and set status: ready."
    if stage == "interview" and not s.interview:
        return "Interview: the agent writes interview.md from the brief and asks the owner its questions."
    if stage == "interview":
        n = len(s.open_questions())
        return f"Interview: the agent asks the owner the {n} open question(s), then sets interview.md status: complete."
    if stage in GATES:
        what = {"requirements": "requirements.md", "design": "design/", "acceptance": "the delivery"}[stage]
        return f"{GATES[stage]}: the owner approves {what}; the Lead records it with `sdlc approve {stage}`."
    if stage == "development":
        tasks = s.tasks()
        left = [t.get("id", "?") for t in tasks if t.get("status") not in ("done", "dropped")]
        return (f"Development: the crew finishes {len(left)} of {len(tasks)} task(s); "
                f"{len(s.unaccepted())} must requirement(s) lack a passing Acceptance row.")
    return "Done: the owner accepted the delivery."


def status(project: Path) -> Status:
    from crewkit.sdlc.lint import lint

    s = Set(project)
    return Status(Path(project), s.stage(), {g: s.signed(g) for g in GATES}, next_gate(project), lint(project))


def scaffold(project: Path, profile: str = "full") -> list[str]:
    """Write each missing deliverable from the templates; never overwrites. Returns the paths written."""
    written = []
    for rel in DELIVERABLES:
        dst = Path(project) / rel
        if dst.exists():
            continue
        text = template_text(rel)
        if rel == "brief.md" and profile != "full":
            text = re.sub(r"(?m)^profile: full", f"profile: {profile}", text)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(text, encoding="utf-8")
        written.append(rel)
    return written


def _row(rev: int, by: str, kind: str, words: str, digest_: str) -> str:
    quoted = '"' + words.strip().strip('"').replace("|", "\\|") + '"'
    return f"| {rev} | {date.today().isoformat()} | {by} | {kind} | {quoted} | {digest_} |"


def _ready(s: Set, gate: str) -> None:
    """Refuse unless the project has reached the gate's stage and lint has no error but the drift approval clears."""
    from crewkit.sdlc.lint import lint

    stage = s.stage()
    if STAGES.index(stage) < STAGES.index(gate):
        raise Refused(f"{gate}: the project is at stage {stage}; {next_gate(s.root)}")
    doc = s.doc(gate)
    own = str(doc.path.relative_to(s.root)) if doc else ""
    errors = [f for f in lint(s.root) if f.level == "error" and not (f.code == "edited-after-signoff" and f.path == own)]
    if errors:
        raise Refused(f"{gate}: lint has {len(errors)} error(s): " + "; ".join(f"{f.path} {f.code}" for f in errors))


def approve(project: Path, gate: str, by: str, words: str, kind: str = "approved") -> Path:
    """Append a Sign-off row for a gate (requirements, design, acceptance) or an approved CR-n; returns the file."""
    if not words.strip():
        raise Refused("the approver's words are required")
    if kind not in ("approved", "adopted"):
        raise Refused(f"kind is approved or adopted, not {kind}")
    s = Set(project)
    if re.fullmatch(r"CR-\d+", gate):
        return _approve_change(s, gate, by, words, kind)
    if gate not in GATES:
        raise Refused(f"gate is requirements, design, acceptance or CR-<n>, not {gate}")
    if gate == "requirements" and (rev := s.signed(gate)):
        raise Refused(f"requirements: approved at rev {rev}; a change goes through `sdlc change` and `sdlc approve CR-<n>`")
    _ready(s, gate)
    if gate == "requirements":
        _check_requirements(s)
    doc = s.doc(gate)
    assert doc is not None  # _ready's lint requires it
    text = set_meta(doc.text, "status", "accepted" if gate == "acceptance" else "approved")
    if gate == "requirements":
        text = set_meta(text, "signed_reqs", " ".join(f"{k}={v}" for k, v in s.req_digests().items()))
    rev = as_int(doc.meta.get("rev")) or 1 if gate == "requirements" else len(s.signoffs(gate)) + 1
    doc.path.write_text(add_signoff(text, _row(rev, by, kind, words, s.hash(gate))), encoding="utf-8")
    return doc.path


def _check_requirements(s: Set) -> None:
    reqs = s.reqs()
    if not reqs:
        raise Refused("requirements: no REQ entries to approve")
    gaps = [i for i, f in reqs.items() if f.get("priority", "").startswith("must") and not (f.get("success") and f.get("acceptance"))]
    if gaps:
        raise Refused(f"requirements: must entries without Success or Acceptance: {', '.join(gaps)}")
    listed = (s.requirements.section("Assumptions") or "") if s.requirements else ""
    hidden = [i for i, f in reqs.items() if f.get("source", "").strip("` ").startswith("assumed") and i not in listed]
    if hidden:
        raise Refused(f"requirements: assumed entries not listed under Assumptions and open items: {', '.join(hidden)}")


def _approve_change(s: Set, cr: str, by: str, words: str, kind: str) -> Path:
    doc = s.requirements
    if doc is None or not s.signed("requirements"):
        raise Refused(f"{cr}: requirements are not approved yet, so there is nothing to change")
    if cr not in (proposed := s.proposed()):
        raise Refused(f"{cr}: no proposed change request by that id")
    if (rev := as_int(doc.meta.get("rev") or 1)) is None:
        raise Refused(f"{cr}: requirements.md rev {doc.meta.get('rev')!r} is not a number")
    changed = s.changed_reqs()
    if outside := sorted((changed if changed is not None else {"*"}) - proposed[cr]):
        raise Refused(f"{cr}: changed text outside what it affects: {', '.join(outside)}")
    _ready(s, "requirements")
    text = set_meta(set_meta(doc.text, "rev", rev + 1), "status", "amended")
    text = set_meta(text, "signed_reqs", " ".join(f"{k}={v}" for k, v in s.req_digests().items()))
    head = re.search(rf"(?m)^### +{cr}\b.*$", text)
    assert head is not None  # the entry was parsed from this text
    end = m.start() if (m := re.compile(r"(?m)^#{2,3} ").search(text, head.end())) else len(text)
    block = re.sub(r"(?m)^- +Status:.*$", f"- Status: approved\n- Rev: {rev + 1}", text[head.end(): end], count=1)
    text = text[: head.end()] + block + text[end:]
    doc.path.write_text(add_signoff(text, _row(rev + 1, by, kind, words, s.hash("requirements"))), encoding="utf-8")
    return doc.path


def change(project: Path, title: str, affects: list[str], why: str = "", by: str = "owner") -> str:
    """Append a proposed CR under requirements.md's Changes; returns its id."""
    s = Set(project)
    doc = s.requirements
    if doc is None:
        raise Refused("change: requirements.md is missing")
    if bad := [a for a in affects if not REQ_ID.fullmatch(a)]:
        raise Refused(f"change: not REQ ids: {', '.join(bad)}")
    if unknown := [a for a in affects if a not in s.reqs()]:
        raise Refused(f"change: no such requirement: {', '.join(unknown)}; write the entry first, then propose the change")
    ids = [int(m) for m in re.findall(r"(?m)^### +CR-(\d+)", doc.body)]
    cr = f"CR-{max(ids, default=0) + 1:03d}"
    entry = (f"\n### {cr} {title}\n- What: {title}\n- Why: {why}\n- Requested by: {by}\n"
             f"- Affects: {', '.join(affects)}\n- Status: proposed\n")
    m = re.search(r"(?m)^## +Sign-off", doc.text)
    pos = m.start() if m else len(doc.text)
    doc.path.write_text(doc.text[:pos].rstrip("\n") + "\n" + entry + "\n" + doc.text[pos:], encoding="utf-8")
    return cr


def md_files(ws: Path):
    """Markdown files under ws, never descending into a journal/, evidence/, archive/ or hidden folder."""
    for d, dirs, names in os.walk(ws):
        dirs[:] = [n for n in dirs if not n.startswith(".") and n not in HISTORY and n not in SKIP]
        yield from (Path(d) / n for n in names if n.endswith(".md"))


def retire(ws: Path, paths: list[str]) -> list[str]:
    """Move legacy files to archive/pre-sdlc/, rewrite references outside journal/, evidence/ and archive/, and log
    each move in archive/path-moves.md. Refuses, moving nothing, when a target exists or a path is outside ws."""
    from crewkit.migrate import EDGE, _move
    from crewkit.workspace import git

    ws = Path(ws).resolve()
    moves = []
    for p in paths:
        src = (ws / p).resolve()
        if not src.is_relative_to(ws) or not src.is_file():
            raise Refused(f"retire: {p} is not a file in {ws}")
        rel = str(src.relative_to(ws))
        if (ws / "archive/pre-sdlc" / rel).exists():
            raise Refused(f"retire: archive/pre-sdlc/{rel} exists")
        moves.append((rel, f"archive/pre-sdlc/{rel}"))
    folder = Path(git(ws, "rev-parse", "--show-toplevel") or ws)
    for old, new in moves:
        tracked = bool(git(ws, "ls-files", "--", old))
        _move(folder, {"kind": "lift", "from": ws / old, "to": ws / new, "how": "git" if tracked else "rename"})
    # A reference is the retired file's path from the workspace, from the folder that holds it, or absolute.
    spellings = {f"{base}{old}": f"{base}{new}" for old, new in moves for base in ("", f"{ws.name}/", f"{ws}/")}
    rx = re.compile(EDGE.format("(?:" + "|".join(map(re.escape, sorted(spellings, key=len, reverse=True))) + ")"))
    rewritten = []
    for f in md_files(ws):
        text = f.read_text(encoding="utf-8", errors="surrogateescape")
        if (new_text := rx.sub(lambda m: spellings[m.group(0)], text)) != text:
            f.write_text(new_text, encoding="utf-8", errors="surrogateescape")
            rewritten.append(str(f.relative_to(ws)))
    log = ws / "archive/path-moves.md"
    head = "" if log.exists() else "# Path moves\n\nEach old path and where it is now.\n"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(head + f"\n## Retired to archive/pre-sdlc/, {date.today().isoformat()}\n\n| Old path | New path |\n|---|---|\n")
        fh.writelines(f"| `{old}` | `{new}` |\n" for old, new in moves)
    return [f"retire: {old} -> {new}" for old, new in moves] + [f"reference: {r}" for r in rewritten]


def line(ws: Path) -> str:
    """One line for a SessionStart hook: each project's stage and next gate."""
    found = projects(ws)
    if not found:
        return "sdlc: no brief.md in this workspace; `sdlc init` starts one."
    return " | ".join(f"sdlc {p.name if p != Path(ws) else 'project'}: {Set(p).stage()}; next: {next_gate(p)}" for p in found)


__all__ = ["DELIVERABLES", "SDLC_MAP", "STAGES", "Finding", "Refused", "Status", "approve", "change", "guide", "line",
           "next_gate", "projects", "retire", "scaffold", "status"]
