"""crewkit.stopline: the cycle's usage line as a Stop hook, driven through `total` on U1's fixture."""
import io
import json
import os
import random
import shutil
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import usagecase as uc

from crewkit import stopline, usage


def later():
    return time.monotonic() + 60


def run(s, st, skill="example", others=(), deadline=None, last=None):
    return stopline.total(Path(s), skill, st, set(others), later() if deadline is None else deadline, last)


def line(res):
    return usage.line({"total": res}) if res else None


class Case(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)

    def fresh(self, s, **kw):
        return run(s, {}, **kw)

    def lead(self, rows, name="s"):
        s = self.d / f"{name}.jsonl"
        s.write_text(uc.dump(rows), encoding="utf-8")
        return s

    def append(self, s, rows):
        with open(s, "a", encoding="utf-8") as f:
            f.write(uc.dump(rows))


class Counting(Case):
    def test_u1_any_split_of_the_lead_counts_each_call_once(self):
        s = uc.build(self.d)
        data = s.read_bytes()
        for k in range(len(data) + 1):
            s.write_bytes(data[:k])
            st = {}
            run(s, st)
            s.write_bytes(data)
            res = run(s, st)
            self.assertEqual((k, res and res["tokens"], res and res["nanos"]), (k, uc.TOKENS, uc.NANOS))
            self.assertEqual(line(res), uc.LINE)

    def test_u1b_the_cents_do_not_depend_on_order_or_grouping(self):
        calls = [uc.priced_call(i, c) for i, c in enumerate(uc.PRICED)]
        rnd = random.Random(7)
        for trial in range(50):
            d = self.d / f"t{trial}"
            d.mkdir()
            order = calls[:]
            rnd.shuffle(order)
            cuts = sorted(rnd.sample(range(1, 10), rnd.randint(1, 5)))
            groups = [order[a:b] for a, b in zip([0, *cuts], [*cuts, 10])]
            if trial == 0:
                groups = [calls[:1], calls[1:]]  # the grouping in which a rounded float sum prints $0.41
            s = d / "s.jsonl"
            s.write_text(uc.dump([uc.typed("/example")] + uc.call("m0", 0, tools=[f"tu{i}" for i in range(len(groups))])))
            sub = d / "s" / "subagents"
            sub.mkdir(parents=True)
            for i, g in enumerate(groups):
                (sub / f"agent-g{i}.meta.json").write_text(json.dumps({"toolUseId": f"tu{i}"}))
                (sub / f"agent-g{i}.jsonl").write_text(uc.dump(g))
            res = run(s, {})
            self.assertEqual((trial, res["nanos"], usage.usd_text(res["usd"])), (trial, uc.PRICED_NANOS, "$0.40"))

    def test_u1d_a_fork_replaying_a_pre_cycle_call_counts_it_once(self):
        st = {}
        run(uc.build(self.d), st)
        self.assertEqual(st["agents"]["F"]["tokens"], uc.AGENT_TOKENS["F"])

    def test_u2_no_typed_start_is_silent(self):
        self.assertIsNone(self.fresh(self.lead([uc.typed("hello")] + uc.call("m1", 10))))

    def test_u2b_a_skill_tool_call_is_not_a_start(self):
        skill = [{"type": "assistant", "message": {"id": "m1", "model": "claude-opus-5-5", "usage": {"input_tokens": 1},
                  "content": [{"type": "tool_use", "id": "t", "name": "Skill", "input": {"skill": "example"}}]}}]
        self.assertIsNone(self.fresh(self.lead([uc.typed("go on")] + skill + uc.call("m2", 10))))

    def test_u3_a_new_start_resets_sums_seen_and_selection(self):
        s = uc.build(self.d)
        data, st = s.read_bytes(), {}
        s.write_bytes(data[:data.index(b"/example go")])
        run(s, st)
        self.assertIn("Z", st["agents"])
        s.write_bytes(data)
        res = run(s, st)
        self.assertNotIn("Z", st["agents"])
        self.assertEqual(res["tokens"], uc.TOKENS)


