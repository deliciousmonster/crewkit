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
from crewkit.sdlc.doc import REQ_ID, Doc, digest, insert_after_table, set_meta

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

    def __str__(self) -> str:
        return f"{self.level}: {self.path}: {self.message} [{self.code}]"


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
        self.brief = Doc.read(self.root / "brief.md")
        self.interview = Doc.read(self.root / "interview.md")
        self.requirements = Doc.read(self.root / "requirements.md")
        self.index = Doc.read(self.root / "design/index.md")
        self.feedback = Doc.read(self.root / "design/feedback.md")
        self.development = Doc.read(self.root / "development.md")
        design = self.root / "design"
        names = sorted(p.name for p in design.iterdir() if p.suffix == ".md") if design.is_dir() else []
        self.components = [d for n in names if n not in ("index.md", "feedback.md") and (d := Doc.read(design / n))]

    def profile(self) -> str:
        return str(self.brief.meta.get("profile") or "full") if self.brief else "full"

    def reqs(self) -> dict[str, dict[str, str]]:
        return {e.id: e.fields for e in self.requirements.entries("Requirements")} if self.requirements else {}

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
        return int(rows[-1].get("rev") or len(rows)) if rows else None

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
    doc = s.doc(gate)
    if doc is None:
        raise Refused(f"{gate}: the document is missing")
    earlier = list(GATES)[: list(GATES).index(gate)]
    if unsigned := [g for g in earlier if not s.signed(g)]:
        raise Refused(f"{gate}: {', '.join(unsigned)} not approved yet")
    if gate == "requirements":
        _check_requirements(s)
    rev = int(s.requirements.meta.get("rev") or 1) if gate == "requirements" and s.requirements else len(s.signoffs(gate)) + 1
    status_word = {"requirements": "approved", "design": "approved", "acceptance": "accepted"}[gate]
    text = insert_after_table(set_meta(doc.text, "status", status_word), "Sign-off", _row(rev, by, kind, words, s.hash(gate)))
    doc.path.write_text(text, encoding="utf-8")
    return doc.path


def _check_requirements(s: Set) -> None:
    reqs = s.reqs()
    if not reqs:
        raise Refused("requirements: no REQ entries to approve")
    gaps = [i for i, f in reqs.items() if f.get("priority", "").startswith("must") and not (f.get("success") and f.get("acceptance"))]
    if gaps:
        raise Refused(f"requirements: must entries without Success or Acceptance: {', '.join(gaps)}")
    listed = s.requirements.section("Assumptions") or "" if s.requirements else ""
    hidden = [i for i, f in reqs.items() if f.get("source", "").strip("` ").startswith("assumed") and i not in listed]
    if hidden:
        raise Refused(f"requirements: assumed entries not listed under Assumptions and open items: {', '.join(hidden)}")


def _approve_change(s: Set, cr: str, by: str, words: str, kind: str) -> Path:
    doc = s.requirements
    if doc is None or not s.signed("requirements"):
        raise Refused(f"{cr}: requirements are not approved yet, so there is nothing to change")
    entry = next((e for e in doc.entries("Changes") if e.id == cr), None)
    if entry is None or entry.fields.get("status") != "proposed":
        raise Refused(f"{cr}: no proposed change request by that id")
    rev = int(doc.meta.get("rev") or 1) + 1
    text = set_meta(set_meta(doc.text, "rev", rev), "status", "amended")
    head = re.search(rf"(?m)^### +{cr}\b.*$", text)
    assert head is not None  # the entry was parsed from this text
    block_end = re.compile(r"(?m)^#{2,3} ").search(text, head.end())
    end = block_end.start() if block_end else len(text)
    block = re.sub(r"(?m)^- +Status:.*$", f"- Status: approved\n- Rev: {rev}", text[head.end(): end], count=1)
    text = text[: head.end()] + block + text[end:]
    text = insert_after_table(text, "Sign-off", _row(rev, by, kind, words, s.hash("requirements")))
    doc.path.write_text(text, encoding="utf-8")
    return doc.path


def change(project: Path, title: str, affects: list[str], why: str = "", by: str = "owner") -> str:
    """Append a proposed CR under requirements.md's Changes; returns its id."""
    doc = Set(project).requirements
    if doc is None:
        raise Refused("change: requirements.md is missing")
    if bad := [a for a in affects if not REQ_ID.fullmatch(a)]:
        raise Refused(f"change: not REQ ids: {', '.join(bad)}")
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
        src = (ws / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
        if not src.is_relative_to(ws) or not src.exists():
            raise Refused(f"retire: {p} is not a file in {ws}")
        rel = str(src.relative_to(ws))
        dst = ws / "archive/pre-sdlc" / rel
        if dst.exists():
            raise Refused(f"retire: {dst} exists")
        moves.append((rel, f"archive/pre-sdlc/{rel}"))
    folder = Path(git(ws, "rev-parse", "--show-toplevel") or ws)
    for old, new in moves:
        src = ws / old
        tracked = bool(git(ws, "ls-files", "--", old))
        _move(folder, {"kind": "lift", "from": src, "to": ws / new, "how": "git" if tracked else "rename"})
    subs = [(re.compile(EDGE.format(re.escape(old))), new) for old, new in moves]
    rewritten = []
    for f in md_files(ws):
        text = new_text = f.read_text(encoding="utf-8")
        for rx, new in subs:
            new_text = rx.sub(new, new_text)
        if new_text != text:
            f.write_text(new_text, encoding="utf-8")
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
