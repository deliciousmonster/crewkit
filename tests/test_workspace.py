import os
import subprocess
import unittest
from pathlib import Path

from crewkit import migrate, sdlc, workspace
from crewkit.migrate import Layout
from installcase import scratch


def put(root: Path, rel: str, text: str):
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(text)


def git(d: Path, *args: str):
    subprocess.run(["git", "-C", str(d), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True, capture_output=True)


def snapshot(d: Path) -> dict:
    """Every path under d with its bytes (a link's target), so two runs can be compared exactly."""
    return {str(p.relative_to(d)): os.readlink(p) if p.is_symlink() else p.read_bytes() if p.is_file() else None
            for p in sorted(d.rglob("*")) if ".git" not in p.relative_to(d).parts}


def case_insensitive(d: Path) -> bool:
    probe = d / "CaseProbe"
    probe.write_text("")
    try:
        return (d / "caseprobe").exists()
    finally:
        probe.unlink()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = scratch(self)
        self.template = self.tmp / "template"
        for rel, text in {"README.md": "r\n", "state.md": "# State\n", "workspace.yaml": "vcs:\n  kind: {{vcs}}\n", "journal/README.md": "j\n"}.items():
            put(self.template, rel, text)
        self.layout = Layout("agents/dev", "agents/old", self.template, [(r"agents/old", "agents/dev"), (r"crews/oldcrew", "crews/dev")])
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q")


class Workspace(Base):
    def test_init_copies_the_template_once_and_fills_the_vcs(self):
        ws = self.repo / "agents/dev"
        self.assertIn("workspace.yaml", workspace.init(self.repo, ws, self.template))
        self.assertIn("kind: git", (ws / "workspace.yaml").read_text())
        (ws / "state.md").write_text("mine")
        self.assertEqual(workspace.init(self.repo, ws, self.template), [])
        self.assertEqual((ws / "state.md").read_text(), "mine")
        self.assertEqual(workspace.policy(self.repo, ws)["commit"], "ask")

    def test_init_with_sdlc_writes_the_six_deliverables_and_overwrites_none(self):
        put(self.template, "brief.md", "the crew's own brief\n")
        ws = self.repo / "agents/dev"
        put(ws, "development.md", "mine\n")
        written = workspace.init(self.repo, ws, self.template, sdlc=True)
        self.assertEqual(sorted(w for w in written if w in sdlc.DELIVERABLES),
                         ["brief.md", "design/feedback.md", "design/index.md", "interview.md", "requirements.md"])
        self.assertEqual((ws / "brief.md").read_text(), "the crew's own brief\n")
        self.assertEqual((ws / "development.md").read_text(), "mine\n")
        self.assertEqual(workspace.init(self.repo, ws, self.template, sdlc=True), [])

    def test_init_without_sdlc_writes_no_deliverable(self):
        ws = self.repo / "agents/dev"
        workspace.init(self.repo, ws, self.template)
        self.assertFalse(any((ws / rel).exists() for rel in sdlc.DELIVERABLES))

    def test_vcs_and_workdir(self):
        self.assertEqual(workspace.vcs_of(self.tmp / "template"), "none")
        (self.repo / "agents/dev/journal").mkdir(parents=True)
        self.assertEqual(workspace.workdir_of(self.repo / "agents/dev/journal", ["agents/dev"]), self.repo)

    def test_a_record_at_the_agents_root_names_its_workdir_only_when_it_holds_the_marker(self):
        put(self.repo, "agents/state/state.md", "# State\n")
        put(self.repo, "src/agents/tool.py", "")
        self.assertEqual(workspace.workdir_of(self.repo / "agents/state", ["agents"], "state/state.md"), self.repo)
        self.assertEqual(workspace.workdir_of(self.repo / "agents/journal/x.md", ["agents"], "state/state.md"), self.repo)
        self.assertEqual(workspace.workdir_of(self.repo / "src/agents", ["agents"], "state/state.md"), self.repo / "src/agents")
        self.assertEqual(workspace.policy(self.repo, self.repo / "agents/state")["kind"], "git")

    def test_find_records_names_older_layouts(self):
        put(self.repo, "agents/old/state.md", "s")
        put(self.repo, "notes/x/AGENTS.md", "a")
        put(self.repo, "notes/x/INTAKE.md", "i")
        put(self.repo, "node_modules/y/state.md", "s")
        self.assertEqual(workspace.find_records(self.repo, "agents/dev"), ["agents/old", "notes/x"])

    def test_find_records_skips_in_project_worktrees(self):
        put(self.repo, "agents/old/state.md", "s")
        put(self.repo, ".claude/worktrees/lane/state.md", "s")
        self.assertEqual(workspace.find_records(self.repo, "agents/dev"), ["agents/old"])


class Migrate(Base):
    def legacy(self):
        put(self.repo, "agents/old/state.md", "# State\n")
        put(self.repo, "agents/old/journal/2026-10-01.md", "# 2026-10-01\n\nhere\n")
        put(self.repo, "agents/old/evidence-2026-10-01/run.log", "ok\n")
        put(self.repo, "agents/old/soak-2026-10-02.log", "soak\n")
        put(self.repo, "agents/old/knowledge/OldFact.md", '---\nname: OldFact\ndescription: "a fact"\nmetadata:\n  node_type: memory\n  modified: 2026-09-01\n---\nbody\n')
        put(self.repo, ".claude/settings.local.json", '{"hook": "~/Developer/crews/oldcrew/state agents/old"}\n')
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")

    def test_kebab_and_frontmatter(self):
        self.assertEqual(migrate.kebab("OldFact.md"), "old-fact")
        slug, text, missing = migrate.normalize_knowledge("---\nname: OldFact\nmetadata:\n  node_type: memory\n---\nLoad when: testing\n", "OldFact.md")
        self.assertEqual((slug, missing), ("old-fact", False))
        self.assertTrue(text.startswith('---\nname: old-fact\ndescription: "Load when: testing"\nmetadata:\n  type: memory\n---\n\nLoad when'))

    def test_a_legacy_workspace_is_relocated_organized_formatted_and_linked(self):
        self.legacy()
        planned = migrate.plan(self.repo, self.layout)
        self.assertTrue((self.repo / "agents/old/state.md").exists(), "a plan changes nothing")
        migrate.apply(planned)
        ws = self.repo / "agents/dev"
        self.assertTrue((self.repo / "agents/old").is_symlink())
        self.assertEqual((self.repo / "agents/old/evidence-2026-10-01/run.log").read_text(), "ok\n")
        self.assertTrue((ws / "evidence/2026-10-02/soak-2026-10-02.log").exists())
        self.assertIn("kind: git", (ws / "workspace.yaml").read_text())
        fact = (ws / "knowledge/old-fact.md").read_text()
        self.assertTrue(fact.startswith('---\nname: old-fact\ndescription: "a fact"\nmetadata:\n  type: memory\n  modified: 2026-09-01\n---\n\nbody'))
        self.assertFalse((ws / "knowledge/OldFact.md").exists())
        settings = (self.repo / ".claude/settings.local.json").read_text()
        self.assertIn("crews/dev", settings)
        self.assertIn("agents/dev", settings)
        status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True, text=True).stdout
        self.assertIn("R  agents/old/state.md -> agents/dev/state.md", status)
        self.assertIn("RM agents/old/knowledge/OldFact.md -> agents/dev/knowledge/old-fact.md", status)
        self.assertEqual(migrate.plan(self.repo, self.layout)["steps"], [], "a second run plans nothing")

    def test_a_case_only_rename_keeps_the_file(self):
        """knowledge/INDEX.md formats to index.md. On a case-insensitive volume the two names are one file, so writing
        index.md and unlinking INDEX.md deleted it; now it is renamed through a free name. Same result on either volume."""
        ws = self.repo / "agents/dev"
        put(ws, "state.md", "# State\n")
        put(ws, "knowledge/INDEX.md", "# Index\n\nLoad when: always\n")
        for tracked in (False, True):
            with self.subTest(tracked=tracked):
                if tracked:
                    put(ws, "knowledge/INDEX.md", "# Index\n\nLoad when: always\n")
                    git(self.repo, "add", ".")
                    git(self.repo, "commit", "-qm", "index")
                planned = migrate.plan(self.repo, Layout("agents/dev", None, self.template))
                step = next(s for s in planned["steps"] if s["kind"] == "format")
                self.assertEqual((step["from"].name, step["to"].name), ("INDEX.md", "index.md"))
                if case_insensitive(ws):
                    self.assertTrue(migrate.same(step["from"], step["to"]), "this volume folds case, so the rename is the hazard")
                migrate.apply(planned)
                self.assertEqual([p.name for p in (ws / "knowledge").iterdir()], ["index.md"])
                self.assertIn("Load when: always", (ws / "knowledge/index.md").read_text())
                self.assertTrue((ws / "knowledge/index.md").read_text().startswith("---\nname: index\n"))
                if tracked:
                    files = subprocess.run(["git", "-C", str(self.repo), "ls-files", "agents/dev/knowledge"], capture_output=True, text=True).stdout
                    self.assertEqual(files.split(), ["agents/dev/knowledge/index.md"], "the index follows the rename")
                (ws / "knowledge/index.md").unlink()

    def test_a_move_onto_the_same_file_renames_it(self):
        """A move whose two paths samefile() calls one file renames it, and never copies onto it and deletes the source."""
        ws = self.repo / "agents/dev"
        put(ws, "Soak.log", "soak\n")
        migrate.apply({"folder": self.repo, "steps": [{"kind": "organize", "from": ws / "Soak.log", "to": ws / "soak.log", "how": "rename"}]})
        self.assertEqual([p.name for p in ws.iterdir()], ["soak.log"])
        self.assertEqual((ws / "soak.log").read_text(), "soak\n")

    def test_records_elsewhere_are_aggregated_appended_and_collisions_kept(self):
        self.legacy()
        old = self.tmp / "local-thing"
        for rel, text in {"AGENTS.md": "# old\n", "CLAUDE.md": "@AGENTS.md\n", "state.md": "# Old state\n", "journal/2026-10-01.md": "# 2026-10-01\n\nthere\n"}.items():
            put(old, rel, text)
        planned = migrate.plan(self.repo, self.layout, [str(old)])
        self.assertTrue(any("state.from-local-thing.md" in r for r in planned["review"]))
        migrate.apply(planned)
        ws = self.repo / "agents/dev"
        self.assertEqual((ws / "AGENTS.md").read_text(), "# old\n")
        self.assertFalse((ws / "CLAUDE.md").exists())
        self.assertEqual((ws / "state.from-local-thing.md").read_text(), "# Old state\n")
        self.assertTrue((ws / "journal/2026-10-01.md").read_text().endswith("here\n\n## From local-thing\n\nthere\n"))

    def test_another_repositorys_record_lands_under_targets(self):
        old = self.tmp / "widget"
        put(old, "INTAKE.md", "# intake\n")
        migrate.apply(migrate.plan(self.tmp / "template", self.layout, [f"{old}=targets/widget"]))
        self.assertTrue((self.tmp / "template/agents/dev/targets/widget/INTAKE.md").exists())

    def test_a_worktree_workspace_moves_with_git_worktree_move(self):
        put(self.repo, "README.md", "r\n")
        git(self.repo, "add", "README.md")
        git(self.repo, "commit", "-qm", "init")
        git(self.repo, "worktree", "add", "-q", "--orphan", "-b", "memory", "agents/old")
        put(self.repo, "agents/old/state.md", "# State\n")
        planned = migrate.plan(self.repo, self.layout)
        self.assertEqual(planned["steps"][0]["how"], "worktree")
        migrate.apply(planned)
        branch = subprocess.run(["git", "-C", str(self.repo / "agents/dev"), "branch", "--show-current"], capture_output=True, text=True).stdout
        self.assertEqual(branch.strip(), "memory")
        self.assertTrue((self.repo / "agents/old").is_symlink())


