import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from crewkit import agents, install, lint

TOOLS = {"capabilities": {
    "read-local": {"roles": ["scout", "tailor"], "claude_code": {"tools": ["Read", "Grep", "Glob"]}},
    "write-local": {"roles": ["scout", "tailor"], "claude_code": {"tools": ["Edit", "Write", "Bash"]}},
    "web-read": {"roles": ["scout"], "claude_code": {"tools": ["WebFetch", "WebSearch"]}},
    "chrome": {"roles": ["lead"], "claude_code": {"tools": ["mcp__claude-in-chrome__*"]}}}}


class FlowTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def flow(self, name, text):
        (self.dir / f"{name}.md").write_text(text)
        return self.dir / f"{name}.md"

    def test_a_declaration_reads_backticked_names_across_lines_and_by_scope(self):
        found, problems = lint.flow_tools(
            "# Tailor\n\n**Reads:** x\n**Tools:** `Read`, `Grep`,\n`Bash`\n**Chrome tools:** `navigate`\n\n"
            "**Tools (lead, scout):** `WebSearch`\n1. Step that mentions `WebFetch` in prose.\n")
        self.assertEqual(problems, [])
        self.assertEqual(found, {None: ["Read", "Grep", "Bash"], "lead": ["WebSearch"], "scout": ["WebSearch"]})

    def test_none_declares_nothing_and_a_bare_list_is_a_problem(self):
        self.assertEqual(lint.flow_tools("**Tools:** none.\n"), ({None: []}, []))
        self.assertEqual(lint.flow_tools("**Tools:** Read, Grep\n")[1],
                         [(1, "a **Tools:** line names no tool in backticks and does not say none")])
        self.assertEqual(lint.flow_tools("# Flow\n\nUse WebSearch to find the board.\n"), ({}, []))

    def test_a_tool_the_role_lacks_fails_naming_role_flow_and_tool(self):
        # 2026-10-29: the tailor's flow asked for a web search its role cannot make.
        tailor = self.flow("tailor", "**Tools:** `Read`, `Bash`, `WebSearch`\n")
        self.assertEqual(lint.check_flow_tools(TOOLS, {"tailor": [tailor]}),
                         [f"tailor: {tailor}: WebSearch is not among the role's tools "
                          "(Read, Grep, Glob, Edit, Write, Bash)"])
        scout = self.flow("scout", "**Tools:** `Read`, `WebSearch`, `WebFetch`\n")
        self.assertEqual(lint.check_flow_tools(TOOLS, {"scout": [scout]}), [])

    def test_a_line_scoped_to_another_role_binds_only_that_role(self):
        tailor = self.flow("tailor", "**Tools:** `Read`, `Edit`\n**Tools (lead):** `WebSearch`, "
                           "`mcp__claude-in-chrome__navigate`\n")
        self.assertEqual(lint.check_flow_tools(TOOLS, {"tailor": [tailor]}, ["lead", "tailor"]), [])
        found = lint.check_flow_tools(TOOLS, {"tailor": [tailor], "lead": [tailor]}, ["lead", "tailor"])
        self.assertEqual(found, [f"lead: {tailor}: Read is not among the role's tools (mcp__claude-in-chrome__*)",
                                 f"lead: {tailor}: Edit is not among the role's tools (mcp__claude-in-chrome__*)",
                                 f"lead: {tailor}: WebSearch is not among the role's tools (mcp__claude-in-chrome__*)"])

    def test_a_wildcard_the_role_holds_covers_a_named_tool(self):
        f = self.flow("apply", "**Tools:** `mcp__claude-in-chrome__navigate`\n")
        self.assertEqual(lint.check_flow_tools(TOOLS, {"lead": [f]}), [])

    def test_missing_undeclared_and_unknown_scopes_are_named(self):
        bare = self.flow("bare", "# Bare\n\nNo declaration.\n")
        odd = self.flow("odd", "**Tools:** `Read`\n**Tools (tailr):** `Read`\n")
        found = lint.check_flow_tools(TOOLS, {"tailor": [bare, odd, self.dir / "gone.md"]}, ["tailor"])
        self.assertEqual(found, [f"tailor: {bare}: declares no tools; add a **Tools:** line (or **Tools:** none.)",
                                 f"tailor: {odd}: **Tools (tailr):** names a role the crew does not have",
                                 f"tailor: {self.dir / 'gone.md'}: flow file is missing"])

    def test_flows_of_reads_crew_yaml_roles(self):
        crew = {"roles": {"lead": {"knowledge": []}, "tailor": {"claude_code": {"flows": ["tailor", "resume"]}},
                          "scout": {"claude_code": {"description": "x"}}}}
        self.assertEqual(lint.flows_of(crew, "flows/{flow}.md", "/c"),
                         {"tailor": [Path("/c/flows/tailor.md"), Path("/c/flows/resume.md")]})


class LintCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = root = Path(self.tmp.name)
        (root / "skills" / "demo" / "flows").mkdir(parents=True)
        (root / "skills" / "demo" / "SKILL.md").write_text("# demo\n")
        (root / "config").mkdir()
        (root / "config" / "crew.yaml").write_text("roles:\n  lead: {}\n  tailor:\n    claude_code:\n"
                                                   "      flows: [tailor]\n")
        (root / "config" / "tools.yaml").write_text("capabilities:\n  read:\n    roles: [tailor]\n"
                                                    "    claude_code:\n      tools: [Read]\n")
        self.pyproject = ('[project]\nname = "democrew"\n[tool.crewkit]\nskills = ["demo"]\n'
                          '[tool.crewkit.flows]\ntools = "config/tools.yaml"\ncrew = "config/crew.yaml"\n'
                          'path = "skills/demo/flows/{flow}.md"\n')
        (root / "pyproject.toml").write_text(self.pyproject)

    def run_lint(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = install.main(["lint", "--root", str(self.root)])
        return code, out.getvalue()

    def test_lint_fails_on_a_tool_the_role_lacks_and_passes_once_held(self):
        flow = self.root / "skills" / "demo" / "flows" / "tailor.md"
        flow.write_text("# Tailor\n\n**Tools:** `Read`, `WebSearch`\n")
        self.assertEqual(self.run_lint(), (1, "[FAIL] tailor: skills/demo/flows/tailor.md: WebSearch is not among "
                                              "the role's tools (Read)\nlint: 1 problem in 1 role flows\n"))
        flow.write_text("# Tailor\n\n**Tools:** `Read`\n**Tools (lead):** `WebSearch`\n")
        self.assertEqual(self.run_lint(), (0, "lint: 1 role flows hold every tool they declare\n"))

    def test_a_flows_table_without_its_keys_is_refused(self):
        (self.root / "pyproject.toml").write_text(self.pyproject.replace('crew = "config/crew.yaml"\n', ""))
        with self.assertRaises(install.InstallError):
            install.load_spec(self.root)


class Labels(unittest.TestCase):
    def test_a_label_names_the_flow_and_target_on_one_short_line(self):
        self.assertEqual(agents.label("Apply", "Asurion -\n Staff SEM"), "Apply: Asurion - Staff SEM")
        long = agents.label("Apply", "Asurion - Staff Software Engineering Manager, Agent Platform and Tools")
        self.assertEqual(long, "Apply: Asurion - Staff Software Engineering Manager...")
        self.assertLessEqual(len(long), 60)

    def test_a_report_names_its_target_on_its_first_line(self):
        self.assertEqual(agents.target_of("\nTarget: Parallax - Head of Platform\nStaged the form."),
                         "Parallax - Head of Platform")
        self.assertIsNone(agents.target_of("Staged the form.\nTarget: Parallax"))
        self.assertIsNone(agents.target_of("Target:\n"))

    def test_the_guidance_texts_carry_no_em_dash_and_name_the_target_line(self):
        for text in (agents.LEAD_DELEGATION, agents.SUBAGENT_TARGET):
            self.assertNotIn("\u2014", text)
            self.assertIn("`Target: <target>`", text)
