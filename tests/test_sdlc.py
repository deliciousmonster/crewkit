"""crewkit.sdlc on synthetic workspaces: the stage at each step, each lint code, the gates, change requests,
profiles, shared copies, retire, the CLI, and a 30,000-file workspace read only through its deliverables."""
import builtins
import contextlib
import io
import json
import os
import re
import subprocess
import time
import unittest
from pathlib import Path
from unittest import mock

from crewkit import sdlc
from crewkit.install import main as crewkit_main
from crewkit.sdlc.__main__ import main as cli
from crewkit.sdlc.doc import Doc, digest
from crewkit.sdlc.lint import lint
from installcase import scratch

REQS = """
### REQ-001 Nightly export
- Statement: the export runs every night
- Source: brief, Informal requirements
- Priority: must
- Success: an export file each day for a week
- Acceptance: `make check-export` passes

### REQ-002 Summary page
- Statement: a summary page lists the exports
- Source: Q-001
- Priority: should
- Success: the page lists the last seven exports
- Acceptance: a manual check of the page
"""
COMPONENT = """---
requirements_rev: 1
---
# Exporter

## Serves

REQ-001

## Interface

`export(day) -> Path`

## Failure semantics

A failed export leaves no partial file.

## Notes
"""


def edit(path: Path, old: str, new: str, count: int = 1):
    text = path.read_text()
    assert old in text, f"{old!r} not in {path}"
    path.write_text(text.replace(old, new, count))


def build(root: Path, upto: str, profile: str = "full") -> Path:
    """A project carried to the start of stage `upto`, each step through the public API."""
    sdlc.scaffold(root, profile)
    steps = sdlc.STAGES[1: sdlc.STAGES.index(upto) + 1]
    if "interview" in steps:
        edit(root / "brief.md", "status: draft", "status: ready")
    if "requirements" in steps:
        (root / "interview.md").write_text(
            "---\nstatus: complete\n---\n# Interview\n\n### Q-001 Pages\n- Question: which pages?\n"
            "- Answer: \"the summary\"\n- State: answered\n- Feeds: REQ-002\n")
    if "design" in steps:
        edit(root / "requirements.md", "## Out of scope", REQS + "\n## Out of scope")
        sdlc.approve(root, "requirements", "owner", "yes, build it")
    if "development" in steps:
        edit(root / "design/index.md", "|---|---|---|\n", "|---|---|---|\n| Exporter | exporter.md | REQ-001, REQ-002 |\n")
        if profile == "full":
            (root / "design/exporter.md").write_text(COMPONENT)
        sdlc.approve(root, "design", "owner", "looks right")
        edit(root / "development.md", "| Ticket | Evidence |\n|---|---|---|---|---|---|---|---|\n",
             "| Ticket | Evidence |\n|---|---|---|---|---|---|---|---|\n| TASK-001 | Build the exporter | REQ-001, REQ-002 | "
             "engineer | todo | exporter | - | |\n")
    if "acceptance" in steps:
        (root / "evidence/2026-01-02").mkdir(parents=True)
        (root / "evidence/2026-01-02/run.log").write_text("ok\n")
        edit(root / "development.md", "| todo | exporter | - | |", "| done | exporter | - | evidence/2026-01-02/run.log |")
        edit(root / "development.md", "| Result | Evidence |\n|---|---|---|---|\n", "| Result | Evidence |\n|---|---|---|---|\n| REQ-001 | make check-export | pass | "
             "evidence/2026-01-02/run.log |\n")
    if "done" in steps:
        sdlc.approve(root, "acceptance", "owner", "accepted")
    return root


def codes(project: Path) -> dict[str, str]:
    return {f.code: f.level for f in lint(project)}