class Agents(Case):
    def one_agent(self, meta):
        s = self.lead([uc.typed("/example")] + uc.call("m0", 1, tools=["t1"]))
        sub = s.with_suffix("") / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a.meta.json").write_text(meta)
        return s, sub

    def test_u4_a_growing_agent_and_a_late_meta(self):
        s = self.lead([uc.typed("/example")] + uc.call("m0", 1, tools=["t1", "t2"]))
        sub = s.with_suffix("") / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-a.meta.json").write_text(json.dumps({"toolUseId": "t1"}))
        (sub / "agent-a.jsonl").write_text(uc.dump(uc.call("a1", 10)))
        (sub / "agent-b.jsonl").write_text(uc.dump(uc.call("b1", 100)))
        st = {}
        self.assertEqual(run(s, st)["tokens"], 11)
        self.append(sub / "agent-a.jsonl", uc.call("a2", 1000))
        (sub / "agent-b.meta.json").write_text(json.dumps({"toolUseId": "t2"}))
        self.assertEqual(run(s, st)["tokens"], 1111)

    def test_u11_an_empty_meta_skips_its_agent_until_it_parses(self):
        s, sub = self.one_agent("")
        (sub / "agent-a.jsonl").write_text(uc.dump(uc.call("a1", 10)))
        st = {}
        self.assertEqual(run(s, st)["tokens"], 1)
        (sub / "agent-a.meta.json").write_text(json.dumps({"toolUseId": "t1"}))
        self.assertEqual(run(s, st)["tokens"], 11)

    def test_u7b_the_deadline_fires_inside_a_large_agent_file(self):
        s, sub = self.one_agent(json.dumps({"toolUseId": "t1"}))
        pad = "x" * 1000
        big = sub / "agent-a.jsonl"
        big.write_text(uc.dump([r for i in range(20_000) for r in uc.call(f"a{i}", 1, text=pad)]))
        size = big.stat().st_size
        self.assertGreater(size, 20_000_000)
        ticks = iter([0.0, 0.0] + [1e12] * 10)
        st = {}
        with mock.patch.object(stopline, "WINDOW", 1 << 20), mock.patch.object(stopline.time, "monotonic", lambda: next(ticks)):
            self.assertIsNone(run(s, st, deadline=1.0))
        self.assertLess(st["agents"]["a"]["off"], size)
        self.assertGreater(st["agents"]["a"]["off"], 0)
        self.assertEqual(run(s, st)["tokens"], 20_001)


class Identity(Case):
    def counted(self, s):
        return run(s, {})

    def test_u5_lead_rewritten_moved_or_shortened_recounts(self):
        s = uc.build(self.d)
        data = s.read_bytes()
        for change in ("inode", "shorter", "in place"):
            st = {}
            run(s, st)
            if change == "inode":
                tmp = self.d / "new.jsonl"
                tmp.write_bytes(data)
                os.replace(tmp, s)
            elif change == "shorter":
                s.write_bytes(data[:data.index(b"/example go")])
            else:
                s.write_bytes(data.replace(b'"mL3-0"', b'"mL3-9"') + uc.dump(uc.call("mX", 5)).encode())
            self.assertEqual((change, run(s, st)), (change, self.counted(s)))
            s.write_bytes(data)

    def test_u5b_an_agent_rewritten_in_place_recounts_everything(self):
        s = uc.build(self.d)
        st = {}
        run(s, st)
        a = s.with_suffix("") / "subagents" / "agent-A.jsonl"
        a.write_text(uc.dump(uc.call("mR1", 1000, 100, tools=["tuB"]) + uc.call("mR2", 3000) + uc.call("mR3", 1, text="r" * 300)))
        self.assertEqual(run(s, st), self.counted(s))
        self.assertNotEqual(run(s, {})["tokens"], uc.TOKENS)

    def test_u6_corrupt_state_counts_afresh(self):
        s = uc.build(self.d)
        f = self.d / "state.json"
        f.write_text('{"v": 1, "lead": {"off": 12')
        self.assertEqual(stopline.load(f), {})
        self.assertEqual(line(run(s, stopline.load(f))), uc.LINE)

    def test_u12_no_cycle_keeps_an_offset_and_a_hash_only(self):
        s = self.lead([uc.typed("hello")] + uc.call("m1", 10))
        st = {}
        self.assertIsNone(run(s, st))
        f = self.d / "x-example.json"
        stopline.save(f, st)
        saved = json.loads(f.read_text())
        self.assertEqual(set(saved), {"v", "lead"})
        self.assertEqual(set(saved["lead"]), {"dev", "ino", "off", "hash"})


