import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import usagecase as uc

from crewkit import usage


def call(mid, model="claude-opus-5-5", inp=10, w1h=0, w5m=0, read=0, out=0, speed="standard", blocks=1, tool=None):
    u = {"input_tokens": inp, "cache_creation_input_tokens": w1h + w5m, "cache_read_input_tokens": read,
         "output_tokens": out, "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": w5m},
         "speed": speed}
    content = [{"type": "tool_use", "id": tool, "name": "Agent", "input": {"description": "x"}}] if tool else [{"type": "text", "text": "t"}]
    # Claude Code writes one row per content block, each carrying the call's whole usage.
    return [{"type": "assistant", "message": {"id": mid, "model": model, "usage": u, "content": content}} for _ in range(blocks)]


def typed(text):
    return {"type": "user", "message": {"content": text}, "timestamp": "t"}


class Price(unittest.TestCase):
    def test_each_component_at_its_rate(self):
        u = {"input_tokens": 1_000_000, "cache_creation": {"ephemeral_1h_input_tokens": 1_000_000, "ephemeral_5m_input_tokens": 1_000_000},
             "cache_creation_input_tokens": 2_000_000, "cache_read_input_tokens": 1_000_000, "output_tokens": 1_000_000}
        # Opus 5.5: 4 + 4x1.25 + 4x2 + 0.20 + 20
        self.assertAlmostEqual(usage.price("claude-opus-5-5", u), 4 + 5 + 8 + 0.20 + 20)
        self.assertAlmostEqual(usage.price("claude-opus-5-5", {**u, "speed": "fast"}), 2 * (4 + 5 + 8 + 0.20 + 20))

    def test_family_by_longest_prefix_and_unknown(self):
        self.assertEqual(usage.family("claude-opus-5-5-20261001"), "claude-opus-5-5")
        self.assertEqual(usage.family("claude-opus-5"), "claude-opus-5")
        self.assertEqual(usage.family("anthropic.claude-sonnet-5-5"), "claude-sonnet-5-5")
        self.assertIsNone(usage.price("gpt-9", {"input_tokens": 5}))

    def test_haiku_long_prompt_rate(self):
        short = usage.price("claude-haiku-5-5", {"input_tokens": 100_000})
        long_ = usage.price("claude-haiku-5-5", {"input_tokens": 100_001})
        self.assertAlmostEqual(short, 0.01)
        self.assertAlmostEqual(long_, 100_001 * 0.5 / 1e6)

    def test_unsplit_writes_count_at_five_minutes(self):
        self.assertAlmostEqual(usage.price("claude-sonnet-5-5", {"cache_creation_input_tokens": 1_000_000}), 2.5)


class Walk(unittest.TestCase):
    def test_a_call_written_as_several_rows_counts_once(self):
        w = usage.walk(call("m1", inp=100, out=10, blocks=3) + call("m2", inp=200, out=20, blocks=2))
        self.assertEqual(w["calls"], 2)
        self.assertEqual(w["tokens"], 330)
        self.assertAlmostEqual(w["usd"], (300 * 4 + 30 * 20) / 1e6, places=6)

    def test_rows_without_ids_are_separate_calls(self):
        rs = [{"type": "assistant", "message": {"usage": {"input_tokens": 5}, "content": []}} for _ in range(3)]
        self.assertEqual(usage.walk(rs)["calls"], 3)

    def test_unpriced_skips_synthetic(self):
        w = usage.walk(call("a", model="<synthetic>") + call("b", model="mystery-1"))
        self.assertEqual(w["unpriced"], 1)