class Stages(unittest.TestCase):
    def setUp(self):
        self.tmp = scratch(self)

    def test_each_stage_and_its_next_gate(self):
        want = {"brief": "Brief:", "interview": "Interview:", "requirements": "G1:", "design": "G2:",
                "development": "Development:", "acceptance": "G3:", "done": "Done:"}
        for stage, opening in want.items():
            with self.subTest(stage=stage):
                p = build(self.tmp / stage, stage)
                st = sdlc.status(p)
                self.assertEqual(st.stage, stage)
                self.assertTrue(st.next.startswith(opening), st.next)
                self.assertEqual([f for f in st.findings if f.level == "error"], [])

    def test_a_fresh_scaffold_lints_clean(self):
        self.assertEqual(lint(build(self.tmp, "brief")), [])

    def test_a_finished_project_lints_clean(self):
        self.assertEqual(lint(build(self.tmp, "done")), [])

    def test_an_open_question_holds_the_interview(self):
        p = build(self.tmp, "requirements")
        edit(p / "interview.md", "State: answered", "State: open")
        self.assertEqual(sdlc.status(p).stage, "interview")
        self.assertIn("1 open question", sdlc.next_gate(p))

    def test_development_needs_every_must_accepted(self):
        p = build(self.tmp, "acceptance")
        edit(p / "development.md", "| pass |", "| fail |")
        self.assertEqual(sdlc.status(p).stage, "development")
        self.assertIn("1 must requirement(s) lack", sdlc.next_gate(p))

    def test_projects_are_the_root_and_projects_folders(self):
        build(self.tmp, "brief")
        build(self.tmp / "projects/b", "brief")
        build(self.tmp / "projects/a", "brief")
        (self.tmp / "projects/notes").mkdir()
        self.assertEqual(sdlc.projects(self.tmp), [self.tmp, self.tmp / "projects/a", self.tmp / "projects/b"])
        self.assertEqual(sdlc.line(self.tmp).count(": brief; next"), 3)

    def test_line_without_a_brief(self):
        self.assertIn("no brief.md", sdlc.line(self.tmp))


class Gates(unittest.TestCase):
    def setUp(self):
        self.p = build(scratch(self), "requirements")
        edit(self.p / "requirements.md", "## Out of scope", REQS + "\n## Out of scope")

    def test_approve_writes_the_row_status_and_hash(self):
        path = sdlc.approve(self.p, "requirements", "owner", '"yes | build it"', kind="adopted")
        doc = Doc.read(path)
        row = doc.table("Sign-off")[-1]
        self.assertEqual((row["rev"], row["approver"], row["kind"], row["words"]), ("1", "owner", "adopted", '"yes | build it"'))
        self.assertEqual(row["hash"], digest(doc.section("Requirements")))
        self.assertEqual(doc.status(), "approved")

    def test_refusals_write_nothing(self):
        before = (self.p / "requirements.md").read_text()
        cases = {
            "words are required": lambda: sdlc.approve(self.p, "requirements", "owner", "  "),
            "at stage requirements": lambda: sdlc.approve(self.p, "design", "owner", "ok"),
            "kind is approved or adopted": lambda: sdlc.approve(self.p, "requirements", "owner", "ok", kind="maybe"),
            "gate is requirements": lambda: sdlc.approve(self.p, "build", "owner", "ok"),
        }
        for msg, call in cases.items():
            with self.subTest(msg), self.assertRaisesRegex(sdlc.Refused, msg):
                call()
        self.assertEqual((self.p / "requirements.md").read_text(), before)

    def test_a_must_without_acceptance_is_refused(self):
        edit(self.p / "requirements.md", "- Acceptance: `make check-export` passes\n", "")
        with self.assertRaisesRegex(sdlc.Refused, "REQ-001"):
            sdlc.approve(self.p, "requirements", "owner", "ok")

    def test_an_assumed_source_must_be_listed(self):
        edit(self.p / "requirements.md", "- Source: Q-001", "- Source: assumed")
        with self.assertRaisesRegex(sdlc.Refused, "assumed entries .* REQ-002"):
            sdlc.approve(self.p, "requirements", "owner", "ok")
        edit(self.p / "requirements.md", "## Assumptions and open items\n", "## Assumptions and open items\n\n- REQ-002\n")
        sdlc.approve(self.p, "requirements", "owner", "ok")

    def test_an_edit_after_signoff_is_an_error(self):
        sdlc.approve(self.p, "requirements", "owner", "ok")
        self.assertNotIn("edited-after-signoff", codes(self.p))
        edit(self.p / "requirements.md", "every night", "every hour")
        self.assertEqual(codes(self.p)["edited-after-signoff"], "error")

    def test_an_edit_outside_the_signed_section_is_not(self):
        sdlc.approve(self.p, "requirements", "owner", "ok")
        edit(self.p / "requirements.md", "## Summary\n", "## Summary\n\nA nightly export.\n")
        self.assertNotIn("edited-after-signoff", codes(self.p))


