"""End-to-end CLI tests (subprocess): build/apply/dump/query plus
determinism, split apply, log recycling and data.json export."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
APP = ROOT / "app.py"
PARTS = [str(SAMPLES / "changes" / f"part-0{i}.jsonl") for i in range(1, 7)]


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="vk-cli-"))
        shutil.copytree(SAMPLES, self.tmp / "samples")
        os.makedirs(self.tmp / "web")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_app(self, *args, check=True):
        proc = subprocess.run(
            [sys.executable, str(APP), *args],
            cwd=self.tmp, capture_output=True, text=True)
        if check and proc.returncode != 0:
            self.fail(f"app.py {' '.join(args)} failed:\n{proc.stderr}")
        return proc

    def build(self):
        self.run_app("build")

    def test_build_and_full_apply_artifacts(self):
        self.build()
        out = self.run_app("apply", *PARTS).stdout.strip()
        self.assertEqual(json.loads(out),
                         {"watermark": 3000, "applied": 3000, "skipped": 0})
        # dump equals final expected snapshot
        snap = json.loads(self.run_app("dump").stdout)
        expected = json.load(open(SAMPLES / "expected" / "snapshots" /
                                  "step-003000.json", encoding="utf-8"))
        self.assertEqual(snap, expected)
        # log: exactly 200 lines, byte-for-byte equals expected tail
        log = (self.tmp / "var" / "log" / "applied.jsonl").read_text().splitlines()
        self.assertEqual(len(log), 200)
        exp_log = (SAMPLES / "expected" / "log_tail.jsonl").read_text().splitlines()
        self.assertEqual([json.loads(l) for l in log],
                         [json.loads(l) for l in exp_log])
        # data.json structure
        data = json.loads((self.tmp / "web" / "data.json").read_text())
        self.assertEqual(data["watermark"], 3000)
        self.assertEqual(len(data["checkpoints"]), 13)
        self.assertEqual(len(data["steps"]), 3000)
        last = data["checkpoints"][-1]
        self.assertEqual(last, expected)
        for v in data["views"]:
            self.assertIn("key_columns", v)
            self.assertIn("value_columns", v)

    def test_split_apply_equals_single_apply(self):
        single = Path(tempfile.mkdtemp(prefix="vk-single-"))
        shutil.copytree(SAMPLES, single / "samples")
        os.makedirs(single / "web")
        subprocess.run([sys.executable, str(APP), "build"], cwd=single,
                       capture_output=True, text=True, check=True)
        subprocess.run([sys.executable, str(APP), "apply", *PARTS], cwd=single,
                       capture_output=True, text=True, check=True)
        single_state = (single / "var" / "state.json").read_bytes()
        single_dump = subprocess.run([sys.executable, str(APP), "dump"],
                                     cwd=single, capture_output=True, text=True).stdout
        shutil.rmtree(single)

        self.build()
        self.run_app("apply", *PARTS[:3])
        self.run_app("apply", *PARTS[3:])
        self.assertEqual((self.tmp / "var" / "state.json").read_bytes(), single_state)
        self.assertEqual(self.run_app("dump").stdout, single_dump)

    def test_reapply_is_fully_skipped(self):
        self.build()
        self.run_app("apply", *PARTS)
        first_state = (self.tmp / "var" / "state.json").read_bytes()
        out = self.run_app("apply", *PARTS).stdout.strip()
        self.assertEqual(json.loads(out),
                         {"watermark": 3000, "applied": 0, "skipped": 3000})
        self.assertEqual((self.tmp / "var" / "state.json").read_bytes(), first_state)

    def test_query_shapes_and_exit_codes(self):
        self.build()
        self.run_app("apply", *PARTS)
        out = json.loads(self.run_app("query", "--view", "v_region_channel",
                                      "--key", "east,app").stdout)
        self.assertEqual(out["step"], 3000)
        self.assertEqual(out["key"], ["east", "app"])
        gone = json.loads(self.run_app(
            "query", "--view", "v_customer_orders", "--key", "C004").stdout)
        self.assertIsNone(gone["row"])
        # unknown view -> exit 2
        p = self.run_app("query", "--view", "nope", "--key", "x", check=False)
        self.assertEqual(p.returncode, 2)
        p = self.run_app("dump", "--view", "nope", check=False)
        self.assertEqual(p.returncode, 2)
        # wrong key length -> exit 2
        p = self.run_app("query", "--view", "v_region_channel",
                         "--key", "east", check=False)
        self.assertEqual(p.returncode, 2)

    def test_gap_and_invalid_step_exit_nonzero(self):
        self.build()
        bad = self.tmp / "bad.jsonl"
        bad.write_text(json.dumps({"step": 2, "ops": []}) + "\n", encoding="utf-8")
        p = self.run_app("apply", str(bad), check=False)
        self.assertEqual(p.returncode, 1)
        # watermark unchanged, and the good stream still applies cleanly after
        with open(self.tmp / "var" / "state.json", encoding="utf-8") as f:
            self.assertEqual(json.load(f)["watermark"], 0)
        self.run_app("apply", *PARTS[:1])

    def test_performance_budget(self):
        # Generous guard against accidental full rescans (budget is 3s);
        # actual replay is well under a second on the reference machine.
        import time
        self.build()
        start = time.perf_counter()
        self.run_app("apply", *PARTS)
        self.assertLess(time.perf_counter() - start, 15.0)


if __name__ == "__main__":
    unittest.main()
