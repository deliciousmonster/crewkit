"""The lint over one project's deliverables. Errors fail the set; warnings fail it only under --strict."""
from __future__ import annotations

import re
from pathlib import Path

from crewkit.sdlc import GATES, STAGES, Finding, Set
from crewkit.sdlc.doc import REQ_ID, Doc, as_int, digest

REQUIRED_AT = {"interview": ["interview.md"], "requirements": ["requirements.md"],
               "design": ["design/index.md", "design/feedback.md"], "development": ["development.md"]}
HEADINGS = {
    "brief.md": ["What we want to deliver", "Who it is for and why", "Informal requirements", "Sources", "Outputs",
                 "Constraints and out of scope"],
    "requirements.md": ["Summary", "Requirements", "Out of scope", "Assumptions and open items", "Changes", "Sign-off"],
    "design/index.md": ["Components", "How they interact", "Data contracts", "Infrastructure", "Order of development",
                        "Success criteria", "Example functions", "Folder structure", "Sign-off"],
    "development.md": ["Order", "Tasks", "Acceptance", "Blocked and dropped", "Sign-off"],
    "component": ["Serves", "Interface", "Failure semantics", "Notes"],
}
STATUSES = {"brief.md": {"draft", "ready"}, "interview.md": {"open", "complete"},
            "requirements.md": {"draft", "in-review", "approved", "amended"},
            "design/index.md": {"draft", "in-review", "approved"},
            "development.md": {"planned", "active", "blocked", "accepted"}}
TASK_STATUS = {"todo", "doing", "blocked", "review", "done", "dropped"}
Q_STATE = {"open", "answered", "deferred", "withdrawn"}