class ChangeRequests(unittest.TestCase):
    def test_a_cr_bumps_rev_and_stales_the_design_until_reread(self):
        p = build(scratch(self), "development")
        self.assertEqual(lint(p), [])
        cr = sdlc.change(p, "Hourly export", ["REQ-001"], why="fresher data")
        self.assertEqual(cr, "CR-001")
        edit(p / "requirements.md", "every night", "every hour")
        self.assertEqual(codes(p), {"pending-change": "warning"})
        with self.assertRaisesRegex(sdlc.Refused, "no proposed change"):
            sdlc.approve(p, "CR-009", "owner", "ok")
        sdlc.approve(p, "CR-001", "owner", "hourly is fine")
        req = Doc.read(p / "requirements.md")
        self.assertEqual((req.meta["rev"], req.status()), (2, "amended"))
        self.assertEqual(req.table("Sign-off")[-1]["rev"], "2")
        self.assertEqual({e.id: e.fields for e in req.entries("Changes")}["CR-001"]["rev"], "2")
        found = lint(p)
        self.assertEqual({f.code for f in found}, {"stale-design"})
        self.assertEqual(sorted(f.path for f in found), ["design/exporter.md", "design/index.md"])
        for doc in ("design/index.md", "design/exporter.md"):
            edit(p / doc, "requirements_rev: 1", "requirements_rev: 2")
        self.assertEqual(lint(p), [])  # front matter is outside the G2 hash, so a re-read needs no new approval
        self.assertEqual(sdlc.status(p).stage, "development")

    def test_change_refuses_a_bad_id(self):
        p = build(scratch(self), "design")
        with self.assertRaisesRegex(sdlc.Refused, "not REQ ids"):
            sdlc.change(p, "x", ["REQ-1x"])


