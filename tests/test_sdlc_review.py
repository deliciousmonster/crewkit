"""The break review's reproductions for crewkit.sdlc: each test failed on the commit its class names."""
from __future__ import annotations

import contextlib
import io
import unittest
from pathlib import Path

from crewkit import sdlc
from crewkit.sdlc.__main__ import main as cli
from crewkit.sdlc.doc import Doc
from installcase import scratch
from test_sdlc import COMPONENT, REQS, build, codes, edit


class ReviewRound1(unittest.TestCase):
    """The break review's reproductions, round 1; each failed on 38d42c2."""

    def setUp(self):
        self.tmp = scratch(self)

    def signed(self) -> Path:
        return build(self.tmp / "p", "design")

    def rows(self, p: Path) -> int:
        return len(Doc.read(p / "requirements.md").table("Sign-off"))

    def test_a_cr_naming_an_unknown_req_is_refused(self):
        p = self.signed()
        with self.assertRaisesRegex(sdlc.Refused, "REQ-009"):
            sdlc.change(p, "unrelated", ["REQ-009"])

    def test_a_cr_downgrades_only_an_edit_within_the_reqs_it_names(self):
        p = self.signed()
        edit(p / "requirements.md", "every night", "every week")
        sdlc.change(p, "unrelated", ["REQ-002"])
        self.assertEqual(codes(p).get("edited-after-signoff"), "error")
        sdlc.change(p, "weekly", ["REQ-001"])
        self.assertEqual({k: v for k, v in codes(p).items() if k != "uncovered-design"},
                         {"pending-change": "warning", "missing-component": "warning"})

    def test_a_second_approve_of_changed_requirements_is_refused(self):
        p = self.signed()
        edit(p / "requirements.md", "every night", "every week")
        with self.assertRaisesRegex(sdlc.Refused, "change"):
            sdlc.approve(p, "requirements", "owner", "fine")
        self.assertEqual(self.rows(p), 1)

    def test_approve_requirements_needs_a_ready_brief_and_a_closed_interview(self):
        for rel, old, new in (("brief.md", "status: ready", "status: draft"),
                              ("interview.md", "State: answered", "State: open")):
            with self.subTest(rel):
                p = build(self.tmp / rel, "requirements")
                edit(p / "requirements.md", "## Out of scope", REQS + "\n## Out of scope")
                edit(p / rel, old, new)
                with self.assertRaisesRegex(sdlc.Refused, "stage"):
                    sdlc.approve(p, "requirements", "owner", "ok")
                self.assertEqual(self.rows(p), 0)

    def test_approve_design_needs_a_clean_lint(self):
        p = build(self.tmp / "d", "design")
        edit(p / "design/index.md", "|---|---|---|\n", "|---|---|---|\n| Exporter | exporter.md | REQ-077 |\n")
        (p / "design/exporter.md").write_text(COMPONENT)
        with self.assertRaisesRegex(sdlc.Refused, "unknown-req"):
            sdlc.approve(p, "design", "owner", "ok")

    def test_approve_acceptance_needs_every_task_done(self):
        p = build(self.tmp / "a", "development")
        with self.assertRaisesRegex(sdlc.Refused, "stage development"):
            sdlc.approve(p, "acceptance", "owner", "ok")
        out = io.StringIO()
        with contextlib.redirect_stderr(out):
            self.assertEqual(cli(["--ws", str(p), "approve", "acceptance", "--words", "ok"]), 1)
        self.assertIn("stage development", out.getvalue())

    def test_a_gate_signed_before_its_stage_is_out_of_order(self):
        p = self.signed()
        edit(p / "brief.md", "status: ready", "status: draft")
        self.assertEqual(codes(p).get("out-of-order"), "error")

    def test_retire_rewrites_only_references_to_the_retired_file(self):
        ws = self.tmp / "r"
        for rel in ("PLAN.md", "targets/x/PLAN.md"):
            (ws / rel).parent.mkdir(parents=True, exist_ok=True)
            (ws / rel).write_text("plan\n")
        (ws / "state.md").write_text("see PLAN.md and targets/x/PLAN.md\n")
        sdlc.retire(ws, ["PLAN.md"])
        self.assertEqual((ws / "state.md").read_text(), "see archive/pre-sdlc/PLAN.md and targets/x/PLAN.md\n")
        sdlc.retire(ws, ["targets/x/PLAN.md"])
        self.assertEqual((ws / "state.md").read_text(),
                         "see archive/pre-sdlc/PLAN.md and archive/pre-sdlc/targets/x/PLAN.md\n")

    def test_malformed_input_is_a_finding_not_a_traceback(self):
        cases = [("design/index.md", "status: draft", "status: [unclosed"),
                 ("requirements.md", "| 1 |", "| x |"),
                 ("design/index.md", "requirements_rev: 1", "requirements_rev: two"),
                 ("requirements.md", "rev: 1", "rev: abc")]
        for i, (rel, old, new) in enumerate(cases):
            with self.subTest(new):
                p = build(self.tmp / f"m{i}", "design")
                edit(p / rel, old, new)
                self.assertEqual(codes(p).get("unreadable"), "error")
                sdlc.status(p)
        p = build(self.tmp / "shared", "brief")
        edit(p / "brief.md", "shared_with:", "shared_with: other.md")
        (p / "other.md").write_bytes(b"\xff\xfe bad")
        self.assertEqual(codes(p).get("unreadable"), "error")

    def test_a_cr_approval_refuses_edits_outside_its_reqs(self):
        p = self.signed()
        edit(p / "requirements.md", "every night", "every week")
        sdlc.change(p, "page", ["REQ-002"])
        with self.assertRaisesRegex(sdlc.Refused, "outside what it affects: REQ-001"):
            sdlc.approve(p, "CR-001", "owner", "ok")

    def test_a_deliverable_that_is_not_utf8_is_unreadable(self):
        p = build(self.tmp / "u", "brief")
        (p / "interview.md").write_bytes(b"\xff\xfe")
        self.assertEqual(codes(p).get("unreadable"), "error")

    def test_an_existing_project_is_chosen(self):
        build(self.tmp / "projects/a", "brief")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(cli(["--ws", str(self.tmp), "--project", "a", "status"]), 0)
        self.assertIn("projects/a: stage brief", out.getvalue())

    def test_a_bad_rev_refuses_a_cr_approval(self):
        p = self.signed()
        sdlc.change(p, "weekly", ["REQ-001"])
        edit(p / "requirements.md", "rev: 1", "rev: abc")
        with self.assertRaisesRegex(sdlc.Refused, "rev"):
            sdlc.approve(p, "CR-001", "owner", "ok")

    def test_a_missing_or_escaping_project_is_usage(self):
        build(self.tmp, "brief")
        for argv in (["--project", "nope", "status"], ["--project", "nope", "lint"], ["--project", "../x", "init"]):
            with self.subTest(argv), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli(["--ws", str(self.tmp), *argv]), 2)
                self.assertIn("project", err.getvalue())
        self.assertFalse((self.tmp.parent / "x").exists())

    def test_next_without_an_interview_says_to_write_it(self):
        p = build(self.tmp, "interview")
        (p / "interview.md").unlink()
        self.assertIn("interview.md", sdlc.next_gate(p))
        self.assertNotIn("0 open", sdlc.next_gate(p))