class Lift(unittest.TestCase):
    """A legacy record inside the workspace, agents/old under agents, lifted into it."""

    def setUp(self):
        self.tmp = scratch(self)
        self.template = self.tmp / "template"
        for rel, text in {"AGENTS.md": "# Fresh\n\n<!-- map: x -->\n- map\n<!-- /map -->\n", "CLAUDE.md": "@AGENTS.md\n",
                          "state/state.md": "# State\n", "state/workspace.yaml": "vcs: {{vcs}}\n", "journal/README.md": "j\n",
                          "archive/README.md": "a\n"}.items():
            put(self.template, rel, text)
        self.layout = Layout("agents", "agents/old", self.template, moves={
            "README.md": "AGENTS.md", "state.md": "state/state.md", "workspace.yaml": "state/workspace.yaml",
            "manifest.json": "archive/manifest.json", "procedures": "knowledge/procedures"}, history=["journal", "evidence", "archive"],
            marker="state/state.md")
        self.repo = self.tmp / "repo"
        self.old = self.repo / "agents/old"
        for rel, text in {"README.md": "# Our record\n\nRead `state.md` first.\n", "state.md": "Gate: `agents/old/tools/gate.sh`; the record was agents/old.\n",
                          "workspace.yaml": "gate: agents/old/tools/gate.sh\n", "manifest.json": "{}\n", "MIGRATION.md": "m\n",
                          ".gitignore": "manifest.json\n", "journal/README.md": "j\n",
                          "journal/2026-10-01.md": "Ran agents/old/tools/gate.sh into agents/old/evidence-2026-10-01/.\n",
                          "knowledge/INDEX.md": "Paths are relative to `agents/old/knowledge/`; see agents/old/state.md.\n",
                          "tools/gate.sh": "out=agents/old/evidence/$(date +%F)\n", "evidence/2026-10-01/run.log": "ok\n",
                          "procedures/smoke.md": "Run agents/old/tools/gate.sh; see agents/old/procedures/smoke.md.\n",
                          "evidence-2026-10-02/soak.log": "soak\n"}.items():
            put(self.old, rel, text)
        (self.old / "evidence-2026-10-01").symlink_to("evidence/2026-10-01")
        self.repo.joinpath(".claude").mkdir()
        self.repo.joinpath(".claude/settings.json").write_text('{"cmd": "cat agents/old/state.md"}\n')
        put(self.repo, "docs/ci.md", "Smoke: agents/old/procedures/smoke.md.\n")
        git(self.repo, "init", "-q")

    def test_the_record_moves_up_with_only_the_map_files_at_its_root(self):
        planned = migrate.plan(self.repo, self.layout)
        self.assertEqual(planned["collisions"], [])
        self.assertTrue((self.old / "state.md").exists(), "a plan changes nothing")
        migrate.apply(planned)
        ws = self.repo / "agents"
        self.assertFalse(self.old.exists())
        self.assertEqual(sorted(p.name for p in ws.iterdir() if p.is_file()), [".gitignore", "AGENTS.md", "CLAUDE.md"])
        self.assertEqual(sorted(p.name for p in ws.iterdir() if p.is_dir()), ["archive", "evidence", "journal", "knowledge", "state", "tools"])
        agents_md = (ws / "AGENTS.md").read_text()
        self.assertTrue(agents_md.startswith("# Our record\n\nRead `state/state.md` first.\n\n<!-- map: x -->"), agents_md)
        self.assertEqual((ws / "state/state.md").read_text(), "Gate: `agents/tools/gate.sh`; the record was agents/old.\n")
        self.assertEqual((ws / "state/workspace.yaml").read_text(), "gate: agents/tools/gate.sh\n")
        self.assertEqual((ws / "knowledge/INDEX.md").read_text(), "Paths are relative to `agents/knowledge/`; see agents/state/state.md.\n")
        self.assertEqual((ws / "tools/gate.sh").read_text(), "out=agents/evidence/$(date +%F)\n")
        self.assertIn("agents/old/tools/gate.sh", (ws / "journal/2026-10-01.md").read_text(), "history keeps its paths")
        self.assertEqual((ws / "evidence/2026-10-02/soak.log").read_text(), "soak\n")
        self.assertEqual((ws / "evidence/2026-10-01/run.log").read_text(), "ok\n")
        self.assertFalse(os.path.lexists(ws / "evidence-2026-10-01"))
        self.assertTrue((ws / "archive/MIGRATION.md").exists() and (ws / "archive/manifest.json").exists())
        moves = (ws / "archive/path-moves.md").read_text()
        self.assertIn("| `agents/old/state.md` | `agents/state/state.md` |", moves)
        self.assertIn("| `agents/old/evidence-2026-10-02` | `agents/evidence/2026-10-02` |", moves)
        self.assertEqual((self.repo / ".claude/settings.json").read_text(), '{"cmd": "cat agents/state/state.md"}\n')
        self.assertEqual((ws / "knowledge/procedures/smoke.md").read_text(),
                         "Run agents/tools/gate.sh; see agents/knowledge/procedures/smoke.md.\n", "a folder moved into another")
        self.assertIn("| `agents/old/procedures` | `agents/knowledge/procedures` |", moves)

    def test_a_folder_moved_inside_another_folds_in_file_by_file(self):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        put(self.old, "knowledge/procedures/merge.md", "m\n")  # the live record's copy already moved; dev's has not
        planned = migrate.plan(self.repo, self.layout)
        self.assertIn("  lift (git) agents/old/procedures -> agents/knowledge/procedures (1 file folds in)", migrate.summary(planned).splitlines())
        migrate.apply(planned)
        ws = self.repo / "agents"
        self.assertEqual(sorted(p.name for p in (ws / "knowledge/procedures").iterdir()), ["merge.md", "smoke.md"])
        self.assertFalse(os.path.lexists(ws / "procedures"))
        self.assertFalse(os.path.lexists(ws / "knowledge/procedures/procedures"))
        self.assertEqual((self.repo / "docs/ci.md").read_text(), "Smoke: agents/knowledge/procedures/smoke.md.\n")
        status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True, text=True).stdout
        self.assertIn("RM agents/old/procedures/smoke.md -> agents/knowledge/procedures/smoke.md", status, "git mv, then the rewrite")

    def test_a_file_both_folders_hold_is_a_collision(self):
        put(self.old, "knowledge/procedures/smoke.md", "other\n")
        planned = migrate.plan(self.repo, self.layout)
        self.assertEqual(len(planned["collisions"]), 1, planned["collisions"])
        self.assertIn("procedures/smoke.md", planned["collisions"][0])
        with self.assertRaises(migrate.Refused):
            migrate.apply(planned)

    def test_a_second_run_on_the_lifted_record_changes_nothing(self):
        """The lifted record holds knowledge/INDEX.md; a rerun used to plan INDEX.md -> index.md and, on a
        case-insensitive volume, delete it. With the layout's marker in place and no legacy record, it stops."""
        for tracked in (False, True):
            with self.subTest(tracked=tracked):
                if tracked:
                    self.setUp()
                    git(self.repo, "add", ".")
                    git(self.repo, "commit", "-qm", "init")
                migrate.apply(migrate.plan(self.repo, self.layout))
                before = snapshot(self.repo)
                status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True, text=True).stdout
                again = migrate.plan(self.repo, self.layout)
                self.assertEqual(again["steps"], [])
                self.assertTrue(again.get("migrated"))
                self.assertIn("already in this layout", migrate.summary(again))
                self.assertEqual(migrate.apply(again), [])
                self.assertEqual(snapshot(self.repo), before, "a second run changes no byte")
                self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True, text=True).stdout, status)
                self.assertTrue((self.repo / "agents/knowledge/INDEX.md").is_file())

    def test_a_second_run_without_a_marker_still_keeps_the_index(self):
        """A layout that names no marker plans the rerun as before; its case-only format step keeps the file."""
        migrate.apply(migrate.plan(self.repo, self.layout))
        bare = Layout("agents", "agents/old", self.template, moves=self.layout.moves, history=self.layout.history)
        migrate.apply(migrate.plan(self.repo, bare))
        names = [p.name for p in (self.repo / "agents/knowledge").iterdir() if p.is_file()]
        self.assertEqual(len([n for n in names if n.lower() == "index.md"]), 1, names)
        self.assertIn("agents/knowledge/", next((self.repo / "agents/knowledge").glob("[Ii][Nn][Dd][Ee][Xx].md")).read_text())

    def test_a_tracked_record_moves_with_git_mv(self):
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        migrate.apply(migrate.plan(self.repo, self.layout))
        status = subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"], capture_output=True, text=True).stdout
        self.assertIn("R  agents/old/journal/2026-10-01.md -> agents/journal/2026-10-01.md", status)
        self.assertIn("R  agents/old/evidence-2026-10-02/soak.log -> agents/evidence/2026-10-02/soak.log", status)
        self.assertIn("D  agents/old/evidence-2026-10-01", status)
        self.assertIn("RM agents/old/state.md -> agents/state/state.md", status)

    def test_a_collision_refuses_the_whole_plan(self):
        put(self.old, "evidence/2026-10-02/soak.log", "other\n")
        put(self.repo, "agents/CLAUDE.md", "mine\n")
        planned = migrate.plan(self.repo, self.layout)
        self.assertEqual(len(planned["collisions"]), 1, planned["collisions"])
        self.assertIn("soak.log", planned["collisions"][0])
        with self.assertRaises(migrate.Refused):
            migrate.apply(planned)
        self.assertTrue((self.old / "evidence-2026-10-02/soak.log").exists())
        self.assertEqual((self.repo / "agents/CLAUDE.md").read_text(), "mine\n")