class LintCodes(unittest.TestCase):
    """Each code from a minimal failing file."""

    def setUp(self):
        self.tmp = scratch(self)

    def at(self, stage: str, profile: str = "full") -> Path:
        return build(self.tmp / f"{stage}-{profile}", stage, profile)

    def test_missing_deliverable(self):
        p = self.at("design")
        (p / "design/feedback.md").unlink()
        self.assertEqual(codes(p)["missing-deliverable"], "error")

    def test_missing_heading(self):
        p = self.at("brief")
        edit(p / "brief.md", "## Sources", "## Data")
        self.assertEqual(codes(p)["missing-heading"], "error")

    def test_missing_heading_in_a_component(self):
        p = self.at("development")
        edit(p / "design/exporter.md", "## Failure semantics", "## Failures")
        self.assertEqual(codes(p)["missing-heading"], "error")

    def test_unknown_status(self):
        for rel, old, new in (("brief.md", "status: draft", "status: done"),
                              ("interview.md", "State: answered", "State: maybe")):
            p = self.at("requirements") if rel == "interview.md" else self.at("brief")
            with self.subTest(rel):
                edit(p / rel, old, new)
                self.assertEqual(codes(p)["unknown-status"], "error")

    def test_unknown_task_status(self):
        p = self.at("acceptance")
        edit(p / "development.md", "| done |", "| finished |")
        self.assertEqual(codes(p)["unknown-status"], "error")

    def test_out_of_order(self):
        p = self.at("design")
        edit(p / "design/index.md", "|---|---|---|---|---|---|\n", "|---|---|---|---|---|---|\n| 1 | 2026-01-01 | owner | approved | \"ok\" | x |\n")
        edit(p / "requirements.md", "| owner | approved |", "| owner | approved | \"\" |", 1)
        found = codes(p)
        self.assertEqual(found["out-of-order"], "error")
        self.assertEqual(found["signoff-no-words"], "error")

    def test_design_edited_after_g2(self):
        p = self.at("development")
        edit(p / "design/exporter.md", "no partial file", "a partial file")
        self.assertEqual(codes(p)["edited-after-signoff"], "error")

    def test_delivery_edited_after_g3(self):
        p = self.at("done")
        edit(p / "development.md", "Build the exporter", "Build it")
        self.assertEqual(codes(p)["edited-after-signoff"], "error")

    def test_duplicate_id(self):
        p = self.at("design")
        edit(p / "requirements.md", "### REQ-002", "### REQ-001")
        self.assertEqual(codes(p)["duplicate-id"], "error")

    def test_duplicate_task(self):
        p = self.at("acceptance")
        text = (p / "development.md").read_text()
        row = re.search(r"(?m)^\| TASK-001 .*\n", text).group(0)
        (p / "development.md").write_text(text.replace(row, row + row))
        self.assertEqual(codes(p)["duplicate-id"], "error")

    def test_unknown_req(self):
        p = self.at("acceptance")
        edit(p / "development.md", "| REQ-001, REQ-002 | engineer", "| REQ-007 | engineer")
        edit(p / "design/exporter.md", "REQ-001\n", "REQ-001, REQ-009\n")
        msgs = [f.message for f in lint(p) if f.code == "unknown-req"]
        self.assertEqual(len(msgs), 2, msgs)

    def test_interview_open(self):
        p = self.at("requirements")
        edit(p / "interview.md", "State: answered", "State: open")
        self.assertEqual(codes(p)["interview-open"], "error")

    def test_req_incomplete(self):
        p = self.at("design")
        edit(p / "requirements.md", "- Success: the page lists the last seven exports\n", "")
        self.assertEqual(codes(p)["req-incomplete"], "warning")

    def test_uncovered_design_and_task(self):
        p = self.at("development")
        for rel in ("design/index.md", "design/exporter.md", "development.md"):
            (p / rel).write_text((p / rel).read_text().replace("REQ-001", "REQ-002"))
        found = codes(p)
        self.assertEqual(found["uncovered-design"], "warning")
        self.assertEqual(found["uncovered-task"], "warning")

    def test_stale_interview(self):
        p = self.at("requirements")
        brief = Doc.read(p / "brief.md")
        edit(p / "interview.md", "status: complete", f"status: complete\nbrief_hash: {digest(brief.body)}")
        self.assertNotIn("stale-interview", codes(p))
        edit(p / "brief.md", "## Outputs\n", "## Outputs\n\nA CSV a day.\n")
        self.assertEqual(codes(p)["stale-interview"], "warning")

    def test_missing_evidence(self):
        p = self.at("acceptance")
        (p / "evidence/2026-01-02/run.log").unlink()
        self.assertEqual(codes(p)["missing-evidence"], "warning")

    def test_open_feedback(self):
        p = self.at("development")
        (p / "design/feedback.md").write_text("# Design feedback\n\n## Round 1 (2026-01-01)\n\n### FB-001 Retry\n- From: reviewer\n- Disposition: open\n")
        self.assertEqual(codes(p)["open-feedback"], "warning")

    def test_no_ticket(self):
        p = self.at("acceptance")
        edit(p / "development.md", "tracker: none", "tracker: linear")
        self.assertEqual(codes(p)["no-ticket"], "warning")

    def test_shared_drift(self):
        a, b = self.at("brief"), self.tmp / "other"
        b.mkdir()
        edit(a / "brief.md", "shared_with:", "shared_with: ../other/brief.md")
        (b / "brief.md").write_text((a / "brief.md").read_text().replace("../other/brief.md", "../brief/brief.md"))
        self.assertNotIn("shared-drift", codes(a))
        edit(b / "brief.md", "## Outputs\n", "## Outputs\n\nDiverged.\n")
        self.assertEqual(codes(a)["shared-drift"], "warning")

    def test_light_needs_no_component_doc_and_full_does(self):
        light, full = self.at("development", "light"), self.at("development")
        (full / "design/exporter.md").unlink()
        self.assertEqual(lint(light), [])
        self.assertEqual(codes(full)["missing-component"], "warning")


