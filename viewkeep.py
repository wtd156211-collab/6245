"""viewkeep: 物化视图增量维护引擎,只用标准库。

支持范围见 README:单表或主表+一张维表的等值内连接、AND 过滤、
按列分组上的 count/sum/min/max。引擎只维护被变更波及的分组,
不做每步全量重算。
"""

import json
import os
from collections import Counter

CHECKPOINT_EVERY = 250
LOG_WINDOW = 200


class StepError(Exception):
    """整步失败:操作校验不通过或水位不连续。"""


def _read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _write_json(path, obj):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, ensure_ascii=False, sort_keys=True,
                  separators=(",", ":"))
        fh.write("\n")


def _compare(op, a, b):
    if op == "=":
        return a == b
    if op == "!=":
        return a != b
    if op == "<":
        return a < b
    if op == "<=":
        return a <= b
    if op == ">":
        return a > b
    if op == ">=":
        return a >= b
    if op == "in":
        return a in b
    if op == "not_in":
        return a not in b
    raise StepError("未知谓词: %r" % (op,))


def _split_col(ref):
    table, column = ref.split(".", 1)
    return table, column


class GroupState:
    """单个分组的聚合状态:count/sum 直接维护,min/max 用 Counter。

    普通增删 O(1);仅当当前极值值被删且该值在组内不再存在时才
    扫描 Counter 的键,因此单步开销只与该步涉及的行/分组有关。
    """

    __slots__ = ("count", "sums", "extr")

    def __init__(self, aggs):
        self.count = 0
        self.sums = {}
        self.extr = {}
        for agg in aggs:
            func = agg["func"]
            if func == "sum":
                self.sums[agg["name"]] = 0
            elif func in ("min", "max"):
                self.extr[agg["name"]] = [Counter(), None, func == "min"]

    def add(self, aggs, contrib):
        self.count += 1
        for agg, value in zip(aggs, contrib):
            func = agg["func"]
            if func == "sum":
                self.sums[agg["name"]] += value
            elif func in ("min", "max"):
                entry = self.extr[agg["name"]]
                entry[0][value] += 1
                cur = entry[1]
                if cur is None or (value < cur if entry[2] else value > cur):
                    entry[1] = value

    def remove(self, aggs, contrib):
        self.count -= 1
        for agg, value in zip(aggs, contrib):
            func = agg["func"]
            if func == "sum":
                self.sums[agg["name"]] -= value
            elif func in ("min", "max"):
                entry = self.extr[agg["name"]]
                counter = entry[0]
                counter[value] -= 1
                if counter[value] == 0:
                    del counter[value]
                    if entry[1] == value:
                        pick = min if entry[2] else max
                        entry[1] = pick(counter) if counter else None


