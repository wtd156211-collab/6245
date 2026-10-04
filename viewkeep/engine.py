"""Incremental materialized view maintenance engine.

Views are maintained per change step: a step touches only the source rows it
mentions (plus, for join views, the rows referencing a changed dimension row),
and each touched row's net effect (pre-step membership vs post-step membership)
is applied to its group(s) exactly once.
"""


class StepError(Exception):
    """A change step is invalid; the whole step is rejected (exit code 1)."""


def _check(op, x, v):
    if op == "=":
        return x == v
    if op == "!=":
        return x != v
    if op == "<":
        return x < v
    if op == "<=":
        return x <= v
    if op == ">":
        return x > v
    if op == ">=":
        return x >= v
    if op == "in":
        return x in v
    return x not in v  # not_in


def evaluate(defn, src_row, dim_row):
    """Return (group_key, agg_inputs) for a source row, or None if the row
    is filtered out or fails the inner join."""
    if defn.joined and dim_row is None:
        return None
    source_table = defn.source_table

    def get(t, c):
        return src_row[c] if t == source_table else dim_row[c]

    for t, c, op, v in defn.filters:
        if not _check(op, get(t, c), v):
            return None
    key = tuple(get(t, c) for t, c in defn.group_cols)
    inputs = tuple(
        None if func == "count" else get(*col)
        for (_, func, col) in defn.aggregates
    )
    return key, inputs


class ViewState:
    def __init__(self, defn):
        self.defn = defn
        self.groups = {}      # key tuple -> [count, [per-agg state]]
        self.membership = {}  # source pk -> key tuple
        self.appeared = {}    # key tuple -> step it (re)appeared
        self.vanished = {}    # key tuple -> vanished entry dict
        self._touched = {}    # per-step: source pk -> old (key, inputs) | None
        self._gfrom = {}      # per-step: key tuple -> pre-step values | None

    def group_values(self, key):
        count, states = self.groups[key]
        out = {}
        for (name, func, _), st in zip(self.defn.aggregates, states):
            if func == "count":
                out[name] = count
            elif func == "sum":
                out[name] = st
            elif func == "min":
                out[name] = min(st)
            else:
                out[name] = max(st)
        return out

    def _note_from(self, key):
        if key not in self._gfrom:
            self._gfrom[key] = self.group_values(key) if key in self.groups else None

    def _apply(self, g, inputs, sign):
        states = g[1]
        for i, ((_, func, _), x) in enumerate(zip(self.defn.aggregates, inputs)):
            if func == "count":
                continue
            if func == "sum":
                states[i] = states[i] + sign * x
            else:
                bag = states[i]
                if sign > 0:
                    bag[x] = bag.get(x, 0) + 1
                else:
                    n = bag[x] - 1
                    if n:
                        bag[x] = n
                    else:
                        del bag[x]

    def add(self, pk, key, inputs):
        self._note_from(key)
        g = self.groups.get(key)
        if g is None:
            g = [0, [None if f == "count" else (0 if f == "sum" else {})
                     for (_, f, _) in self.defn.aggregates]]
            self.groups[key] = g
        g[0] += 1
        self._apply(g, inputs, +1)
        self.membership[pk] = key

    def remove(self, pk, key, inputs):
        self._note_from(key)
        g = self.groups[key]
        g[0] -= 1
        self._apply(g, inputs, -1)
        if g[0] == 0:
            del self.groups[key]
        self.membership.pop(pk, None)


