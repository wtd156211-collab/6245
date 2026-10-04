"""In-process engine tests: replay the sample change stream and compare
against every expected artifact under samples/expected/."""

import json
import unittest
from pathlib import Path

from viewkeep.defs import Schema, ViewDef, load_json
from viewkeep.engine import Engine, StepError

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"


def load_engine():
    schema = Schema(load_json(SAMPLES / "init" / "schema.json"))
    defs = [ViewDef(v, schema) for v in load_json(SAMPLES / "init" / "views.json")["views"]]
    engine = Engine(schema, defs)
    engine.build(load_json(SAMPLES / "init" / "tables.json"))
    return engine


def change_steps():
    for i in range(1, 7):
        with open(SAMPLES / "changes" / f"part-0{i}.jsonl", encoding="utf-8") as f:
            for line in f:
                rec = json.loads(line)
                yield rec["step"], rec["ops"]


class ReplayTest(unittest.TestCase):
    """Replay all 3000 steps once; every expected artifact must match."""

    @classmethod
    def setUpClass(cls):
        cls.engine = load_engine()
        cls.deltas = {}       # step -> {view: delta}
        cls.logs = []         # per applied step log entries
        cls.snapshots = {0: cls.engine.snapshot()}
        cls.query_results = {}  # (step, view, key-json) -> row

        queries = {}
        with open(SAMPLES / "expected" / "queries.jsonl", encoding="utf-8") as f:
            for line in f:
                q = json.loads(line)
                queries.setdefault(q["step"], []).append(q)

        def answer_queries(step):
            for q in queries.get(step, ()):
                cls.query_results[(step, q["view"], json.dumps(q["key"]))] = (
                    cls.engine.query(q["view"], q["key"]))

        answer_queries(0)
        for step, ops in change_steps():
            got = cls.engine.apply_step(step, ops)
            cls.deltas[step] = got or {}
            cls.logs.append({
                "watermark": step,
                "ops": len(ops),
                "changed_rows": {
                    name: len(d["inserted"]) + len(d["updated"]) + len(d["deleted"])
                    for name, d in (got or {}).items()
                },
            })
            if step % 250 == 0:
                cls.snapshots[step] = cls.engine.snapshot()
            answer_queries(step)

    def test_deltas_match_expected(self):
        with open(SAMPLES / "expected" / "deltas.jsonl", encoding="utf-8") as f:
            expected = [json.loads(line) for line in f]
        self.assertEqual(len(expected), 3000)
        for rec in expected:
            self.assertEqual(self.deltas[rec["step"]], rec["views"],
                             f"step {rec['step']}")

    def test_checkpoints_match_expected(self):
        for step in range(0, 3001, 250):
            with open(SAMPLES / "expected" / "snapshots" / f"step-{step:06d}.json",
                      encoding="utf-8") as f:
                expected = json.load(f)
            self.assertEqual(self.snapshots[step], expected, f"checkpoint {step}")

    def test_queries_match_expected(self):
        with open(SAMPLES / "expected" / "queries.jsonl", encoding="utf-8") as f:
            expected = [json.loads(line) for line in f]
        self.assertEqual(len(self.query_results), len(expected))
        for q in expected:
            got = self.query_results[(q["step"], q["view"], json.dumps(q["key"]))]
            self.assertEqual(got, q["row"],
                             f"step {q['step']} {q['view']} {q['key']}")

    def test_log_tail_matches_expected(self):
        with open(SAMPLES / "expected" / "log_tail.jsonl", encoding="utf-8") as f:
            tail = [json.loads(line) for line in f]
        self.assertEqual(len(tail), 200)
        self.assertEqual(self.logs[-200:], tail)
        self.assertEqual(self.logs[-200]["watermark"], 2801)

    def test_watermark(self):
        self.assertEqual(self.engine.watermark, 3000)

    def test_deterministic_replay(self):
        again = load_engine()
        for step, ops in change_steps():
            again.apply_step(step, ops)
        self.assertEqual(again.snapshot(), self.engine.snapshot())

    def test_idempotent_skip(self):
        before = self.engine.snapshot()
        self.assertIsNone(self.engine.apply_step(3000, []))
        self.assertIsNone(self.engine.apply_step(1, []))
        self.assertEqual(self.engine.snapshot(), before)

    def test_gap_step_rejected(self):
        with self.assertRaises(StepError):
            self.engine.apply_step(3002, [])


