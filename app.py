#!/usr/bin/env python3
"""viewkeep 唯一命令行入口:build / apply / dump / query / serve。"""

import argparse
import functools
import json
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import viewkeep

INIT_DIR = "samples/init"
VAR_DIR = "var"
WEB_DIR = "web"


def _load_or_exit():
    try:
        return viewkeep.Engine.load(INIT_DIR, VAR_DIR)
    except FileNotFoundError:
        print("未找到 %s/state.json,请先运行 build" % VAR_DIR, file=sys.stderr)
        sys.exit(1)


def _emit(obj):
    print(json.dumps(obj, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")))


def cmd_build(_args):
    engine = viewkeep.Engine.build(INIT_DIR)
    engine.save(VAR_DIR)
    engine.export_web(WEB_DIR)
    _emit({"watermark": engine.watermark, "views": len(engine.views)})


def cmd_apply(args):
    engine = _load_or_exit()
    applied = 0
    skipped = 0
    try:
        for path in args.files:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    record = json.loads(line)
                    result = engine.apply_step(record["step"], record["ops"])
                    if result is None:
                        skipped += 1
                    else:
                        applied += 1
    except (viewkeep.StepError, ValueError, KeyError) as exc:
        engine.save(VAR_DIR)
        engine.export_web(WEB_DIR)
        print("apply 失败: %s" % exc, file=sys.stderr)
        sys.exit(1)
    engine.save(VAR_DIR)
    engine.export_web(WEB_DIR)
    _emit({"watermark": engine.watermark,
           "applied": applied, "skipped": skipped})


def cmd_dump(args):
    engine = _load_or_exit()
    snapshot = engine.snapshot()
    if args.view is not None:
        if args.view not in engine.view_by_name:
            print("未知视图: %r" % args.view, file=sys.stderr)
            sys.exit(2)
        snapshot["views"] = [
            part for part in snapshot["views"] if part["view"] == args.view
        ]
    _emit(snapshot)


def cmd_query(args):
    engine = _load_or_exit()
    view = engine.view_by_name.get(args.view)
    if view is None:
        print("未知视图: %r" % args.view, file=sys.stderr)
        sys.exit(2)
    values = args.key.split(",")
    if len(values) != len(view.group_cols):
        print("键长度 %d 与分组列数 %d 不符"
              % (len(values), len(view.group_cols)), file=sys.stderr)
        sys.exit(2)
    key = []
    for (table, column), raw in zip(view.group_cols, values):
        col_type = engine.col_type[(table, column)]
        try:
            key.append(int(raw) if col_type == "int" else raw)
        except ValueError:
            print("键 %r 无法按 %s 列 %s 解析"
                  % (raw, col_type, column), file=sys.stderr)
            sys.exit(2)
    row = view.row_for(tuple(key))
    _emit({"step": engine.watermark, "view": args.view,
           "key": key, "row": row})


def cmd_serve(args):
    handler = functools.partial(SimpleHTTPRequestHandler, directory=WEB_DIR)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print("serving %s at http://localhost:%d/" % (WEB_DIR, args.port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


def main(argv=None):
    parser = argparse.ArgumentParser(prog="app.py")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("build").set_defaults(func=cmd_build)

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("files", nargs="+")
    p_apply.set_defaults(func=cmd_apply)

    p_dump = sub.add_parser("dump")
    p_dump.add_argument("--view")
    p_dump.set_defaults(func=cmd_dump)

    p_query = sub.add_parser("query")
    p_query.add_argument("--view", required=True)
    p_query.add_argument("--key", required=True)
    p_query.set_defaults(func=cmd_query)

    p_serve = sub.add_parser("serve")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.set_defaults(func=cmd_serve)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