class Engine:
    def __init__(self, schema, view_defs):
        self.schema = schema
        self.defs = {d.name: d for d in view_defs}
        self.tables = {name: {} for name in schema.tables}
        self.views = {d.name: ViewState(d) for d in view_defs}
        self.by_source = {}
        self.by_join = {}
        for d in view_defs:
            self.by_source.setdefault(d.source_table, []).append(d.name)
            if d.joined:
                self.by_join.setdefault(d.join_table, []).append(d.name)
        self.fk_index = {d.name: {} for d in view_defs if d.joined}
        self.watermark = 0

    # ---------- build ----------

    def build(self, tables):
        for name, rows in tables.items():
            self.schema.require_table(name)
            pkcol = self.schema.pk[name]
            tbl = self.tables[name]
            for row in rows:
                tbl[row[pkcol]] = row
        for vname, idx in self.fk_index.items():
            d = self.defs[vname]
            for pk, row in self.tables[d.source_table].items():
                idx.setdefault(row[d.join_left], set()).add(pk)
        for vname, view in self.views.items():
            d = view.defn
            dimtbl = self.tables[d.join_table] if d.joined else None
            for pk, row in self.tables[d.source_table].items():
                dim = dimtbl.get(row[d.join_left]) if d.joined else None
                got = evaluate(d, row, dim)
                if got is None:
                    continue
                key, inputs = got
                g = view.groups.get(key)
                if g is None:
                    g = [0, [None if f == "count" else (0 if f == "sum" else {})
                             for (_, f, _) in d.aggregates]]
                    view.groups[key] = g
                g[0] += 1
                view._apply(g, inputs, +1)
                view.membership[pk] = key
            for key in view.groups:
                view.appeared[key] = 0
        self.watermark = 0

    # ---------- step application ----------

    def apply_step(self, step, ops):
        """Apply one change step. Returns {view: delta} for changed views,
        or None if the step was already applied (idempotent skip)."""
        if step <= self.watermark:
            return None
        if step != self.watermark + 1:
            raise StepError(
                f"步号断裂: 当前水位 {self.watermark}, 收到 step={step}")
        self._validate(ops)
        for view in self.views.values():
            view._touched = {}
            view._gfrom = {}
        for op in ops:
            self._apply_op(op)
        deltas = {}
        for name in sorted(self.views):
            delta = self._finish_view(self.views[name], step)
            if delta is not None:
                deltas[name] = delta
        self.watermark = step
        return deltas

    def _validate(self, ops):
        pks = {name: set(tbl) for name, tbl in self.tables.items()}
        for i, op in enumerate(ops):
            kind = op.get("op")
            tname = op.get("table")
            if tname not in self.tables:
                raise StepError(f"op#{i}: 未知表 {tname!r}")
            pkcol = self.schema.pk[tname]
            if kind == "insert":
                row = op.get("row")
                if not isinstance(row, dict) or pkcol not in row:
                    raise StepError(f"op#{i}: insert 缺少主键列 {pkcol}")
                missing = self.schema.tables[tname] - set(row)
                if missing:
                    raise StepError(f"op#{i}: insert 缺少列 {sorted(missing)}")
                if row[pkcol] in pks[tname]:
                    raise StepError(f"op#{i}: 主键已存在 {row[pkcol]!r}")
                pks[tname].add(row[pkcol])
            elif kind in ("update", "delete"):
                key = op.get("key")
                if key not in pks[tname]:
                    raise StepError(f"op#{i}: 主键不存在 {key!r}")
                if kind == "update":
                    setmap = op.get("set")
                    if not isinstance(setmap, dict) or not setmap:
                        raise StepError(f"op#{i}: update 需要非空 set")
                    if pkcol in setmap:
                        raise StepError(f"op#{i}: set 不能含主键 {pkcol}")
                    unknown = set(setmap) - self.schema.tables[tname]
                    if unknown:
                        raise StepError(f"op#{i}: set 含未知列 {sorted(unknown)}")
                else:
                    pks[tname].discard(key)
            else:
                raise StepError(f"op#{i}: 未知操作 {kind!r}")

    def _touch(self, vname, pk):
        view = self.views[vname]
        if pk in view._touched:
            return
        d = view.defn
        src = self.tables[d.source_table].get(pk)
        if src is None:
            view._touched[pk] = None
            return
        dim = self.tables[d.join_table].get(src[d.join_left]) if d.joined else None
        view._touched[pk] = evaluate(d, src, dim)

    def _touch_dim_refs(self, vname, join_value):
        for src_pk in self.fk_index[vname].get(join_value, ()):
            self._touch(vname, src_pk)

    def _apply_op(self, op):
        tname = op["table"]
        tbl = self.tables[tname]
        pkcol = self.schema.pk[tname]
        kind = op["op"]

        if kind == "insert":
            row = op["row"]
            pk = row[pkcol]
            for vname in self.by_source.get(tname, ()):
                self._touch(vname, pk)
            for vname in self.by_join.get(tname, ()):
                self._touch_dim_refs(vname, row[self.defs[vname].join_right])
            tbl[pk] = row
            for vname in self.by_source.get(tname, ()):
                d = self.defs[vname]
                if d.joined:
                    self.fk_index[vname].setdefault(row[d.join_left], set()).add(pk)
            return

        pk = op["key"]
        if kind == "update":
            setmap = op["set"]
            old = tbl[pk]
            for vname in self.by_source.get(tname, ()):
                self._touch(vname, pk)
            for vname in self.by_join.get(tname, ()):
                d = self.defs[vname]
                self._touch_dim_refs(vname, old[d.join_right])
                if d.join_right in setmap:
                    self._touch_dim_refs(vname, setmap[d.join_right])
            new = dict(old)
            new.update(setmap)
            tbl[pk] = new
            for vname in self.by_source.get(tname, ()):
                d = self.defs[vname]
                if d.joined and d.join_left in setmap:
                    idx = self.fk_index[vname]
                    idx[old[d.join_left]].discard(pk)
                    idx.setdefault(new[d.join_left], set()).add(pk)
            return

        # delete
        old = tbl[pk]
        for vname in self.by_source.get(tname, ()):
            self._touch(vname, pk)
        for vname in self.by_join.get(tname, ()):
            self._touch_dim_refs(vname, old[self.defs[vname].join_right])
        del tbl[pk]
        for vname in self.by_source.get(tname, ()):
            d = self.defs[vname]
            if d.joined:
                self.fk_index[vname][old[d.join_left]].discard(pk)

    def _finish_view(self, view, step):
        d = view.defn
        tbl = self.tables[d.source_table]
        dimtbl = self.tables[d.join_table] if d.joined else None
        for pk, old in view._touched.items():
            src = tbl.get(pk)
            new = None
            if src is not None:
                dim = dimtbl.get(src[d.join_left]) if d.joined else None
                new = evaluate(d, src, dim)
            if old is not None:
                view.remove(pk, old[0], old[1])
            if new is not None:
                view.add(pk, new[0], new[1])

        inserted, updated, deleted = [], [], []
        for key, from_vals in view._gfrom.items():
            to_vals = view.group_values(key) if key in view.groups else None
            if from_vals is None and to_vals is None:
                continue
            klist = list(key)
            if from_vals is None:
                view.appeared[key] = step
                view.vanished.pop(key, None)
                inserted.append({"key": klist, "values": to_vals})
            elif to_vals is None:
                view.vanished[key] = {
                    "key": klist,
                    "appeared_step": view.appeared.pop(key),
                    "vanished_step": step,
                    "last_values": from_vals,
                }
                deleted.append({"key": klist, "values": from_vals})
            elif from_vals != to_vals:
                updated.append({"key": klist, "from": from_vals, "to": to_vals})
        if not (inserted or updated or deleted):
            return None
        inserted.sort(key=lambda r: r["key"])
        updated.sort(key=lambda r: r["key"])
        deleted.sort(key=lambda r: r["key"])
        return {"inserted": inserted, "updated": updated, "deleted": deleted}

    # ---------- reads ----------

    def query(self, view_name, key):
        view = self.views[view_name]
        tkey = tuple(key)
        if tkey not in view.groups:
            return None
        return {"key": list(tkey), "values": view.group_values(tkey)}

    def snapshot_view(self, view):
        rows = [{"key": list(k), "values": view.group_values(k)}
                for k in sorted(view.groups)]
        vanished = [view.vanished[k] for k in sorted(view.vanished)]
        return {"view": view.defn.name, "rows": rows, "vanished": vanished}

    def snapshot(self):
        return {"step": self.watermark,
                "views": [self.snapshot_view(self.views[n]) for n in sorted(self.views)]}