class SdlcMigrate(Base):
    """A workspace with a legacy PLAN.md and a hybrid brief.md: scaffold only, then retire, then a clean rerun."""

    def test_scaffold_only_then_retire_then_nothing(self):
        ws = self.repo / "agents"
        hybrid = "# Brief\n\nThe mission.\n\n## Questions and answers\n\n| Q | A |\n"
        put(ws, "state.md", "# State\n\nThe plan: targets/demo/PLAN.md.\n")
        put(ws, "brief.md", hybrid)
        put(ws, "targets/demo/PLAN.md", "# Plan\n")
        put(ws, "journal/2026-10-01.md", "Wrote targets/demo/PLAN.md.\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-qm", "init")
        layout = Layout("agents", None, self.template, marker="state.md", sdlc=True,
                        sdlc_sources={"**/PLAN.md": "design/index.md", "**/plan-*.md": "development.md"})
        before = snapshot(ws)
        planned = migrate.plan(self.repo, layout)
        self.assertEqual(sorted(str(s["to"].relative_to(ws)) for s in planned["steps"]),
                         ["design/feedback.md", "design/index.md", "development.md", "interview.md", "requirements.md"])
        self.assertEqual({s["kind"] for s in planned["steps"]}, {"scaffold"})
        self.assertIn("targets/demo/PLAN.md: draft source for design/index.md; `sdlc retire targets/demo/PLAN.md` once drafted",
                      planned["review"])
        self.assertIn("scaffold: 5", migrate.summary(planned))
        migrate.apply(planned)
        after = snapshot(ws)
        self.assertEqual({k: v for k, v in after.items() if k in before}, before)  # nothing existing moved or changed
        sdlc.retire(ws, ["targets/demo/PLAN.md"])
        self.assertIn("archive/pre-sdlc/targets/demo/PLAN.md", (ws / "state.md").read_text())
        self.assertIn("Wrote targets/demo/PLAN.md.", (ws / "journal/2026-10-01.md").read_text())
        self.assertEqual((ws / "brief.md").read_text(), hybrid)
        rerun = migrate.plan(self.repo, layout)
        self.assertEqual(rerun["steps"], [])
        self.assertEqual([r for r in rerun["review"] if "draft source" in r], [])

    def test_a_layout_without_sdlc_plans_no_scaffold(self):
        put(self.repo, "agents/state.md", "# State\n")
        planned = migrate.plan(self.repo, Layout("agents", None, self.template, marker="state.md"))
        self.assertEqual(planned["steps"], [])


if __name__ == "__main__":
    unittest.main()