class View:
    """一个视图的全部状态:分组聚合、成员归属、连接索引、出现/消失记录。"""

    def __init__(self, defn):
        self.defn = defn
        self.name = defn["name"]
        source = defn["source"]
        self.table = source["table"]
        join = source.get("join")
        self.dim_table = join["table"] if join else None
        if join:
            self.join_left = _split_col(join["on"][0])[1]
            self.join_right = _split_col(join["on"][1])[1]
        else:
            self.join_left = self.join_right = None
        self.filters = [
            (_split_col(f["column"]), f["op"], f["value"])
            for f in defn.get("filter", [])
        ]
        self.group_cols = [_split_col(c) for c in defn["group_by"]]
        self.aggs = defn["aggregates"]
        self.agg_cols = [
            _split_col(a["column"]) if a.get("column") else None
            for a in self.aggs
        ]
        # key 元组 -> GroupState
        self.groups = {}
        # 主表主键 -> (key, contrib)
        self.member = {}
        # 维表连接列值 -> set(主表主键)
        self.join_index = {}
        # 维表连接列值 -> 维表主键
        self.dim_index = {}
        # 现存分组 key -> 出现步号;已消失分组 key -> 快照记录
        self.appeared = {}
        self.vanished = {}

    def full_compute(self, tables):
        """按当前源表整体建立一次索引/成员/分组(build 或重启后用)。"""
        self.groups = {}
        self.member = {}
        self.join_index = {}
        self.dim_index = {}
        dim_rows = tables.get(self.dim_table) if self.dim_table else None
        if self.dim_table:
            for dim_pk, dim_row in dim_rows.items():
                self.dim_index[dim_row[self.join_right]] = dim_pk
        for pk, main_row in tables[self.table].items():
            if self.dim_table:
                bucket = self.join_index.setdefault(
                    main_row[self.join_left], set())
                bucket.add(pk)
            dim_row = self._dim_row(main_row, dim_rows)
            result = self.evaluate(main_row, dim_row)
            if result is not None:
                self._put(pk, result)

    def _dim_row(self, main_row, dim_rows):
        if not self.dim_table:
            return None
        dim_pk = self.dim_index.get(main_row[self.join_left])
        return dim_rows.get(dim_pk) if dim_pk is not None else None

    def evaluate(self, main_row, dim_row):
        """连接 + 过滤 + 投影,返回 (key 元组, contrib 元组) 或 None。"""
        if self.dim_table and dim_row is None:
            return None
        namespace = {self.table: main_row}
        if self.dim_table:
            namespace[self.dim_table] = dim_row
        for (table, column), op, value in self.filters:
            if not _compare(op, namespace[table][column], value):
                return None
        key = tuple(namespace[t][c] for t, c in self.group_cols)
        contrib = tuple(
            None if col is None else namespace[col[0]][col[1]]
            for col in self.agg_cols
        )
        return key, contrib

    def _put(self, pk, result):
        key, contrib = result
        group = self.groups.get(key)
        if group is None:
            group = GroupState(self.aggs)
            self.groups[key] = group
        group.add(self.aggs, contrib)
        self.member[pk] = result

    def values_of(self, key):
        group = self.groups.get(key)
        if group is None:
            return None
        values = {}
        for agg in self.aggs:
            func = agg["func"]
            name = agg["name"]
            if func == "count":
                values[name] = group.count
            elif func == "sum":
                values[name] = group.sums[name]
            else:
                values[name] = group.extr[name][1]
        return values

    def maintain(self, tables, changes, step):
        """应用一步里与本视图相关的表变更,返回三类净变化或 None。"""
        main_changes = changes.get(self.table)
        dim_changes = changes.get(self.dim_table) if self.dim_table else None
        if not main_changes and not dim_changes:
            return None

        if dim_changes:
            for _pk, old, new in dim_changes:
                if old is not None:
                    self.dim_index.pop(old[self.join_right], None)
                if new is not None:
                    self.dim_index[new[self.join_right]] = _pk

        affected = set()
        if self.dim_table and main_changes:
            for pk, old, new in main_changes:
                if old is not None:
                    bucket = self.join_index.get(old[self.join_left])
                    if bucket is not None:
                        bucket.discard(pk)
                if new is not None:
                    self.join_index.setdefault(
                        new[self.join_left], set()).add(pk)
        if main_changes:
            affected.update(pk for pk, _o, _n in main_changes)
        if dim_changes:
            for _pk, old, new in dim_changes:
                for row in (old, new):
                    if row is not None:
                        bucket = self.join_index.get(row[self.join_right])
                        if bucket is not None:
                            affected.update(bucket)
        if not affected:
            return None

        touched = {}

        def touch(key):
            if key not in touched:
                touched[key] = self.values_of(key)

        dim_rows = tables.get(self.dim_table) if self.dim_table else None
        main_rows = tables[self.table]
        for pk in sorted(affected):
            old_member = self.member.pop(pk, None)
            if old_member is not None:
                key, contrib = old_member
                touch(key)
                group = self.groups[key]
                group.remove(self.aggs, contrib)
                if group.count == 0:
                    del self.groups[key]
            main_row = main_rows.get(pk)
            if main_row is not None:
                dim_row = self._dim_row(main_row, dim_rows)
                result = self.evaluate(main_row, dim_row)
                if result is not None:
                    key = result[0]
                    touch(key)
                    self._put(pk, result)

        inserted = []
        updated = []
        deleted = []
        for key, before in touched.items():
            after = self.values_of(key)
            if before is None and after is None:
                continue
            key_list = list(key)
            if before is None:
                inserted.append({"key": key_list, "values": after})
                self.appeared[key] = step
                self.vanished.pop(key, None)
            elif after is None:
                deleted.append({"key": key_list, "values": before})
                self.vanished[key] = {
                    "key": key_list,
                    "appeared_step": self.appeared.pop(key),
                    "vanished_step": step,
                    "last_values": before,
                }
            elif before != after:
                updated.append({
                    "key": key_list, "from": before, "to": after,
                })

        if not (inserted or updated or deleted):
            return None
        return {
            "inserted": sorted(inserted, key=lambda r: r["key"]),
            "updated": sorted(updated, key=lambda r: r["key"]),
            "deleted": sorted(deleted, key=lambda r: r["key"]),
        }

    def snapshot_part(self):
        rows = [
            {"key": list(key), "values": self.values_of(key)}
            for key in sorted(self.groups)
        ]
        vanished = [self.vanished[key] for key in sorted(self.vanished)]
        return {"view": self.name, "rows": rows, "vanished": vanished}

    def row_for(self, key_tuple):
        values = self.values_of(key_tuple)
        if values is None:
            return None
        return {"key": list(key_tuple), "values": values}