class Windows(Case):
    def test_u7_deadline_after_the_first_window_then_resume(self):
        s = uc.build(self.d)
        st = {}
        with mock.patch.object(stopline, "WINDOW", 200):
            self.assertIsNone(run(s, st, deadline=time.monotonic() - 1))
            self.assertLess(st["lead"]["off"], s.stat().st_size)
            self.assertEqual(line(run(s, st)), uc.LINE)

    def test_u10_a_row_longer_than_a_window_counts(self):
        s = self.lead([uc.typed("/example")] + uc.call("m1", 5, text="y" * (9 << 20)) + uc.call("m2", 7))
        st = {}
        self.assertEqual(run(s, st)["tokens"], 12)
        self.assertEqual(st["lead"]["off"], s.stat().st_size)

    def filler(self, n, hit=False):
        text = ("<command-name>/example " if hit else "") + "z" * 900
        return [{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t", "content": text}]}}
                for _ in range(n)]

    def counting(self):
        calls = []
        real = json.loads
        return calls, mock.patch.object(usage.json, "loads", lambda b, *a, **k: calls.append(1) or real(b, *a, **k))

    def test_u17_new_bytes_with_no_marker_are_not_parsed(self):
        s = self.lead([])
        st = {}
        run(s, st)
        self.append(s, self.filler(22_000))
        calls, patch = self.counting()
        with patch:
            self.assertIsNone(run(s, st))
        self.assertEqual(len(calls), 0)
        self.assertEqual(st["lead"]["off"], s.stat().st_size)

    def test_u17_a_marker_row_across_the_cut_is_found_whole(self):
        s = self.lead([])
        st = {}
        run(s, st)
        per = len(uc.dump(self.filler(1)))
        self.append(s, self.filler(((8 << 20) - 40) // per))
        before = s.stat().st_size
        self.append(s, [uc.typed("/example " + "p" * 1200)] + uc.call("m9", 3))
        self.assertLess(before + 20, 8 << 20)
        self.assertGreater(before + 1200, 8 << 20)  # the start row straddles the 8 MB cut
        self.assertEqual(run(s, st)["tokens"], 3)

    def test_u18_backward_search_parses_only_the_last_window_and_skips_false_hits(self):
        rows = self.filler(10_000) + self.filler(1, hit=True) + self.filler(12_000) + [uc.typed("/example go")] + uc.call("m9", 3)
        s = self.lead(rows)
        self.assertGreater(s.stat().st_size, 20_000_000)
        calls, patch = self.counting()
        with patch:
            self.assertEqual(run(s, {})["tokens"], 3)
        self.assertLess(len(calls), 10_000)  # one window of about 8,900 rows, of 22,000

    def test_u18_a_false_hit_in_the_last_window_sends_the_search_back(self):
        rows = uc.lead_rows() + self.filler(22_000) + self.filler(1, hit=True)
        s = self.lead(rows, name="sess")
        uc.write_agents(s)
        self.assertEqual(line(run(s, {})), uc.LINE)


class Others(Case):
    def test_u8_another_crews_later_start_silences_until_typed_again(self):
        s = self.lead([uc.typed("/example")] + uc.call("d1", 10, text="r1"))
        st = {}
        self.assertIsNotNone(run(s, st, others={"other"}))
        self.append(s, [uc.typed("/other")] + uc.call("e1", 20, text="r2"))
        self.assertIsNone(run(s, st, others={"other"}))
        self.append(s, [uc.typed("and then?")] + uc.call("e2", 30, text="r3"))
        self.assertIsNone(run(s, st, others={"other"}))
        self.append(s, [uc.typed("/example again")] + uc.call("d2", 40, text="r4"))
        self.assertEqual(run(s, st, others={"other"})["tokens"], 40)

    def test_u8b_the_backward_first_run_rebuilds_other_at(self):
        s = self.lead([uc.typed("/example")] + uc.call("d1", 10) + [uc.typed("/other")] + uc.call("e1", 20))
        st = {}
        self.assertIsNone(run(s, st, others={"other"}))
        self.append(s, [uc.typed("/example")] + uc.call("d2", 40))
        self.assertEqual(run(s, st, others={"other"})["tokens"], 40)

    def test_u8c_own_skill_is_dropped_and_a_tie_is_silent_for_both(self):
        both = self.lead([uc.typed("<command-name>/example</command-name> see <command-name>/other")] + uc.call("x", 1), "b")
        self.assertIsNone(run(both, {}, others={"other"}))
        self.assertIsNone(run(both, {}, skill="other", others={"example"}))

    def test_u8c_the_hook_drops_its_own_skill_from_installed_skills(self):
        s = self.lead([uc.typed("/example")] + uc.call("d1", 10))
        home = self.d / "home"
        with mock.patch.dict(os.environ, {"HOME": str(home)}), mock.patch.object(stopline.spec, "installed_skills",
                                                                                 lambda: {"example", "other"}):
            out = stopline._run("example", uc.event(s))
        self.assertEqual(out, "Tokens this cycle: 0.0k · API price: $0.00")


class FinalCall(Case):
    def home(self):
        patch = mock.patch.dict(os.environ, {"HOME": str(self.d / "home")})
        patch.start()
        self.addCleanup(patch.stop)
        os.environ.pop("XDG_CACHE_HOME", None)

    def test_u9_two_text_blocks_print_and_a_missing_call_is_silent(self):
        self.home()
        rows = [uc.typed("/example")] + uc.call("f1", 10, 0, rows=2, tools=["x"])
        rows[1]["message"]["content"] = [{"type": "text", "text": "hello\n"}]
        rows[2]["message"]["content"] = [{"type": "text", "text": "  world"}]
        s = self.lead(rows)
        self.assertIsNotNone(stopline._run("example", uc.event(s, last="hello world")))
        self.assertIsNotNone(stopline._run("example", uc.event(s, last="hello world")))
        self.append(s, [uc.typed("more")] + uc.call("f2", 10, text="first"))
        with mock.patch.object(stopline.time, "sleep") as slept:
            self.assertIsNone(stopline._run("example", uc.event(s, last="the reply not yet written")))
        self.assertEqual(slept.call_count, 3)


class Files(Case):
    def test_u13_prune_by_name_and_age_once_a_day(self):
        now = time.time()
        f = self.d / "st"
        f.mkdir()
        ages = {"a-1-example.json": 31, "a-2-example.json": 29, "a-3-example.json.k9x_.tmp": 2,
                "a-4-example.json.k9x_.tmp": 1 / 24, "notes.txt": 99}
        for n, days in ages.items():
            (f / n).write_text("")
            os.utime(f / n, (now - days * 86400, now - days * 86400))
        stopline.prune(f, now)
        self.assertEqual(sorted(p.name for p in f.iterdir() if p.name != ".pruned"),
                         ["a-2-example.json", "a-4-example.json.k9x_.tmp", "notes.txt"])
        (f / "a-5-example.json").write_text("")
        os.utime(f / "a-5-example.json", (now - 40 * 86400,) * 2)
        stopline.prune(f, now + 60)
        self.assertTrue((f / "a-5-example.json").exists())

    def test_u14_state_path_names_and_refusals(self):
        with mock.patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.d)}):
            self.assertEqual(stopline.state_path("abc-1", "example"), self.d / "crewkit" / "stopline" / "abc-1-example.json")
            for sid, skill in (("abc", "x-other"), ("abc", "../x"), ("../x", "example"), ("a/b", "example"), ("abc", "")):
                self.assertIsNone(stopline.state_path(sid, skill))

    def test_u15_two_interleaved_saves_both_land(self):
        f = self.d / "x-example.json"
        real = os.replace
        nested = []

        def replace(a, b):
            if not nested:
                nested.append(1)
                stopline.save(f, {"lead": {"off": 2}})
            real(a, b)
        with mock.patch.object(stopline.os, "replace", replace):
            stopline.save(f, {"lead": {"off": 1}})
        self.assertEqual(json.loads(f.read_text())["lead"]["off"], 1)
        self.assertEqual([p.name for p in self.d.iterdir()], ["x-example.json"])

    def test_u16_the_alarm_is_cancelled_before_the_line_is_written(self):
        s = uc.build(self.d)
        seen = []

        class Out(io.StringIO):
            def write(self, x):
                seen.append(signal.getitimer(signal.ITIMER_REAL))
                return super().write(x)
        out = Out()
        with mock.patch.dict(os.environ, {"HOME": str(self.d / "home"), "XDG_CACHE_HOME": str(self.d / "c")}), \
                mock.patch.object(sys, "stdin", io.StringIO(uc.event(s))), mock.patch.object(sys, "stdout", out), \
                mock.patch.object(sys, "stderr", sys.stderr):
            self.assertEqual(stopline.main(["example"]), 0)
        self.assertEqual(json.loads(out.getvalue())["systemMessage"], uc.LINE)
        self.assertEqual(seen[0], (0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
