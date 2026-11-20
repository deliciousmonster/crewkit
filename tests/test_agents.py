import tempfile
import unittest
from pathlib import Path

from crewkit import agents, guard

TOOLS = {"capabilities": {
    "read": {"roles": ["lead", "scout", "engineer"], "claude_code": {"tools": ["Read"], "allow": ["Bash(git log *)"]}},
    "push": {"roles": ["lead"], "claude_code": {"tools": ["Bash"], "ask": ["Bash(git push *)"]}},
    "mail": {"roles": ["clerk"], "outbound": True, "claude_code": {"tools": ["mcp__mail__draft"], "allow": ["mcp__mail__draft"]}},
    "gates": {"roles": ["engineer"], "claude_code": {"tools": ["Bash"]}}}}
MODELS = {"runtimes": {"claude_code": {"provider": "a"}}, "providers": {"a": {"deep": "opus-x"}}}


class Agents(unittest.TestCase):
    def test_tools_model_and_header(self):
        self.assertEqual(agents.tool_names(TOOLS, "engineer"), ["Read", "Bash"])
        self.assertEqual(agents.model(MODELS, "deep"), "opus-x")
        with self.assertRaises(ValueError):
            agents.model(MODELS, "chore")
        self.assertIn('description: "Scores postings."', agents.header("x-scout", "Scores postings.", ["Read"], "m"))

    def test_policy_denies_only_consequential_rules_a_role_lacks(self):
        p = agents.policy(TOOLS, {"engineer-go": "engineer", "lead": "lead"}, "agents/x", ["Bash(git stash *)"], "gates")
        self.assertEqual(p["agents"]["engineer-go"]["deny"], {"Bash(git push *)": "push", "mcp__mail__draft": "mail"})
        self.assertTrue(p["agents"]["engineer-go"]["gated"])
        self.assertEqual(p["agents"]["lead"]["deny"], {"mcp__mail__draft": "mail"})


class Guard(unittest.TestCase):
    def test_only_a_parallel_role_spawns_and_only_its_own_role(self):
        p = agents.policy(TOOLS, {"x-scout": "scout", "x-clerk": "clerk"}, "agents/x", [], parallel={"scout": 4})
        self.assertEqual((p["agents"]["x-scout"]["parallel"], p["agents"]["x-clerk"]["parallel"]), (4, 0))

        def spawn(agent, wanted):
            return guard.denial({"agent_type": agent, "tool_name": "Agent", "tool_input": {"subagent_type": wanted}},
                                p, "none")

        self.assertIsNone(spawn("x-scout", "x-scout"))
        self.assertIn("only its own role", spawn("x-scout", "general-purpose"))
        self.assertIn("does not run in parallel", spawn("x-clerk", "x-clerk"))
        self.assertIsNone(spawn("general-purpose", "general-purpose"))

    def test_a_role_listing_its_tools_is_held_to_them_and_to_mcp_it_holds(self):
        p = agents.policy(TOOLS, {"scout": "scout", "clerk": "clerk"}, "agents/x", [])
        self.assertEqual(p["bound"], ["Bash", "Read", "mcp__mail__draft"])

        def why(agent, tool):
            return guard.denial({"agent_type": agent, "tool_name": tool, "tool_input": {}}, p, "none")

        self.assertIsNone(why("scout", "Read"))
        self.assertIn("binds `Bash`", why("scout", "Bash"))
        self.assertIn("binds `mcp__chrome__navigate`", why("scout", "mcp__chrome__navigate"))
        self.assertIsNone(why("scout", "ToolSearch"))
        self.assertIsNone(why("clerk", "mcp__mail__draft"))
        self.assertIsNone(why("general-purpose", "mcp__chrome__navigate"))
        self.assertTrue(guard.held("mcp__chrome__navigate", ["mcp__chrome__*"]))
        self.assertNotIn("tools:", agents.header("x", "d", None, "m"))

    def test_rules_match_commands_and_tools(self):
        self.assertTrue(guard.rule_matches("Bash(git push *)", "Bash", "cd x && FOO=1 git push origin a"))
        self.assertTrue(guard.rule_matches("Bash(git push *)", "Bash", "git push"))
        self.assertFalse(guard.rule_matches("Bash(git push *)", "Bash", "git pushd"))
        self.assertTrue(guard.rule_matches("mcp__mail__draft", "mcp__mail__draft"))

    def test_denial_by_role_and_by_workspace(self):
        p = agents.policy(TOOLS, {"engineer-go": "engineer"}, "agents/x", ["Bash(git stash *)"], "gates")
        call = lambda agent, command, cwd="/nowhere": guard.denial(  # noqa: E731
            {"agent_type": agent, "tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}, p, "agents/x/state.md")
        self.assertIn("does not hold `push`", call("engineer-go", "git push"))
        self.assertIsNone(call("engineer-go", "git log -1"))
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "agents/x").mkdir(parents=True)
            (Path(tmp) / "agents/x/state.md").write_text("")
            self.assertIn("never-list", call("general-purpose", "git stash", cwd=tmp))
        self.assertIsNone(call("general-purpose", "git stash"))
        self.assertIn("deny", guard.deny("no"))

    def test_a_record_at_the_agents_root_is_found_from_any_folder_below_its_workdir(self):
        p = agents.policy(TOOLS, {}, "agents", ["Bash(git stash *)"])
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "agents/state").mkdir(parents=True)
            (Path(tmp) / "src/deep").mkdir(parents=True)
            (Path(tmp) / "agents/state/state.md").write_text("")
            call = lambda cwd: guard.denial(  # noqa: E731
                {"tool_name": "Bash", "tool_input": {"command": "git stash"}, "cwd": cwd}, p, "agents/state/state.md")
            self.assertIn("never-list", call(str(Path(tmp) / "src/deep")))
            self.assertIn("never-list", call(str(Path(tmp) / "agents/state")))
            self.assertIsNone(call(str(Path(tmp).parent)))