class Engine:
    """表数据 + 多个视图 + 水位/日志/检查点,支持持久化续跑。"""

    def __init__(self, schema, view_defns):
        self.schema = schema
        self.primary_key = {}
        self.col_type = {}
        for table in schema["tables"]:
            self.primary_key[table["name"]] = table["primary_key"][0]
            for column in table["columns"]:
                self.col_type[(table["name"], column["name"])] = column["type"]
        self.tables = {t["name"]: {} for t in schema["tables"]}
        self.views = [View(d) for d in view_defns]
        self.view_by_name = {v.name: v for v in self.views}
        self.watermark = 0
        self.log = []
        self.deltas = []
        self.checkpoints = []

    @classmethod
    def build(cls, init_dir):
        schema = _read_json(os.path.join(init_dir, "schema.json"))
        view_defns = _read_json(os.path.join(init_dir, "views.json"))["views"]
        init_tables = _read_json(os.path.join(init_dir, "tables.json"))
        engine = cls(schema, view_defns)
        for name, rows in init_tables.items():
            pk_col = engine.primary_key[name]
            engine.tables[name] = {r[pk_col]: dict(r) for r in rows}
        for view in engine.views:
            view.full_compute(engine.tables)
            for key in view.groups:
                view.appeared[key] = 0
        engine.checkpoints.append(engine.snapshot())
        return engine

    @classmethod
    def load(cls, init_dir, var_dir):
        schema = _read_json(os.path.join(init_dir, "schema.json"))
        view_defns = _read_json(os.path.join(init_dir, "views.json"))["views"]
        state = _read_json(os.path.join(var_dir, "state.json"))
        engine = cls(schema, view_defns)
        engine.tables = state["tables"]
        engine.watermark = state["watermark"]
        for view in engine.views:
            view.full_compute(engine.tables)
            saved = state["views"][view.name]
            view.appeared = {tuple(k): step for k, step in saved["appeared"]}
            view.vanished = {
                tuple(item["key"]): item for item in saved["vanished"]
            }
        deltas_path = os.path.join(var_dir, "deltas.jsonl")
        if os.path.exists(deltas_path):
            with open(deltas_path, encoding="utf-8") as fh:
                engine.deltas = [json.loads(line) for line in fh if line.strip()]
        cp_path = os.path.join(var_dir, "checkpoints.json")
        if os.path.exists(cp_path):
            engine.checkpoints = _read_json(cp_path)
        log_path = os.path.join(var_dir, "log", "applied.jsonl")
        if os.path.exists(log_path):
            with open(log_path, encoding="utf-8") as fh:
                engine.log = [json.loads(line) for line in fh if line.strip()]
        return engine

    def save(self, var_dir):
        os.makedirs(os.path.join(var_dir, "log"), exist_ok=True)
        state = {"watermark": self.watermark, "tables": self.tables, "views": {}}
        for view in self.views:
            state["views"][view.name] = {
                "appeared": [
                    [list(key), step]
                    for key, step in sorted(view.appeared.items())
                ],
                "vanished": [
                    view.vanished[key] for key in sorted(view.vanished)
                ],
            }
        _write_json(os.path.join(var_dir, "state.json"), state)
        with open(os.path.join(var_dir, "deltas.jsonl"), "w",
                  encoding="utf-8", newline="\n") as fh:
            for record in self.deltas:
                fh.write(json.dumps(record, ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")))
                fh.write("\n")
        _write_json(os.path.join(var_dir, "checkpoints.json"), self.checkpoints)
        with open(os.path.join(var_dir, "log", "applied.jsonl"), "w",
                  encoding="utf-8", newline="\n") as fh:
            for record in self.log:
                fh.write(json.dumps(record, ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":")))
                fh.write("\n")

    def apply_step(self, step, ops):
        """应用一步变更;返回该步 delta 记录,跳号返回 None。"""
        if step <= self.watermark:
            return None
        if step != self.watermark + 1:
            raise StepError(
                "step %d 不连续,当前 watermark=%d" % (step, self.watermark))

        changes = {name: [] for name in self.tables}
        undo = []
        try:
            for op in ops:
                self._apply_op(op, changes, undo)
        except StepError:
            for rollback in reversed(undo):
                rollback()
            raise

        step_views = {}
        for view in self.views:
            delta = view.maintain(self.tables, changes, step)
            if delta is not None:
                step_views[view.name] = delta

        self.watermark = step
        changed_rows = {}
        for name in sorted(step_views):
            count = sum(len(rows) for rows in step_views[name].values())
            if count:
                changed_rows[name] = count
        record = {"step": step, "ops": len(ops), "views": step_views}
        self.deltas.append(record)
        self.log.append({
            "watermark": step,
            "ops": len(ops),
            "changed_rows": changed_rows,
        })
        if len(self.log) > LOG_WINDOW:
            del self.log[:-LOG_WINDOW]
        if step % CHECKPOINT_EVERY == 0:
            self.checkpoints.append(self.snapshot())
        return record

    def _apply_op(self, op, changes, undo):
        kind = op["op"]
        table_name = op["table"]
        if table_name not in self.tables:
            raise StepError("未知表: %r" % (table_name,))
        table = self.tables[table_name]
        pk_col = self.primary_key[table_name]
        if kind == "insert":
            row = dict(op["row"])
            pk = row[pk_col]
            if pk in table:
                raise StepError(
                    "insert 主键已存在: %s.%s=%r" % (table_name, pk_col, pk))
            table[pk] = row
            undo.append(lambda: table.pop(pk, None))
            changes[table_name].append((pk, None, row))
        elif kind == "delete":
            pk = op["key"]
            old = table.get(pk)
            if old is None:
                raise StepError(
                    "delete 主键不存在: %s.%s=%r" % (table_name, pk_col, pk))
            del table[pk]
            undo.append(lambda: table.__setitem__(pk, old))
            changes[table_name].append((pk, old, None))
        elif kind == "update":
            pk = op["key"]
            assignments = op["set"]
            if pk_col in assignments:
                raise StepError("update 的 set 不能含主键 %s" % (pk_col,))
            old = table.get(pk)
            if old is None:
                raise StepError(
                    "update 主键不存在: %s.%s=%r" % (table_name, pk_col, pk))
            new = dict(old)
            new.update(assignments)
            table[pk] = new
            undo.append(lambda: table.__setitem__(pk, old))
            changes[table_name].append((pk, old, new))
        else:
            raise StepError("未知操作类型: %r" % (kind,))

    def snapshot(self):
        return {
            "step": self.watermark,
            "views": [
                v.snapshot_part()
                for v in sorted(self.views, key=lambda view: view.name)
            ],
        }

    def query_row(self, view_name, key_values):
        view = self.view_by_name.get(view_name)
        if view is None:
            return None
        return view.row_for(tuple(key_values))

    def export_web(self, web_dir):
        os.makedirs(web_dir, exist_ok=True)
        latest = self.checkpoints[-1]
        views = []
        for part in latest["views"]:
            view = self.view_by_name[part["view"]]
            views.append({
                "view": view.name,
                "key_columns": [c for _t, c in view.group_cols],
                "value_columns": [a["name"] for a in view.aggs],
                "rows": part["rows"],
                "vanished": part["vanished"],
            })
        data = {
            "watermark": self.watermark,
            "views": views,
            "checkpoints": self.checkpoints,
            "steps": self.deltas,
        }
        _write_json(os.path.join(web_dir, "data.json"), data)
