"""Schema and materialized view definition loading/validation."""

import json

FILTER_OPS = {"=", "!=", "<", "<=", ">", ">=", "in", "not_in"}
AGG_FUNCS = {"count", "sum", "min", "max"}
TYPES = {"str", "int"}


class DefError(Exception):
    """Invalid material or view definition (exit code 3)."""


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def split_ref(ref):
    parts = ref.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise DefError(f"列引用必须是 table.column: {ref!r}")
    return parts[0], parts[1]


class Schema:
    def __init__(self, raw):
        self.tables = {}
        self.pk = {}
        self.coltypes = {}
        for t in raw.get("tables", []):
            name = t["name"]
            pks = t.get("primary_key", [])
            if len(pks) != 1:
                raise DefError(f"表 {name} 必须有且仅有一个主键列")
            cols = {c["name"]: c["type"] for c in t["columns"]}
            if pks[0] not in cols:
                raise DefError(f"表 {name} 的主键列 {pks[0]} 不存在")
            for typ in cols.values():
                if typ not in TYPES:
                    raise DefError(f"表 {name} 存在不支持的类型 {typ!r}")
            self.tables[name] = set(cols)
            self.pk[name] = pks[0]
            self.coltypes[name] = cols

    def require_table(self, name):
        if name not in self.tables:
            raise DefError(f"未知表 {name!r}")

    def require_column(self, table, column):
        self.require_table(table)
        if column not in self.tables[table]:
            raise DefError(f"表 {table} 没有列 {column!r}")


class ViewDef:
    """A compiled view definition: join -> filter -> group by -> aggregate."""

    def __init__(self, raw, schema):
        self.name = raw.get("name")
        if not self.name:
            raise DefError("视图缺少 name")
        source = raw.get("source") or {}
        self.source_table = source.get("table")
        schema.require_column(self.source_table, schema.pk[self.source_table])

        join = source.get("join")
        self.join_table = None
        self.join_left = None
        self.join_right = None
        if join:
            self.join_table = join.get("table")
            on = join.get("on") or []
            if len(on) != 2:
                raise DefError(f"视图 {self.name}: 连接需要左右两列")
            lt, lc = split_ref(on[0])
            rt, rc = split_ref(on[1])
            if {lt, rt} != {self.source_table, self.join_table}:
                raise DefError(f"视图 {self.name}: 连接列必须来自主表与维表")
            self.join_left = lc if lt == self.source_table else rc
            self.join_right = rc if rt == self.join_table else lc
            if self.join_left == schema.pk[self.source_table]:
                raise DefError(f"视图 {self.name}: 不支持以主键为外键的连接方向")
            schema.require_column(self.source_table, self.join_left)
            schema.require_column(self.join_table, self.join_right)

        self.filters = self._compile_predicates(raw.get("filter", []), schema)
        group_by = raw.get("group_by") or []
        if not group_by:
            raise DefError(f"视图 {self.name}: group_by 至少一列")
        self.group_cols = []
        for ref in group_by:
            t, c = split_ref(ref)
            schema.require_column(t, c)
            self.group_cols.append((t, c))

        aggs = raw.get("aggregates") or []
        if not aggs:
            raise DefError(f"视图 {self.name}: 至少一个聚合")
        self.aggregates = []
        seen = set()
        for a in aggs:
            aname, func = a.get("name"), a.get("func")
            if not aname or aname in seen:
                raise DefError(f"视图 {self.name}: 聚合名缺失或重复 {aname!r}")
            seen.add(aname)
            if func not in AGG_FUNCS:
                raise DefError(f"视图 {self.name}: 不支持的聚合 {func!r}")
            col = None
            if func != "count":
                if not a.get("column"):
                    raise DefError(f"视图 {self.name}: {aname} 缺少 column")
                col = split_ref(a["column"])
                schema.require_column(*col)
            elif "column" in a:
                raise DefError(f"视图 {self.name}: count 不允许 column")
            self.aggregates.append((aname, func, col))

        self.key_columns = [c for _, c in self.group_cols]
        self.value_columns = [a[0] for a in self.aggregates]

    def _compile_predicates(self, preds, schema):
        out = []
        for p in preds or []:
            t, c = split_ref(p["column"])
            schema.require_column(t, c)
            op = p["op"]
            if op not in FILTER_OPS:
                raise DefError(f"视图 {self.name}: 不支持的谓词 {op!r}")
            value = p["value"]
            if op in ("in", "not_in"):
                if not isinstance(value, list):
                    raise DefError(f"视图 {self.name}: {op} 需要列表值")
            out.append((t, c, op, value))
        return out

    @property
    def joined(self):
        return self.join_table is not None

    def tables_involved(self):
        return {self.source_table, self.join_table} if self.joined else {self.source_table}
