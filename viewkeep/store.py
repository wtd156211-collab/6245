"""Persistence: var/state.json, var/log/applied.jsonl, var/deltas.jsonl,
var/checkpoints/, and the web/data.json export."""

import json
import os
import shutil

from .engine import Engine, ViewState

LOG_WINDOW = 200
CHECKPOINT_EVERY = 250


def _dump_line(f, obj):
    f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    f.write("\n")


class Store:
    def __init__(self, root="."):
        self.root = root
        self.var_dir = os.path.join(root, "var")
        self.log_dir = os.path.join(self.var_dir, "log")
        self.ckpt_dir = os.path.join(self.var_dir, "checkpoints")
        self.state_path = os.path.join(self.var_dir, "state.json")
        self.log_path = os.path.join(self.log_dir, "applied.jsonl")
        self.deltas_path = os.path.join(self.var_dir, "deltas.jsonl")
        self.web_dir = os.path.join(root, "web")
        self.data_path = os.path.join(self.web_dir, "data.json")
        self.log_entries = []

    # ---------- lifecycle ----------

    def reset(self):
        if os.path.isdir(self.var_dir):
            shutil.rmtree(self.var_dir)
        os.makedirs(self.log_dir)
        os.makedirs(self.ckpt_dir)
        os.makedirs(self.web_dir, exist_ok=True)
        open(self.deltas_path, "w", encoding="utf-8").close()
        self.log_entries = []
        self._flush_log()

    def exists(self):
        return os.path.exists(self.state_path)

    def load_log_window(self):
        self.log_entries = []
        if os.path.exists(self.log_path):
            with open(self.log_path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            for line in lines[-LOG_WINDOW:]:
                if line:
                    self.log_entries.append(json.loads(line))

    # ---------- state ----------

    def save_state(self, engine):
        os.makedirs(self.var_dir, exist_ok=True)
        views = {}
        for name in sorted(engine.views):
            v = engine.views[name]
            d = v.defn
            groups = []
            for key in sorted(v.groups):
                count, states = v.groups[key]
                ser_states = []
                for (_, func, _), st in zip(d.aggregates, states):
                    if func == "count":
                        ser_states.append(None)
                    elif func == "sum":
                        ser_states.append(st)
                    else:
                        ser_states.append([[x, n] for x, n in sorted(st.items())])
                groups.append([list(key), count, ser_states])
            views[name] = {
                "groups": groups,
                "membership": [[pk, list(k)] for pk, k in sorted(v.membership.items())],
                "appeared": [[list(k), s] for k, s in sorted(v.appeared.items())],
                "vanished": [v.vanished[k] for k in sorted(v.vanished)],
            }
        fk_index = {
            vname: [[fk, sorted(pks)] for fk, pks in sorted(idx.items())]
            for vname, idx in sorted(engine.fk_index.items())
        }
        state = {
            "watermark": engine.watermark,
            "tables": {name: [tbl[pk] for pk in sorted(tbl)]
                       for name, tbl in sorted(engine.tables.items())},
            "views": views,
            "fk_index": fk_index,
        }
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(state, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, self.state_path)

    def load_state(self, schema, view_defs):
        with open(self.state_path, encoding="utf-8") as f:
            state = json.load(f)
        engine = Engine(schema, view_defs)
        engine.watermark = state["watermark"]
        for name, rows in state["tables"].items():
            pkcol = schema.pk[name]
            engine.tables[name] = {row[pkcol]: row for row in rows}
        for vname, idx in state["fk_index"].items():
            engine.fk_index[vname] = {fk: set(pks) for fk, pks in idx}
        for vname, vs in state["views"].items():
            d = engine.defs[vname]
            v = ViewState(d)
            for key_list, count, ser_states in vs["groups"]:
                states = []
                for (_, func, _), st in zip(d.aggregates, ser_states):
                    if func == "count":
                        states.append(None)
                    elif func == "sum":
                        states.append(st)
                    else:
                        states.append({x: n for x, n in st})
                v.groups[tuple(key_list)] = [count, states]
            v.membership = {pk: tuple(k) for pk, k in vs["membership"]}
            v.appeared = {tuple(k): s for k, s in vs["appeared"]}
            v.vanished = {tuple(e["key"]): e for e in vs["vanished"]}
            engine.views[vname] = v
        self.load_log_window()
        return engine

    # ---------- log / deltas / checkpoints ----------

    def append_log(self, entry):
        self.log_entries.append(entry)
        while len(self.log_entries) > LOG_WINDOW:
            self.log_entries.pop(0)

    def _flush_log(self):
        os.makedirs(self.log_dir, exist_ok=True)
        with open(self.log_path, "w", encoding="utf-8", newline="\n") as f:
            for e in self.log_entries:
                _dump_line(f, e)

    def append_delta(self, record):
        with open(self.deltas_path, "a", encoding="utf-8", newline="\n") as f:
            _dump_line(f, record)

    def write_checkpoint(self, engine):
        os.makedirs(self.ckpt_dir, exist_ok=True)
        path = os.path.join(self.ckpt_dir, f"step-{engine.watermark:06d}.json")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(engine.snapshot(), f, ensure_ascii=False, indent=2)
            f.write("\n")

    def flush(self, engine):
        """Persist everything at the end of build/apply."""
        self.save_state(engine)
        self._flush_log()
        self.export_data_json(engine)

    # ---------- web export ----------

    def export_data_json(self, engine):
        checkpoints = []
        if os.path.isdir(self.ckpt_dir):
            for fn in sorted(os.listdir(self.ckpt_dir)):
                if fn.startswith("step-") and fn.endswith(".json"):
                    with open(os.path.join(self.ckpt_dir, fn), encoding="utf-8") as f:
                        checkpoints.append(json.load(f))
        views = []
        if checkpoints:
            for v in checkpoints[-1]["views"]:
                d = engine.defs[v["view"]]
                views.append({
                    "view": v["view"],
                    "key_columns": d.key_columns,
                    "value_columns": d.value_columns,
                    "rows": v["rows"],
                    "vanished": v["vanished"],
                })
        steps = []
        if os.path.exists(self.deltas_path):
            with open(self.deltas_path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        steps.append(json.loads(line))
        data = {
            "watermark": engine.watermark,
            "views": views,
            "checkpoints": checkpoints,
            "steps": steps,
        }
        os.makedirs(self.web_dir, exist_ok=True)
        tmp = self.data_path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
            f.write("\n")
        os.replace(tmp, self.data_path)