class Retire(unittest.TestCase):
    def setUp(self):
        self.ws = scratch(self) / "agents"
        files = {"targets/demo/PLAN.md": "plan\n", "state/state.md": "See targets/demo/PLAN.md and agents/targets/demo/PLAN.md.\n",
                 "knowledge/x.md": "Per `targets/demo/PLAN.md`. Not targets/demo/PLAN.md.bak.\n",
                 "journal/2026-01-01.md": "Wrote targets/demo/PLAN.md.\n", "evidence/2026-01-01/n.md": "targets/demo/PLAN.md\n"}
        for rel, text in files.items():
            (self.ws / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.ws / rel).write_text(text)

    def test_moves_rewrites_outside_history_and_logs(self):
        done = sdlc.retire(self.ws, ["targets/demo/PLAN.md"])
        self.assertEqual((self.ws / "archive/pre-sdlc/targets/demo/PLAN.md").read_text(), "plan\n")
        self.assertFalse((self.ws / "targets/demo/PLAN.md").exists())
        new = "archive/pre-sdlc/targets/demo/PLAN.md"
        self.assertEqual((self.ws / "state/state.md").read_text(), f"See {new} and agents/{new}.\n")
        self.assertEqual((self.ws / "knowledge/x.md").read_text(), f"Per `{new}`. Not targets/demo/PLAN.md.bak.\n")
        self.assertEqual((self.ws / "journal/2026-01-01.md").read_text(), "Wrote targets/demo/PLAN.md.\n")
        self.assertEqual((self.ws / "evidence/2026-01-01/n.md").read_text(), "targets/demo/PLAN.md\n")
        self.assertIn(f"| `targets/demo/PLAN.md` | `{new}` |", (self.ws / "archive/path-moves.md").read_text())
        self.assertEqual(sorted(done), sorted([f"retire: targets/demo/PLAN.md -> {new}", "reference: knowledge/x.md", "reference: state/state.md"]))

    def test_refuses_a_missing_or_taken_path(self):
        with self.assertRaisesRegex(sdlc.Refused, "not a file"):
            sdlc.retire(self.ws, ["nope.md"])
        (self.ws / "archive/pre-sdlc/targets/demo").mkdir(parents=True)
        (self.ws / "archive/pre-sdlc/targets/demo/PLAN.md").write_text("old\n")
        with self.assertRaisesRegex(sdlc.Refused, "exists"):
            sdlc.retire(self.ws, ["targets/demo/PLAN.md"])
        self.assertTrue((self.ws / "targets/demo/PLAN.md").exists())

    def test_a_tracked_file_moves_with_git_mv(self):
        run = lambda *a: subprocess.run(["git", "-C", str(self.ws), "-c", "user.name=t", "-c", "user.email=t@t", *a],  # noqa: E731
                                        check=True, capture_output=True, text=True).stdout
        run("init", "-q")
        run("add", "targets/demo/PLAN.md")
        run("commit", "-qm", "init")
        sdlc.retire(self.ws, ["targets/demo/PLAN.md"])
        self.assertIn("R  targets/demo/PLAN.md -> archive/pre-sdlc/targets/demo/PLAN.md", run("status", "--short"))


