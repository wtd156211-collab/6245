"""Command line interface: build / apply / dump / query / serve."""

import argparse
import functools
import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from .defs import DefError, Schema, ViewDef, load_json
from .engine import Engine, StepError
from .store import CHECKPOINT_EVERY, Store

EXIT_STEP_ERROR = 1
EXIT_USAGE = 2
EXIT_DEF_ERROR = 3

INIT_DIR = "samples/init"


def _load_defs():
    schema = Schema(load_json(f"{INIT_DIR}/schema.json"))
    raw_views = load_json(f"{INIT_DIR}/views.json")
    view_defs = [ViewDef(v, schema) for v in raw_views.get("views", [])]
    return schema, view_defs


def _load_engine(store):
    schema, view_defs = _load_defs()
    return schema, view_defs, store.load_state(schema, view_defs)


def cmd_build(store, _args):
    schema, view_defs = _load_defs()
    tables = load_json(f"{INIT_DIR}/tables.json")
    store.reset()
    engine = Engine(schema, view_defs)
    engine.build(tables)
    store.write_checkpoint(engine)
    store.flush(engine)
    print(json.dumps({"watermark": 0, "built": sorted(engine.views)},
                     ensure_ascii=False, separators=(",", ":")))
    return 0


def cmd_apply(store, args):
    if not store.exists():
        print("尚未 build，先运行 python app.py build", file=sys.stderr)
        return EXIT_STEP_ERROR
    _, _, engine = _load_engine(store)
    applied = skipped = 0
    try:
        for path in args.files:
            with open(path, encoding="utf-8") as f:
                for lineno, line in enumerate(f, 1):
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    step, ops = rec["step"], rec["ops"]
                    deltas = engine.apply_step(step, ops)
                    if deltas is None:
                        skipped += 1
                        continue
                    applied += 1
                    store.append_delta({"step": step, "ops": len(ops),
                                        "views": deltas})
                    store.append_log({
                        "watermark": step,
                        "ops": len(ops),
                        "changed_rows": {
                            name: len(d["inserted"]) + len(d["updated"]) + len(d["deleted"])
                            for name, d in deltas.items()
                        },
                    })
                    if step % CHECKPOINT_EVERY == 0:
                        store.write_checkpoint(engine)
    except StepError as e:
        store.flush(engine)
        print(f"apply 失败: {e}", file=sys.stderr)
        return EXIT_STEP_ERROR
    store.flush(engine)
    print(json.dumps({"watermark": engine.watermark, "applied": applied,
                      "skipped": skipped}, separators=(",", ":")))
    return 0


def cmd_dump(store, args):
    if not store.exists():
        print("尚未 build", file=sys.stderr)
        return EXIT_STEP_ERROR
    _, _, engine = _load_engine(store)
    snap = engine.snapshot()
    if args.view is not None:
        if args.view not in engine.views:
            print(f"未知视图 {args.view!r}", file=sys.stderr)
            return EXIT_USAGE
        snap["views"] = [v for v in snap["views"] if v["view"] == args.view]
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 0


def cmd_query(store, args):
    if not store.exists():
        print("尚未 build", file=sys.stderr)
        return EXIT_STEP_ERROR
    _, _, engine = _load_engine(store)
    if args.view not in engine.views:
        print(f"未知视图 {args.view!r}", file=sys.stderr)
        return EXIT_USAGE
    d = engine.defs[args.view]
    parts = args.key.split(",")
    if len(parts) != len(d.group_cols):
        print(f"键长度 {len(parts)} 与分组列数 {len(d.group_cols)} 不符",
              file=sys.stderr)
        return EXIT_USAGE
    key = []
    for raw, (t, c) in zip(parts, d.group_cols):
        typ = engine.schema.coltypes[t][c]
        if typ == "int":
            try:
                key.append(int(raw))
            except ValueError:
                print(f"键分量 {raw!r} 不是 int", file=sys.stderr)
                return EXIT_USAGE
        else:
            key.append(raw)
    row = engine.query(args.view, key)
    print(json.dumps({"step": engine.watermark, "view": args.view,
                      "key": key, "row": row}, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(_store, args):
    handler = functools.partial(SimpleHTTPRequestHandler, directory="web")
    with ThreadingHTTPServer(("", args.port), handler) as httpd:
        print(f"serving http://localhost:{args.port}/ (Ctrl+C 退出)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="app.py",
                                     description="viewkeep 物化视图增量维护")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("files", nargs="+")
    p_dump = sub.add_parser("dump")
    p_dump.add_argument("--view", default=None)
    p_query = sub.add_parser("query")
    p_query.add_argument("--view", required=True)
    p_query.add_argument("--key", required=True)
    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    store = Store(".")
    handlers = {"build": cmd_build, "apply": cmd_apply, "dump": cmd_dump,
                "query": cmd_query, "serve": cmd_serve}
    try:
        return handlers[args.cmd](store, args)
    except DefError as e:
        print(f"定义错误: {e}", file=sys.stderr)
        return EXIT_DEF_ERROR
    except FileNotFoundError as e:
        print(f"文件缺失: {e}", file=sys.stderr)
        return EXIT_DEF_ERROR


if __name__ == "__main__":
    sys.exit(main())