class Report(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.s = self.d / "sess.jsonl"
        rows = [typed("hello")] + call("l0", inp=5) + [typed("<command-name>/other</command-name>")] \
            + call("l1", inp=1000, out=100, tool="tu1", blocks=2) + call("l2", inp=2000, out=50)
        self.s.write_text("\n".join(json.dumps(r) for r in rows))
        sub = self.d / "sess" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a1.meta.json").write_text(json.dumps({"agentType": "dev-engineer", "description": "Build: YTC-2976", "toolUseId": "tu1"}))
        (sub / "agent-a1.jsonl").write_text("\n".join(json.dumps(r) for r in call("s1", model="claude-haiku-5-5", inp=50_000, out=1000, blocks=2)))

    def test_cycle_from_the_command_with_its_agents(self):
        rep = usage.report(self.s, "cycle", command="/other", budgets={"lead": 10, "dev-engineer": 1})
        t = rep["total"]
        self.assertEqual(t["lead"], 3150)
        self.assertEqual(t["agents"], 51_000)
        self.assertEqual(t["tokens"], 54_150)
        self.assertAlmostEqual(t["usd"], (3000 * 4 + 150 * 20) / 1e6 + (50_000 * 0.10 + 1000 * 0.50) / 1e6, places=6)
        self.assertTrue(rep["turns"][0]["agents"][0]["over"])
        self.assertEqual(usage.line(rep), "Tokens this cycle: 54.1k · API price: $0.02")

    def test_by_key_totals_agents_per_ticket(self):
        rep = usage.report(self.s, "all")
        self.assertEqual(usage.by_key([rep]), {"YTC-2976": {"tokens": 51_000, "usd": 0.0055, "nanos": 5_500_000,
                                                            "agents": 1, "unpriced": 0}})



class Scratch(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)


class Nested(Scratch):
    """R1, R2, R4 and U1c's report half, on U1's fixture with its hand-worked totals."""

    def setUp(self):
        super().setUp()
        self.s = uc.build(self.d)
        self.rep = usage.report(self.s, "cycle", command="/example")

    def agent(self, desc):
        return next(a for a in self.rep["turns"][0]["agents"] if a["agent"] == desc)

    def test_r1_depth_two_agent_counts(self):
        self.assertEqual(self.agent("Build: ABC-1 child")["tokens"], uc.AGENT_TOKENS["B"])
        self.assertEqual(self.rep["total"]["tokens"], uc.TOKENS)

    def test_r2_fork_replays_from_inside_the_cycle_count_once(self):
        self.assertEqual(self.agent("fork")["tokens"], uc.AGENT_TOKENS["F"])
        self.assertEqual(self.rep["total"]["agents"], sum(uc.AGENT_TOKENS.values()))

    def test_r4_a_row_holding_u2028_stays_whole(self):
        texts = [b.get("text") for r in usage.rows(self.s) for b in (r.get("message") or {}).get("content") or []
                 if isinstance(b, dict)]
        self.assertIn(f"a{uc.SEP}b", texts)
        self.assertEqual(self.rep["total"]["lead"], uc.LEAD_TOKENS)

    def test_u1c_line_and_nanos_are_the_hand_worked_figures(self):
        self.assertEqual(self.rep["total"]["nanos"], uc.NANOS)
        self.assertEqual(usage.line(self.rep), uc.LINE)
        self.assertNotIn("old", [a["agent"] for a in self.rep["turns"][0]["agents"]])


class Missing(Scratch):
    """A selected agent with an empty .meta.json drops out and one without its .jsonl counts as empty; 0.14.1 raised."""

    def test_an_empty_meta_and_a_missing_agent_file_leave_the_rest_counted(self):
        s = uc.build(self.d)
        sub = s.with_suffix("") / "subagents"
        (sub / "agent-N1.meta.json").write_text("")
        (sub / "agent-B.jsonl").unlink()
        rep = usage.report(s, "cycle", command="/example")
        names = [a["agent"] for a in rep["turns"][0]["agents"]]
        self.assertNotIn("n1", names)
        self.assertIn("Build: ABC-1 child", names)
        self.assertEqual(rep["total"]["tokens"], uc.TOKENS - uc.AGENT_TOKENS["N1"] - uc.AGENT_TOKENS["B"])
        self.assertEqual(rep["total"]["nanos"], uc.NANOS - 120_000 - 30_000_000)  # N1: 2 x (10 in, 1 out); B: 5,000 in, 500 out


class IdLess(Scratch):
    def test_r2b_thirty_agents_of_idless_calls_all_count(self):
        s = self.d / "s.jsonl"
        tools = [f"tu{i}" for i in range(30)]
        s.write_text(uc.dump([uc.typed("/example")] + uc.call("m0", 1, tools=tools)))
        sub = s.with_suffix("") / "subagents"
        sub.mkdir(parents=True)
        row = {"type": "assistant", "message": {"model": "claude-opus-5-5", "usage": {"input_tokens": 7}, "content": []}}
        for i, t in enumerate(tools):
            (sub / f"agent-x{i}.meta.json").write_text(json.dumps({"toolUseId": t, "description": f"a{i}"}))
            (sub / f"agent-x{i}.jsonl").write_text(uc.dump([row] * 5))
        rep = usage.report(s, "cycle", command="/example")
        self.assertEqual(sum(a["calls"] for a in rep["turns"][0]["agents"]), 150)
        self.assertEqual(rep["total"]["agents"], 1050)


class SessionPath(Scratch):
    def test_r3_finds_the_one_transcript_and_refuses_paths(self):
        proj = self.d / ".claude" / "projects" / "-x"
        proj.mkdir(parents=True)
        (proj / "abc-1.jsonl").write_text("")
        (proj.parent / "x.jsonl").write_text("")  # what "../x" would reach
        (proj / "a").mkdir()
        (proj / "a" / "b.jsonl").write_text("")  # and "a/b"
        with mock.patch.dict(os.environ, {"HOME": str(self.d)}):
            self.assertEqual(usage.session_path("abc-1"), proj / "abc-1.jsonl")
            self.assertIsNone(usage.session_path("abc-2"))
            for bad in ("../x", "a/b", "", "x" * 65):
                self.assertIsNone(usage.session_path(bad))


class Contract(Scratch):
    """R5: one `seen` per report; under "all" it spans the session. R6: by_key in nano-dollars."""

    def setUp(self):
        super().setUp()
        self.s = self.d / "s.jsonl"
        self.s.write_text(uc.dump([uc.typed("one")] + uc.call("t1", 100) + [uc.typed("two")] + uc.call("t2", 200, tools=["tuG"])))
        sub = self.s.with_suffix("") / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-G.meta.json").write_text(json.dumps({"toolUseId": "tuG", "description": "fork", "isFork": True}))
        (sub / "agent-G.jsonl").write_text(uc.dump(uc.call("t1", 100) + uc.call("g1", 50)))

    def test_r5_all_counts_a_replayed_turn_one_call_once(self):
        every = usage.report(self.s, "all")
        self.assertEqual([t["lead"]["tokens"] for t in every["turns"]], [100, 200])
        self.assertEqual(every["turns"][1]["agents"][0]["tokens"], 50)
        self.assertEqual(every["total"]["tokens"], 350)
        two = usage.report(self.s, "2")
        self.assertEqual(two["turns"][0]["agents"][0]["tokens"], 150)

    def test_r5_keys_stay_and_nanos_is_added(self):
        rep = usage.report(self.s, "all")
        walk = {"calls", "start", "end", "peak", "output", "cached", "tokens", "usd", "unpriced", "models", "top", "nanos"}
        self.assertEqual(set(rep["turns"][1]["agents"][0]) - {"agent", "type", "budget", "over"}, walk)
        self.assertEqual(set(rep["turns"][0]["lead"]), walk | {"added", "budget", "over"})
        self.assertEqual(set(rep["total"]), {"tokens", "output", "cached", "unpriced", "usd", "lead", "lead_usd", "agents",
                                             "agents_usd", "n_agents", "lead_start", "lead_end", "peak_agent", "nanos"})
        for level in [rep["total"], rep["turns"][0]["lead"], rep["turns"][1]["agents"][0]]:
            self.assertIsInstance(level["usd"], float)
            self.assertIsInstance(level["nanos"], int)
            self.assertEqual(level["usd"], usage.cycle_usd(level["nanos"]))

    def test_r6_by_key_agrees_with_the_cycle_line(self):
        s = self.d / "p.jsonl"
        calls = [uc.priced_call(i, c) for i, c in enumerate(uc.PRICED)]
        s.write_text(uc.dump([uc.typed("/example")] + uc.call("m0", 0, tools=["tu0", "tu1"]) ))
        sub = s.with_suffix("") / "subagents"
        sub.mkdir(parents=True)
        # One call, then nine: the grouping in which 0.14.1's rounded float sum prints $0.41.
        for i, part in enumerate([calls[:1], calls[1:]]):
            (sub / f"agent-k{i}.meta.json").write_text(json.dumps({"toolUseId": f"tu{i}", "description": f"Build: ABC-1 {i}"}))
            (sub / f"agent-k{i}.jsonl").write_text(uc.dump(part))
        rep = usage.report(s, "cycle", command="/example")
        row = usage.by_key([rep])["ABC-1"]
        self.assertEqual(row["nanos"], uc.PRICED_NANOS)
        self.assertEqual(row["nanos"], rep["total"]["nanos"])
        self.assertEqual(usage.usd_text(row["usd"]), "$0.40")
        self.assertTrue(usage.line(rep).endswith("API price: $0.40"))


if __name__ == "__main__":
    unittest.main()