class Cli(unittest.TestCase):
    def setUp(self):
        self.ws = scratch(self)

    def run_cli(self, *argv: str, via_crewkit: bool = False) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = crewkit_main(["sdlc", "--ws", str(self.ws), *argv]) if via_crewkit else cli(["--ws", str(self.ws), *argv])
        return code, out.getvalue() + err.getvalue()

    def test_init_status_next_lint_guide(self):
        self.assertEqual(self.run_cli("lint")[0], 1)  # no project yet
        code, out = self.run_cli("init", "--profile", "light")
        self.assertEqual((code, out.count("wrote")), (0, 6))
        self.assertIn("profile: light", (self.ws / "brief.md").read_text())
        self.assertIn("nothing written", self.run_cli("init")[1])
        code, out = self.run_cli("status", via_crewkit=True)
        self.assertIn(".: stage brief; G1 open, G2 open, G3 open; 0 error(s), 0 warning(s)", out)
        self.assertIn("brief_hash: ", out)
        self.assertTrue(self.run_cli("next")[1].startswith("Brief:"))
        self.assertEqual(self.run_cli("lint"), (0, ".: clean\n"))
        self.assertIn("# The agent SDLC", self.run_cli("guide")[1])

    def test_a_workdir_resolves_to_its_agents_folder(self):
        build(self.ws / "agents", "brief")
        code, out = self.run_cli("status")
        self.assertIn("stage brief", out)

    def test_lint_exit_codes_and_json(self):
        p = build(self.ws, "requirements")
        edit(p / "requirements.md", "## Out of scope", REQS.replace("- Success: the page lists the last seven exports\n", "") + "\n## Out of scope")
        self.assertEqual(self.run_cli("lint")[0], 0)
        self.assertEqual(self.run_cli("lint", "--strict")[0], 1)
        code, out = self.run_cli("lint", "--json")
        self.assertEqual(json.loads(out)["."][0]["code"], "req-incomplete")
        edit(p / "requirements.md", "### REQ-002", "### REQ-001")
        self.assertEqual(self.run_cli("lint")[0], 1)

    def test_approve_change_and_refusal(self):
        build(self.ws, "design")
        self.assertEqual(self.run_cli("approve", "design", "--words", "")[0], 1)
        code, out = self.run_cli("change", "Hourly", "--affects", "REQ-001")
        self.assertEqual((code, out.strip()), (0, "CR-001"))
        code, out = self.run_cli("approve", "CR-001", "--by", "owner", "--words", "fine")
        self.assertEqual(code, 0, out)
        code, out = self.run_cli("--project", "x", "approve", "requirements", "--words", "ok")
        self.assertEqual(code, 2)  # no such project

    def test_usage_is_2(self):
        self.assertEqual(self.run_cli("bogus")[0], 2)
        self.assertEqual(self.run_cli("approve", "design")[0], 2)

    def test_retire(self):
        build(self.ws, "brief")
        (self.ws / "PLAN.md").write_text("p\n")
        code, out = self.run_cli("retire", "PLAN.md")
        self.assertEqual(code, 0)
        self.assertTrue((self.ws / "archive/pre-sdlc/PLAN.md").exists())


class LargeWorkspace(unittest.TestCase):
    """30,000 files under evidence/ and more under journal/ and archive/: status, lint and retire open none."""

    def test_only_the_deliverables_are_read(self):
        ws = scratch(self)
        for top, n in (("evidence", 30_000), ("journal", 500), ("archive", 500)):
            for i in range(n):
                d = ws / top / f"d{i // 1000:03d}"
                if i % 1000 == 0:
                    d.mkdir(parents=True)
                (d / f"f{i}.md").write_text("targets/demo/PLAN.md\n")
        build(ws, "acceptance")
        (ws / "targets/demo").mkdir(parents=True)
        (ws / "targets/demo/PLAN.md").write_text("plan\n")
        touched: list[str] = []
        real_open, real_io_open, real_scandir, real_listdir = builtins.open, io.open, os.scandir, os.listdir

        def rec(real):
            def wrapper(path, *a, **k):
                touched.append(os.fspath(path) if not isinstance(path, int) else "")
                return real(path, *a, **k)
            return wrapper

        with mock.patch.object(builtins, "open", rec(real_open)), mock.patch.object(io, "open", rec(real_io_open)), \
                mock.patch.object(os, "scandir", rec(real_scandir)), mock.patch.object(os, "listdir", rec(real_listdir)):
            start = time.monotonic()
            st = sdlc.status(ws)
            found = lint(ws)
            elapsed = time.monotonic() - start
            sdlc.retire(ws, ["targets/demo/PLAN.md"])
        self.assertEqual((st.stage, found), ("acceptance", []))
        self.assertLess(elapsed, 2.0)
        self.assertIn(str(ws / "brief.md"), touched)  # the recorder sees reads
        rel = {str(Path(t).relative_to(ws)) for t in touched if t and Path(t).is_relative_to(ws)}
        history = sorted(r for r in rel if r.split("/")[0] in ("journal", "evidence") or r.startswith("archive/d"))
        self.assertEqual(history, [])
        self.assertEqual((ws / "evidence/d000/f0.md").read_text(), "targets/demo/PLAN.md\n")


if __name__ == "__main__":
    unittest.main()
