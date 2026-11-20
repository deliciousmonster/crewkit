import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from crewkit import llm as llm_mod
from crewkit.config import CrewConfig

try:  # the `run` extra; the core suite runs without it
    from crewkit.plan_llm import ClaudePlanLLM
except ModuleNotFoundError as e:
    if (e.name or "").split(".")[0] not in ("crewai", "crewai_tools"):
        raise
    ClaudePlanLLM = None
needs_run = unittest.skipIf(ClaudePlanLLM is None, "needs the run extra: uv run --extra run --python 3.13")

MODELS = {
    "providers": {"anthropic": {"deep": "claude-opus-5-5", "standard": "claude-sonnet-5-5"}},
    "runtimes": {"crewai": {"provider": "anthropic", "prefix": "anthropic/"}},
}


@needs_run
class Backend(unittest.TestCase):
    def test_auto_takes_the_plan_without_a_key(self):
        with mock.patch.dict(os.environ, {"CREW_LLM_BACKEND": "auto"}, clear=True):
            self.assertEqual(llm_mod.backend(Path("/nonexistent")), "plan")

    def test_auto_takes_the_api_with_a_key_file(self):
        with tempfile.NamedTemporaryFile() as key, mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(llm_mod.backend(Path(key.name)), "api")

    def test_plan_backend_builds_the_plan_llm_for_the_tier(self):
        with mock.patch.dict(os.environ, {"CREW_LLM_BACKEND": "plan"}, clear=True):
            built = llm_mod.llm(MODELS, "deep")
        self.assertIsInstance(built, ClaudePlanLLM)
        self.assertEqual(built.model, "claude-opus-5-5")

    def test_api_backend_prefixes_the_provider(self):
        with mock.patch.dict(os.environ, {"CREW_LLM_BACKEND": "api", "ANTHROPIC_API_KEY": "test"}, clear=True):
            built = llm_mod.llm(MODELS, "standard")
        self.assertNotIsInstance(built, ClaudePlanLLM)
        self.assertIn("claude-sonnet-5-5", built.model)

    def test_a_bad_backend_exits(self):
        with mock.patch.dict(os.environ, {"CREW_LLM_BACKEND": "cloud"}, clear=True):
            with self.assertRaises(SystemExit):
                llm_mod.backend()


@needs_run
class PlanLLM(unittest.TestCase):
    """The one-shot path, CREW_PLAN_PERSIST=0; PlanSession covers the default."""

    def setUp(self):
        patch = mock.patch.dict(os.environ, {"CREW_PLAN_PERSIST": "0"})
        patch.start()
        self.addCleanup(patch.stop)

    def test_reply_is_cut_at_the_stop_sequence(self):
        done = mock.Mock(returncode=0, stdout=json.dumps({"result": "Action: read\nObservation: invented"}), stderr="")
        plan = ClaudePlanLLM(model="claude-haiku-4-5-20251001", stop=["\nObservation:"])
        with mock.patch("crewkit.plan_llm.subprocess.run", return_value=done) as run:
            self.assertEqual(plan.call("hi"), "Action: read")
        self.assertNotIn("ANTHROPIC_API_KEY", run.call_args.kwargs["env"])

    def test_the_model_is_told_its_tools_run_in_the_process_directory(self):
        done = mock.Mock(returncode=0, stdout=json.dumps({"result": "ok"}), stderr="")
        seen = {}

        def capture(argv, **kwargs):
            seen["system"] = Path(argv[argv.index("--system-prompt-file") + 1]).read_text()
            return done

        with mock.patch("crewkit.plan_llm.subprocess.run", side_effect=capture):
            ClaudePlanLLM(model="m").call([{"role": "system", "content": "You are X."}, {"role": "user", "content": "hi"}])
        self.assertTrue(seen["system"].startswith("You are X."))
        self.assertIn(f"working directory is {os.getcwd()}", seen["system"])

    def test_a_reply_in_tool_call_markup_becomes_an_action(self):
        markup = ('I write the section.\n<invoke name="write_file">\n<parameter name="path">/x/AGENTS.md</parameter>\n'
                  '<parameter name="content">## Baseline\nok</parameter>\n<parameter name="line_count">40</parameter>\n</invoke>')
        done = mock.Mock(returncode=0, stdout=json.dumps({"result": markup}), stderr="")
        with mock.patch("crewkit.plan_llm.subprocess.run", return_value=done):
            text = ClaudePlanLLM(model="m").call("hi")
        thought, action, given = text.split("\n", 2)
        self.assertEqual((thought, action), ("Thought: I write the section.", "Action: write_file"))
        self.assertEqual(json.loads(given.removeprefix("Action Input: ")),
                         {"path": "/x/AGENTS.md", "content": "## Baseline\nok", "line_count": 40})

    def test_a_worker_that_answers_before_any_tool_result_is_asked_to_read_first(self):
        answers = iter(["Final Answer: framing.md says needs-work.", 'Action: read_file\nAction Input: {"path": "/f"}'])
        reply = lambda *a, **k: mock.Mock(returncode=0, stdout=json.dumps({"result": next(answers)}), stderr="")
        worker = SimpleNamespace(role="go Engineer", tools=["read_file"], allow_delegation=False)
        with mock.patch("crewkit.plan_llm.subprocess.run", side_effect=reply) as run:
            self.assertTrue(ClaudePlanLLM(model="m").call("build it", from_agent=worker).startswith("Action: read_file"))
        self.assertEqual(run.call_count, 2)
        self.assertIn("called no tool", run.call_args.kwargs["input"])
        lead = SimpleNamespace(role="Lead", tools=["delegate"], allow_delegation=True)
        answers = iter(["Final Answer: held for the owner."])
        with mock.patch("crewkit.plan_llm.subprocess.run", side_effect=reply) as run:
            ClaudePlanLLM(model="m").call("build it", from_agent=lead)
        self.assertEqual(run.call_count, 1)

    def test_the_debug_log_times_each_call_and_names_its_agent(self):
        reply = {"result": "ok", "duration_ms": 9000, "duration_api_ms": 6000}
        done = mock.Mock(returncode=0, stdout=json.dumps(reply), stderr="")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"CREW_PLAN_DEBUG": f"{tmp}/log"}), \
                mock.patch("crewkit.plan_llm.subprocess.run", return_value=done):
            ClaudePlanLLM(model="m").call("hi", from_agent=SimpleNamespace(role=" Scout\n"),
                                     from_task=SimpleNamespace(name="baseline"))
            line = json.loads(Path(f"{tmp}/log").read_text())
        self.assertEqual((line["agent"], line["task"], line["duration_api_ms"]), ("Scout", "baseline", 6000))
        self.assertGreaterEqual(line["wall_s"], 0)


