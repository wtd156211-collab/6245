"""viewkeep 自测:只读 samples/,用 unittest。

覆盖:逐步 delta 对拍、检查点快照、按键查询、日志窗口回收、
空分组消失/重建口径、两遍逐字一致、断点续跑、校验失败回滚、
CLI 行为与 web/data.json 内容。
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import viewkeep  # noqa: E402

INIT_DIR = os.path.join(REPO, "samples", "init")
EXPECTED = os.path.join(REPO, "samples", "expected")
CHANGE_FILES = [
    os.path.join(REPO, "samples", "changes", "part-0%d.jsonl" % i)
    for i in range(1, 7)
]
CHECKPOINT_STEPS = list(range(0, 3001, viewkeep.CHECKPOINT_EVERY))


def load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_jsonl(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def replay(engine, upto=3000, queries_by_step=None, snapshots=None):
    """应用前 upto 步,返回逐步 delta 记录;顺带校验查询与快照。"""
    records = []
    if queries_by_step is not None:
        check_queries(engine, queries_by_step.get(0, []))
    if snapshots is not None and 0 in snapshots:
        assert_snapshot(engine, snapshots[0])
    for path in CHANGE_FILES:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                rec = json.loads(line)
                if rec["step"] > upto:
                    return records
                result = engine.apply_step(rec["step"], rec["ops"])
                if result is None:
                    continue  # 续跑时跳过已应用的步
                records.append(result)
                if queries_by_step is not None:
                    check_queries(engine, queries_by_step.get(rec["step"], []))
                if snapshots is not None and rec["step"] in snapshots:
                    assert_snapshot(engine, snapshots[rec["step"]])
    return records


def check_queries(engine, queries):
    for q in queries:
        got = engine.query_row(q["view"], q["key"])
        if got != q["row"]:
            raise AssertionError("查询不符 step=%s %s %s: got %r" % (
                q["step"], q["view"], q["key"], got))


def assert_snapshot(engine, expected):
    got = engine.snapshot()
    if got != expected:
        raise AssertionError("快照不符 step=%s" % expected["step"])


class ReplayTestCase(unittest.TestCase):
    """全量 3000 步对拍,整个类只跑一遍。"""

    @classmethod
    def setUpClass(cls):
        cls.expected_deltas = load_jsonl(
            os.path.join(EXPECTED, "deltas.jsonl"))
        cls.snapshots = {
            step: load_json(os.path.join(
                EXPECTED, "snapshots", "step-%06d.json" % step))
            for step in CHECKPOINT_STEPS
        }
        cls.queries_by_step = {}
        for q in load_jsonl(os.path.join(EXPECTED, "queries.jsonl")):
            cls.queries_by_step.setdefault(q["step"], []).append(q)
        cls.engine = viewkeep.Engine.build(INIT_DIR)
        cls.records = replay(
            cls.engine,
            queries_by_step=cls.queries_by_step,
            snapshots=cls.snapshots,
        )

    def test_step_deltas(self):
        self.assertEqual(len(self.records), 3000)
        for got, exp in zip(self.records, self.expected_deltas):
            self.assertEqual(
                {"step": got["step"], "views": got["views"]},
                {"step": exp["step"], "views": exp["views"]},
                "step %d delta 不符" % exp["step"])

    def test_checkpoints_and_queries(self):
        # setUpClass 的 replay 已逐步断言;这里确认数据确实覆盖到位
        self.assertEqual(len(self.snapshots), 13)
        self.assertEqual(sum(len(v) for v in self.queries_by_step.values()),
                         3620)
        self.assertEqual(self.engine.watermark, 3000)

    def test_empty_group_semantics(self):
        view = self.engine.view_by_name["v_customer_orders"]
        # 空分组从视图内容消失,不留 count=0 零值行
        for key, group in view.groups.items():
            self.assertGreater(group.count, 0, "存在零值分组 %r" % (key,))
        # 消失分组有完整记录,且不在现存分组里
        self.assertEqual(len(view.vanished), 22)
        for key, item in view.vanished.items():
            self.assertNotIn(key, view.groups)
            self.assertEqual(item["key"], list(key))
            self.assertIn("appeared_step", item)
            self.assertIn("vanished_step", item)
            self.assertIn("last_values", item)

    def test_log_window(self):
        self.assertEqual(len(self.engine.log), viewkeep.LOG_WINDOW)
        self.assertEqual(self.engine.log[0]["watermark"], 2801)
        self.assertEqual(self.engine.log[-1]["watermark"], 3000)

    def test_saved_log_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.engine.save(tmp)
            got = load_jsonl(os.path.join(tmp, "log", "applied.jsonl"))
        exp = load_jsonl(os.path.join(EXPECTED, "log_tail.jsonl"))
        self.assertEqual(got, exp)
        self.assertEqual(len(got), 200)

    def test_determinism_bytes(self):
        """同一批变更跑两遍,落盘文件逐字一致。"""
        blobs = []
        for _ in range(2):
            engine = viewkeep.Engine.build(INIT_DIR)
            replay(engine)
            with tempfile.TemporaryDirectory() as tmp:
                engine.save(tmp)
                run = {}
                for root, _dirs, files in os.walk(tmp):
                    for name in files:
                        with open(os.path.join(root, name), "rb") as fh:
                            run[os.path.relpath(os.path.join(root, name), tmp)] = fh.read()
                blobs.append(run)
        self.assertEqual(blobs[0].keys(), blobs[1].keys())
        for name in blobs[0]:
            self.assertEqual(blobs[0][name], blobs[1][name],
                             "文件 %s 两遍不一致" % name)

    def test_split_apply_matches_single_run(self):
        """跑一半落盘重启续跑,与一次跑完的最终视图一致。"""
        single = viewkeep.Engine.build(INIT_DIR)
        replay(single)
        with tempfile.TemporaryDirectory() as tmp:
            half = viewkeep.Engine.build(INIT_DIR)
            replay(half, upto=1500)
            half.save(tmp)
            resumed = viewkeep.Engine.load(INIT_DIR, tmp)
            replay(resumed, upto=3000)
            resumed.save(tmp)
        self.assertEqual(single.snapshot(), resumed.snapshot())
        self.assertEqual(single.deltas, resumed.deltas)
        self.assertEqual(single.log, resumed.log)
        self.assertEqual(single.checkpoints, resumed.checkpoints)


class StepValidationTestCase(unittest.TestCase):
    """整步失败与回滚、幂等跳过、跳号拒绝。"""

    def setUp(self):
        self.engine = viewkeep.Engine.build(INIT_DIR)

    def test_insert_existing_pk_fails_and_rolls_back(self):
        before = self.engine.snapshot()
        with self.assertRaises(viewkeep.StepError):
            self.engine.apply_step(1, [
                {"op": "insert", "table": "orders",
                 "row": {"order_id": "O009999", "customer_id": "C001",
                         "region": "south", "channel": "web",
                         "status": "paid", "amount": 100, "qty": 1,
                         "updated_at": 1}},
                {"op": "insert", "table": "orders",
                 "row": {"order_id": "O000001", "customer_id": "C001",
                         "region": "south", "channel": "web",
                         "status": "paid", "amount": 100, "qty": 1,
                         "updated_at": 1}},
            ])
        self.assertEqual(self.engine.watermark, 0)
        self.assertEqual(self.engine.snapshot(), before)
        self.assertNotIn("O009999", self.engine.tables["orders"])

    def test_update_delete_missing_pk(self):
        for op in ({"op": "update", "table": "orders", "key": "NOPE",
                    "set": {"amount": 1}},
                   {"op": "delete", "table": "orders", "key": "NOPE"}):
            with self.assertRaises(viewkeep.StepError):
                self.engine.apply_step(1, [op])
        self.assertEqual(self.engine.watermark, 0)

    def test_update_set_must_not_touch_pk(self):
        with self.assertRaises(viewkeep.StepError):
            self.engine.apply_step(1, [
                {"op": "update", "table": "orders", "key": "O000001",
                 "set": {"order_id": "O000002"}}])

    def test_gap_rejected_and_replay_skipped(self):
        with self.assertRaises(viewkeep.StepError):
            self.engine.apply_step(2, [])
        self.assertIsNone(self.engine.apply_step(0, []))
        self.assertEqual(self.engine.watermark, 0)

    def test_net_effect_within_step(self):
        # 同一步内插入又删除同一行:净效果为零,无视图变化
        record = self.engine.apply_step(1, [
            {"op": "insert", "table": "orders",
             "row": {"order_id": "O009999", "customer_id": "C001",
                     "region": "south", "channel": "web", "status": "paid",
                     "amount": 100, "qty": 1, "updated_at": 1}},
            {"op": "delete", "table": "orders", "key": "O009999"},
        ])
        self.assertEqual(record["views"], {})
        self.assertEqual(self.engine.watermark, 1)

    def test_unreferenced_column_change_is_noop(self):
        record = self.engine.apply_step(1, [
            {"op": "update", "table": "customers", "key": "C001",
             "set": {"city": "nowhere"}}])
        self.assertEqual(record["views"], {})


class CliTestCase(unittest.TestCase):
    """CLI 端到端:临时目录里 build/apply/dump/query,只读 samples。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        os.symlink(os.path.join(REPO, "samples"),
                   os.path.join(cls.tmp.name, "samples"))
        cls.app = os.path.join(REPO, "app.py")

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_cli(self, *argv):
        return subprocess.run(
            [sys.executable, self.app, *argv],
            cwd=self.tmp.name, capture_output=True, text=True)

    def test_01_build_and_apply(self):
        r = self.run_cli("build")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)["watermark"], 0)
        r = self.run_cli(
            "apply", *[os.path.join("samples", "changes", os.path.basename(p))
                       for p in CHANGE_FILES])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout),
                         {"watermark": 3000, "applied": 3000, "skipped": 0})

    def test_02_dump_matches_final_snapshot(self):
        r = self.run_cli("dump")
        self.assertEqual(r.returncode, 0, r.stderr)
        exp = load_json(os.path.join(
            EXPECTED, "snapshots", "step-003000.json"))
        self.assertEqual(json.loads(r.stdout), exp)
        r = self.run_cli("dump", "--view", "v_status_totals")
        got = json.loads(r.stdout)
        self.assertEqual(len(got["views"]), 1)
        self.assertEqual(got["views"][0]["view"], "v_status_totals")

    def test_03_query_hit_miss_and_errors(self):
        r = self.run_cli("query", "--view", "v_customer_orders",
                         "--key", "C001")
        self.assertEqual(r.returncode, 0, r.stderr)
        got = json.loads(r.stdout)
        self.assertEqual(got["step"], 3000)
        self.assertEqual(got["row"]["key"], ["C001"])
        r = self.run_cli("query", "--view", "v_customer_orders",
                         "--key", "C004")
        self.assertIsNone(json.loads(r.stdout)["row"])
        self.assertEqual(self.run_cli(
            "query", "--view", "nope", "--key", "C001").returncode, 2)
        self.assertEqual(self.run_cli(
            "query", "--view", "v_region_channel",
            "--key", "south").returncode, 2)

    def test_04_apply_is_idempotent(self):
        r = self.run_cli(
            "apply", *[os.path.join("samples", "changes", os.path.basename(p))
                       for p in CHANGE_FILES])
        self.assertEqual(json.loads(r.stdout),
                         {"watermark": 3000, "applied": 0, "skipped": 3000})

    def test_05_log_tail_and_data_json(self):
        got = load_jsonl(os.path.join(self.tmp.name, "var", "log",
                                      "applied.jsonl"))
        exp = load_jsonl(os.path.join(EXPECTED, "log_tail.jsonl"))
        self.assertEqual(got, exp)

        data = load_json(os.path.join(self.tmp.name, "web", "data.json"))
        self.assertEqual(data["watermark"], 3000)
        self.assertEqual(len(data["checkpoints"]), 13)
        self.assertEqual(len(data["steps"]), 3000)
        final = load_json(os.path.join(
            EXPECTED, "snapshots", "step-003000.json"))
        for part, exp_part in zip(data["views"], final["views"]):
            self.assertEqual(part["view"], exp_part["view"])
            self.assertEqual(part["rows"], exp_part["rows"])
            self.assertEqual(part["vanished"], exp_part["vanished"])
            self.assertTrue(part["key_columns"])
            self.assertTrue(part["value_columns"])
        for cp in data["checkpoints"]:
            self.assertEqual(cp, load_json(os.path.join(
                EXPECTED, "snapshots", "step-%06d.json" % cp["step"])))
        exp_deltas = load_jsonl(os.path.join(EXPECTED, "deltas.jsonl"))
        for got_step, exp_step in zip(data["steps"], exp_deltas):
            self.assertEqual(got_step["step"], exp_step["step"])
            self.assertEqual(got_step["views"], exp_step["views"])
            self.assertIsInstance(got_step["ops"], int)

    def test_06_gap_fails_nonzero(self):
        bad = os.path.join(self.tmp.name, "bad.jsonl")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"step": 4000, "ops": []}) + "\n")
        r = self.run_cli("apply", bad)
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