class StepSemanticsTest(unittest.TestCase):
    """Hand-built scenarios for the tricky propagation rules."""

    def setUp(self):
        self.engine = load_engine()

    def test_empty_group_disappears_and_reappears(self):
        eng = self.engine
        row = {"order_id": "T1", "customer_id": "C001", "region": "moon",
               "channel": "web", "status": "paid", "amount": 5, "qty": 1,
               "updated_at": 1}
        eng.apply_step(1, [{"op": "insert", "table": "orders", "row": row}])
        got = eng.query("v_region_channel", ["moon", "web"])
        self.assertIsNotNone(got)
        # delete the only row: group must vanish, no zero-count row left
        eng.apply_step(2, [{"op": "delete", "table": "orders", "key": "T1"}])
        self.assertIsNone(eng.query("v_region_channel", ["moon", "web"]))
        keys = [r["key"] for v in eng.snapshot()["views"]
                if v["view"] == "v_region_channel" for r in v["rows"]]
        self.assertNotIn(["moon", "web"], keys)
        # vanished entry records appearance/disappearance and last values
        view = eng.views["v_region_channel"]
        entry = view.vanished[("moon", "web")]
        self.assertEqual(entry["appeared_step"], 1)
        self.assertEqual(entry["vanished_step"], 2)
        self.assertEqual(entry["last_values"]["order_count"], 1)
        # re-insert: group rebuilt as new data, vanished entry cleared
        eng.apply_step(3, [{"op": "insert", "table": "orders", "row": row}])
        got = eng.query("v_region_channel", ["moon", "web"])
        self.assertEqual(got["values"]["order_count"], 1)
        self.assertNotIn(("moon", "web"), view.vanished)
        self.assertEqual(view.appeared[("moon", "web")], 3)

    def test_update_moves_row_between_groups(self):
        eng = self.engine
        before = eng.query("v_status_totals", ["paid"])
        eng.apply_step(1, [{"op": "update", "table": "orders",
                            "key": "O000002",  # status created -> paid
                            "set": {"status": "paid"}}])
        after = eng.query("v_status_totals", ["paid"])
        self.assertEqual(after["values"]["order_count"],
                         before["values"]["order_count"] + 1)

    def test_net_effect_within_one_step(self):
        eng = self.engine
        before = eng.snapshot()
        # insert then delete the same row in one step: net zero
        row = {"order_id": "T2", "customer_id": "C001", "region": "south",
               "channel": "web", "status": "paid", "amount": 9, "qty": 1,
               "updated_at": 1}
        deltas = eng.apply_step(1, [
            {"op": "insert", "table": "orders", "row": row},
            {"op": "delete", "table": "orders", "key": "T2"},
        ])
        self.assertEqual(deltas, {})
        after = eng.snapshot()
        after["step"] = before["step"]
        self.assertEqual(after, before)

    def test_dimension_delete_and_reinsert(self):
        eng = self.engine
        # C001 leaves the join view entirely, then comes back
        before = eng.query("v_tier_region", ["plus", "south"])
        eng.apply_step(1, [{"op": "delete", "table": "customers", "key": "C001"}])
        mid = eng.query("v_tier_region", ["plus", "south"])
        self.assertLess(mid["values"]["order_count"],
                        before["values"]["order_count"])
        eng.apply_step(2, [{"op": "insert", "table": "customers",
                            "row": {"customer_id": "C001", "tier": "plus",
                                    "city": "beijing"}}])
        after = eng.query("v_tier_region", ["plus", "south"])
        self.assertEqual(after, before)

    def test_unreferenced_column_change_is_noop(self):
        eng = self.engine
        before = eng.snapshot()
        deltas = eng.apply_step(1, [{"op": "update", "table": "customers",
                                     "key": "C001", "set": {"city": "nowhere"}}])
        self.assertEqual(deltas, {})
        after = eng.snapshot()
        after["step"] = before["step"]
        self.assertEqual(after, before)

    def test_invalid_ops_rejected_atomically(self):
        eng = self.engine
        before = eng.snapshot()
        bad_steps = [
            [{"op": "insert", "table": "orders",
              "row": {"order_id": "O000001", "customer_id": "C001",
                      "region": "south", "channel": "web", "status": "paid",
                      "amount": 1, "qty": 1, "updated_at": 1}}],
            [{"op": "update", "table": "orders", "key": "NOPE", "set": {"qty": 1}}],
            [{"op": "delete", "table": "orders", "key": "NOPE"}],
            [{"op": "update", "table": "orders", "key": "O000001",
              "set": {"order_id": "X"}}],
            # first op valid, second invalid: whole step must be rejected
            [{"op": "update", "table": "orders", "key": "O000001",
              "set": {"qty": 999}},
             {"op": "delete", "table": "orders", "key": "NOPE"}],
        ]
        for ops in bad_steps:
            with self.assertRaises(StepError):
                eng.apply_step(1, ops)
        self.assertEqual(eng.snapshot(), before)
        self.assertEqual(eng.watermark, 0)


if __name__ == "__main__":
    unittest.main()