# A stand-in for `claude -p` in stream-json mode, logging its start and each message to $FAKE_LOG: a tool result
# gets a final answer, "invent" a reply that writes its own observation, and anything else an action.
FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
log = open(os.environ["FAKE_LOG"], "a")
log.write(json.dumps({"start": sys.argv[1:]}) + "\\n"); log.flush()
for line in sys.stdin:
    text = json.loads(line)["message"]["content"]
    log.write(json.dumps({"got": text}) + "\\n"); log.flush()
    if "Observation:" in text:
        reply = "Final Answer: done"
    elif "bare" in text:
        reply = "Action: read\\nAction Input: {}\\n\\nObservation:"
    elif "invent" in text:
        reply = "Action: read\\nAction Input: {}\\nObservation: invented"
    else:
        reply = "Action: read\\nAction Input: {}"
    print(json.dumps({"type": "result", "result": reply, "duration_ms": 5}), flush=True)
"""


@needs_run
class PlanSession(unittest.TestCase):
    def setUp(self):
        from crewkit import plan_llm
        self.tmp = tempfile.TemporaryDirectory()
        fake = Path(self.tmp.name) / "claude"
        fake.write_text(FAKE_CLAUDE)
        fake.chmod(0o755)
        self.log = Path(self.tmp.name) / "log"
        for patch in (mock.patch.object(plan_llm, "CLAUDE", str(fake)),
                      mock.patch.dict(os.environ, {"FAKE_LOG": str(self.log), "CREW_PLAN_PERSIST": "1"})):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(plan_llm._close_pool)

    def events(self):
        return [json.loads(l) for l in self.log.read_text().splitlines()]

    def test_a_tool_result_goes_to_the_live_process_alone(self):
        plan = ClaudePlanLLM(model="m", stop=["\nObservation:"])
        first = [{"role": "system", "content": "You are X."}, {"role": "user", "content": "read the file"}]
        action = plan.call(first)
        self.assertEqual(action, "Action: read\nAction Input: {}")
        done = plan.call(first + [{"role": "assistant", "content": action + "\nObservation: 42"}])
        self.assertEqual(done, "Final Answer: done")
        events = self.events()
        self.assertEqual(sum("start" in e for e in events), 1)
        self.assertIn("--input-format", events[0]["start"])
        self.assertEqual(events[-1]["got"], "Observation: 42")

    def test_a_reply_cut_at_a_stop_word_starts_a_new_process(self):
        # The process remembers the observation it invented; CrewAI kept the real one, so they no longer agree.
        plan = ClaudePlanLLM(model="m", stop=["\nObservation:"])
        first = [{"role": "user", "content": "invent it"}]
        action = plan.call(first)
        plan.call(first + [{"role": "assistant", "content": action + "\nObservation: 42"}])
        events = self.events()
        self.assertEqual(sum("start" in e for e in events), 2)
        self.assertTrue(events[-1]["got"].startswith("[user]\ninvent it"))
        self.assertIn("Observation: 42", events[-1]["got"])

    def test_a_reply_ending_on_a_bare_stop_word_keeps_its_process(self):
        plan = ClaudePlanLLM(model="m", stop=["\nObservation:"])
        first = [{"role": "user", "content": "bare stop"}]
        action = plan.call(first)
        plan.call(first + [{"role": "assistant", "content": action + "\nObservation: 42"}])
        self.assertEqual(sum("start" in e for e in self.events()), 1)

    def test_another_conversation_or_model_gets_its_own_process(self):
        ClaudePlanLLM(model="m").call("read a")
        ClaudePlanLLM(model="m").call("read b")
        ClaudePlanLLM(model="n").call([{"role": "user", "content": "read a"},
                                       {"role": "assistant", "content": "Action: read\nAction Input: {}\nObservation: 1"}])
        self.assertEqual(sum("start" in e for e in self.events()), 3)

    def test_the_debug_log_says_whether_the_process_was_reused(self):
        plan = ClaudePlanLLM(model="m")
        first = [{"role": "user", "content": "read the file"}]
        with mock.patch.dict(os.environ, {"CREW_PLAN_DEBUG": f"{self.tmp.name}/debug"}):
            action = plan.call(first)
            plan.call(first + [{"role": "assistant", "content": action + "\nObservation: 42"}])
        lines = [json.loads(l) for l in Path(f"{self.tmp.name}/debug").read_text().splitlines()]
        self.assertEqual([l["session"] for l in lines], ["new", "reused"])
        self.assertEqual(lines[1]["sent"], len("Observation: 42"))


class Config(unittest.TestCase):
    def test_knowledge_inlines_role_docs_and_the_language_profile(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "config").mkdir()
            (root / "knowledge" / "languages").mkdir(parents=True)
            (root / "config" / "crew.yaml").write_text("roles:\n  eng:\n    knowledge: [rules]\n    languages: true\n")
            (root / "knowledge" / "rules.md").write_text("---\nsummary: \"x\"\nload_when: \"always\"\n---\nRULES")
            (root / "knowledge" / "languages" / "go.md").write_text("GO")
            cfg = CrewConfig(root / "config", root / "knowledge")
            self.assertEqual(cfg.knowledge("eng", {"language": "go"}), "RULES\n\nGO")


    def test_inline_sections_keeps_only_the_named_sections(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            (root / "config").mkdir()
            (root / "knowledge").mkdir()
            (root / "config" / "crew.yaml").write_text("roles:\n  ops:\n    knowledge: [voice]\n    inline_sections:\n      voice: [Prose]\n")
            (root / "knowledge" / "voice.md").write_text("# Voice\n\n## Prose\n\nP\n\n## Filed\n\nF\n")
            cfg = CrewConfig(root / "config", root / "knowledge")
            self.assertEqual(cfg.knowledge("ops", {}), "# Voice\n\n## Prose\n\nP")


AGENTS = "lead:\n  role: Lead\n  goal: Lead {who}\n  backstory: B\n  tier: deep\nwriter:\n  role: Writer\n  goal: Write for {who}\n  backstory: B\n  tier: standard\n"
TASKS = (
    "browse:\n  description: Browse for {who}\n  expected_output: pages\n  runtime: claude_code\n"
    "draft:\n  description: Draft for {who}\n  expected_output: a draft\n  agent: writer\n  gate: checked\n"
)


@needs_run
class Builder(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        root = Path(self.root.name)
        (root / "config").mkdir()
        (root / "config" / "agents.yaml").write_text(AGENTS)
        (root / "config" / "tasks.yaml").write_text(TASKS)
        (root / "config" / "crew.yaml").write_text("process: sequential\ninputs: [who]\n")
        (root / "config" / "models.yaml").write_text(json.dumps(MODELS))
        self.cfg = CrewConfig(root / "config", root / "knowledge")

    def tearDown(self):
        self.root.cleanup()

    def test_a_claude_code_task_is_skipped_and_a_gate_becomes_the_guardrail(self):
        from crewkit.builder import build_crew

        seen = {}

        def checked(inputs):
            seen.update(inputs)
            return lambda output: (True, output)

        with mock.patch.dict(os.environ, {"CREW_LLM_BACKEND": "plan"}, clear=True):
            crew = build_crew(self.cfg, ["browse", "draft"], {"who": "x"}, gates={"checked": checked})
        self.assertEqual([t.name for t in crew.tasks], ["draft"])
        self.assertIsNotNone(crew.tasks[0].guardrail)
        self.assertEqual(seen["who"], "x")

    def test_an_unregistered_gate_or_an_all_claude_code_run_exits(self):
        from crewkit.builder import build_crew

        with mock.patch.dict(os.environ, {"CREW_LLM_BACKEND": "plan"}, clear=True):
            with self.assertRaises(SystemExit):
                build_crew(self.cfg, ["draft"], {"who": "x"})
            with self.assertRaises(SystemExit):
                build_crew(self.cfg, ["browse"], {"who": "x"})


    def test_a_class_two_capabilities_bind_is_given_once(self):
        from crewai.tools import BaseTool

        from crewkit.builder import build_agent

        class Runner(BaseTool):
            name: str = "runner"
            description: str = "runs"

            def _run(self) -> str:
                return ""

        caps = {"shell": {"roles": ["writer"], "crewai": [{"class": "Runner"}]},
                "box": {"roles": ["writer"], "crewai": [{"class": "Runner"}, {"class": "Unbuilt"}]}}
        (Path(self.root.name) / "config" / "tools.yaml").write_text(json.dumps({"capabilities": caps}))
        cfg = CrewConfig(Path(self.root.name) / "config", Path(self.root.name) / "knowledge")
        with mock.patch.dict(os.environ, {"CREW_LLM_BACKEND": "plan"}, clear=True):
            agent = build_agent(cfg, "writer", {"who": "x"}, registry={"Runner": Runner})
        self.assertEqual([t.name for t in agent.tools], ["runner"])

    def test_a_hierarchical_manager_gets_no_worker_tools(self):
        from crewai.tools import BaseTool

        from crewkit.builder import build_crew

        class Runner(BaseTool):
            name: str = "runner"
            description: str = "runs"

            def _run(self) -> str:
                return ""

        root = Path(self.root.name) / "config"
        (root / "crew.yaml").write_text("process: hierarchical\nmanager: lead\ninputs: [who]\n")
        (root / "tools.yaml").write_text(json.dumps({"capabilities": {"run": {"roles": ["writer"], "crewai": [{"class": "Runner"}]}}}))
        cfg = CrewConfig(root, Path(self.root.name) / "knowledge")
        with mock.patch.dict(os.environ, {"CREW_LLM_BACKEND": "plan"}, clear=True):
            crew = build_crew(cfg, ["draft"], {"who": "x"}, gates={"checked": lambda inputs: lambda o: (True, o)}, registry={"Runner": Runner})
        self.assertEqual(crew.tasks[0].tools, [])
        self.assertEqual([t.name for t in crew.tasks[0].agent.tools], ["runner"])
        self.assertEqual(crew.manager_agent.tools, [])

    def chain(self):
        from crewkit.builder import build_crew

        root = Path(self.root.name) / "config"
        (root / "crew.yaml").write_text("process: hierarchical\nmanager: lead\ninputs: [who]\n")
        (root / "tasks.yaml").write_text(
            "draft:\n  description: Draft for {who}\n  expected_output: a draft\n  agent: writer\n"
            "edit:\n  description: Edit\n  expected_output: an edit\n  agent: writer\n  context: [design]\n")
        cfg = CrewConfig(root, Path(self.root.name) / "knowledge")
        with mock.patch.dict(os.environ, {"CREW_LLM_BACKEND": "plan"}, clear=True):
            return build_crew(cfg, ["draft", "edit"], {"who": "x"})

    def test_the_manager_has_its_inputs_filled(self):
        crew = self.chain()
        self.assertEqual(crew.manager_agent.goal, "Lead x")
        self.assertNotIn("{knowledge_lead}", crew.manager_agent.backstory)

    def test_a_task_whose_context_is_not_in_the_run_reads_earlier_outputs(self):
        from crewai.utilities.constants import NOT_SPECIFIED

        crew = self.chain()
        self.assertEqual(crew.tasks[0].context, [])
        self.assertIs(crew.tasks[1].context, NOT_SPECIFIED)

if __name__ == "__main__":
    unittest.main()