class _Lint:
    def __init__(self, project: Path):
        self.root = Path(project)
        self.s = Set(project)
        self.out: list[Finding] = []

    def add(self, level: str, code: str, doc: Doc | Path | str, message: str) -> None:
        path = doc.path if isinstance(doc, Doc) else Path(doc)
        rel = str(path.relative_to(self.root)) if path.is_absolute() and path.is_relative_to(self.root) else str(path)
        self.out.append(Finding(level, code, rel, message))

    def docs(self) -> dict[str, Doc]:
        s = self.s
        named = {"brief.md": s.brief, "interview.md": s.interview, "requirements.md": s.requirements,
                 "design/index.md": s.index, "design/feedback.md": s.feedback, "development.md": s.development}
        return {k: v for k, v in named.items() if v}

    def run(self) -> list[Finding]:
        for check in (self.unreadable, self.deliverables, self.shape, self.gates, self.ids, self.coverage, self.staleness, self.progress,
                      self.shared):
            check()
        return self.out

    def deliverables(self) -> None:
        stage, s = self.s.stage(), self.s
        reached = [f for st, files in REQUIRED_AT.items() if STAGES.index(st) <= STAGES.index(stage) for f in files]
        for rel in reached:
            if not (self.root / rel).is_file():
                self.add("error", "missing-deliverable", rel, f"missing at stage {stage}; `sdlc init` writes it")
        if s.index and s.profile() == "full" and not s.components and STAGES.index(stage) >= STAGES.index("design"):
            self.add("warning", "missing-component", s.index, "profile full and no component doc under design/")

    def headings(self, d: Doc, wants: list[str]) -> None:
        have = [h.lower() for h in d.headings()]
        for want in wants:
            if not any(h.startswith(want.lower()) for h in have):
                self.add("error", "missing-heading", d, f"no `## {want}`")

    def unreadable(self) -> None:
        s = self.s
        for d in [*self.docs().values(), *s.components]:
            if d.error:
                self.add("error", "unreadable", d, d.error)
        numbers: list[tuple[Doc | None, str]] = [(s.requirements, "rev"), *((d, "requirements_rev") for d in self.design_docs())]
        for doc, key in numbers:
            if doc and doc.meta.get(key) is not None and as_int(doc.meta[key]) is None:
                self.add("error", "unreadable", doc, f"{key} {doc.meta[key]!r} is not a number")
        for gate in GATES:
            for r in s.signoffs(gate):
                if as_int(r.get("rev")) is None:
                    self.add("error", "unreadable", s.doc(gate) or "", f"sign-off rev {r.get('rev')!r} is not a number")

    def shape(self) -> None:
        for rel, d in self.docs().items():
            self.headings(d, HEADINGS.get(rel, []))
            if rel in STATUSES and d.status() not in STATUSES[rel]:
                self.add("error", "unknown-status", d, f"status {d.status()!r} is not one of {', '.join(sorted(STATUSES[rel]))}")
        for d in self.s.components:
            self.headings(d, HEADINGS["component"])
        for t in self.s.tasks():
            if t.get("status") not in TASK_STATUS:
                self.add("error", "unknown-status", self.s.development or "development.md",
                         f"{t.get('id', '?')} status {t.get('status')!r} is not one of {', '.join(sorted(TASK_STATUS))}")
        self.questions()

    def questions(self) -> None:
        iv = self.s.interview
        for e in iv.entries() if iv else []:
            state = (e.fields.get("state") or "open").split()[0]
            if state not in Q_STATE:
                self.add("error", "unknown-status", iv or "", f"{e.id} state {state!r} is not one of {', '.join(sorted(Q_STATE))}")
        if iv and iv.status() == "complete" and (left := self.s.open_questions()):
            self.add("error", "interview-open", iv, f"status complete with open questions: {', '.join(left)}")

    def gates(self) -> None:
        s = self.s
        stage = STAGES.index(s.stage())
        for gate in GATES:
            doc = s.doc(gate)
            rows = s.signoffs(gate)
            if doc is None or not rows:
                continue
            for r in rows:
                if not r.get("words", "").strip('" '):
                    self.add("error", "signoff-no-words", doc, f"{GATES[gate]} sign-off rev {r.get('rev', '?')} has no words")
            if s.signed(gate) and stage < STAGES.index(gate):
                self.add("error", "out-of-order", doc, f"{GATES[gate]} signed while the project is at stage {s.stage()}")
            if (signed := rows[-1].get("hash", "")) and signed != s.hash(gate):
                self.drifted(gate, doc)

    def drifted(self, gate: str, doc: Doc) -> None:
        if gate == "requirements":
            changed, proposed = self.s.changed_reqs(), self.s.proposed()
            known = set(self.s.reqs())
            covered = set().union(*(a for a in proposed.values() if a <= known))
            if changed and changed <= covered:
                self.add("warning", "pending-change", doc, f"{', '.join(sorted(changed))} edited since sign-off; "
                         f"awaiting {', '.join(proposed)}")
            else:
                what = ", ".join(sorted(changed or {"the Requirements section"}))
                self.add("error", "edited-after-signoff", doc, f"{what} changed after sign-off with no change request naming it")
        elif gate == "design":
            self.add("error", "edited-after-signoff", doc, "design changed after G2; record a feedback round and re-approve")
        else:
            self.add("error", "edited-after-signoff", doc, "tasks or acceptance changed after G3")

    def ids(self) -> None:
        for d in [*self.docs().values()]:
            seen: set[str] = set()
            for e in d.entries():
                if e.id in seen:
                    self.add("error", "duplicate-id", d, f"{e.id} appears twice")
                seen.add(e.id)
        task_ids = [t.get("id", "") for t in self.s.tasks()]
        for dup in sorted({i for i in task_ids if i and task_ids.count(i) > 1}):
            self.add("error", "duplicate-id", self.s.development or "development.md", f"{dup} appears twice")
        if not self.s.requirements:
            return
        known = set(self.s.reqs())
        for t in self.s.tasks():
            for r in sorted(set(REQ_ID.findall(t.get("serves", ""))) - known):
                self.add("error", "unknown-req", self.s.development or "", f"{t.get('id', '?')} serves {r}, which does not exist")
        for d in self.design_docs():
            for r in sorted(set(REQ_ID.findall(d.body)) - known):
                self.add("error", "unknown-req", d, f"cites {r}, which does not exist")

    def design_docs(self) -> list[Doc]:
        return [d for d in [self.s.index, *self.s.components] if d]

    def coverage(self) -> None:
        s = self.s
        for i, f in s.reqs().items():
            if not (f.get("success") and f.get("acceptance")):
                self.add("warning", "req-incomplete", s.requirements or "", f"{i} has no Success or Acceptance")
        must = [i for i, f in s.reqs().items() if f.get("priority", "").startswith("must")]
        reached = STAGES.index(s.stage())
        if s.index and reached >= STAGES.index("design"):
            cited = {r for d in self.design_docs() for r in REQ_ID.findall(d.body)}
            for i in must:
                if i not in cited:
                    self.add("warning", "uncovered-design", s.index, f"must {i} is cited by no design doc")
        if s.development and reached >= STAGES.index("development"):
            served = {r for t in s.tasks() for r in REQ_ID.findall(t.get("serves", ""))}
            for i in must:
                if i not in served:
                    self.add("warning", "uncovered-task", s.development, f"must {i} is served by no task")

    def staleness(self) -> None:
        s = self.s
        if s.interview and s.brief and (bh := str(s.interview.meta.get("brief_hash") or "")) and bh != digest(s.brief.body):
            self.add("warning", "stale-interview", s.interview, "the brief changed since brief_hash; re-open the interview")
        if not s.requirements:
            return
        revs: dict[str, int] = {}
        for e in s.requirements.entries("Changes"):
            if e.fields.get("status") == "approved" and (rev := re.match(r"\d+", e.fields.get("rev", ""))):
                for r in REQ_ID.findall(e.fields.get("affects", "")):
                    revs[r] = max(revs.get(r, 1), int(rev.group()))
        base = as_int(s.index.meta.get("requirements_rev") if s.index else 1) or 1
        for d in self.design_docs():
            have = as_int(d.meta.get("requirements_rev") or base) or base
            stale = sorted(r for r in set(REQ_ID.findall(d.body)) if revs.get(r, 1) > have)
            if stale:
                self.add("warning", "stale-design", d, f"requirements_rev {have} is below the rev of {', '.join(stale)}")

    def progress(self) -> None:
        s, dev = self.s, self.s.development
        if dev is None:
            return
        ws = self.root.parent.parent if self.root.parent.name == "projects" else self.root
        tracked = str(dev.meta.get("tracker") or "none") != "none"
        for t in s.tasks():
            ev = t.get("evidence", "").strip("` ")
            if t.get("status") == "done" and not (ev and ((self.root / ev).exists() or (ws / ev).exists())):
                self.add("warning", "missing-evidence", dev, f"{t.get('id', '?')} is done and its evidence {ev or '(none)'} does not exist")
            if tracked and not t.get("ticket", "").strip("- "):
                self.add("warning", "no-ticket", dev, f"{t.get('id', '?')} has no ticket and tracker is set")
        if s.feedback and s.signed("design"):
            open_fb = [e.id for e in s.feedback.entries() if e.fields.get("disposition", "open").startswith("open")]
            if open_fb:
                self.add("warning", "open-feedback", s.feedback, f"design approved with open feedback: {', '.join(open_fb)}")

    def shared(self) -> None:
        for d in (self.s.brief, self.s.requirements):
            other = str(d.meta.get("shared_with") or "") if d else ""
            if not d or not other:
                continue
            path = Path(other).expanduser()
            path = path if path.is_absolute() else d.path.parent / path
            if not path.is_file():
                continue
            try:
                theirs = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                self.add("error", "unreadable", d, f"its shared copy {other} is not UTF-8 text")
                continue
            if _shared_hash(theirs) != _shared_hash(d.text):
                self.add("warning", "shared-drift", d, f"differs from its shared copy {other}")


def _shared_hash(text: str) -> str:
    return digest(re.sub(r"(?m)^shared_with:.*$", "", text))


def lint(project: Path) -> list[Finding]:
    """Every finding for one project, errors first."""
    found = _Lint(project).run()
    return sorted(found, key=lambda f: f.level != "error")
