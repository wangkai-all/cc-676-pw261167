#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入仓修复工作台 HTTP 服务：API + 静态页面。仅依赖标准库。"""
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import explain
import graph as graph_mod
import model
import publisher
import solver

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.environ.get("WORKBENCH_DATA", os.path.join(ROOT, "data"))
RELEASES_DIR = os.environ.get("WORKBENCH_RELEASES", os.path.join(ROOT, "releases"))
WEB_DIR = os.path.join(ROOT, "web")
VERIFIER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verifier.py")

STATE = {"inst": None, "graph": None, "load_error": None, "candidates": {}}


def reload_data():
    try:
        inst = model.load_instance(DATA_DIR)
        STATE["inst"] = inst
        STATE["graph"] = graph_mod.Graph(inst)
        STATE["load_error"] = None
    except model.LoadError as e:
        STATE["load_error"] = str(e)


def state_payload():
    if STATE["load_error"]:
        return {"ok": False, "load_error": STATE["load_error"]}
    inst, g = STATE["inst"], STATE["graph"]
    tables = []
    for t in inst.tables.values():
        tables.append({
            "name": t.name, "pk": t.pk_col, "columns": t.columns,
            "rows": [{"rid": r.rid, "line": r.line, "cells": r.cells,
                      "weight": r.weight, "pk": r.pk} for r in t.rows]})
    return {
        "ok": True,
        "fingerprint": inst.fingerprint,
        "backend": solver.BACKEND,
        "data_dir": os.path.abspath(DATA_DIR),
        "tables": tables,
        "constraints": inst.constraints,
        "graph": {
            "conflicts": [{"a": a, "b": b, "reasons": rs}
                          for (a, b), rs in sorted(g.conflict_reasons.items())],
            "fk": [{"child": rid, "fk": n["fk"], "value": n["value"],
                    "matches": n["matches"]}
                   for rid, needs in sorted(g.fk_needs.items()) for n in needs],
            "forced_out": g.forced_out,
            "components": g.components,
            "summary": g.summary(),
        },
        "releases": publisher.list_releases(RELEASES_DIR),
    }


def do_solve(body):
    inst, g = STATE["inst"], STATE["graph"]
    must = body.get("must_keep", [])
    time_limit = float(body.get("time_limit", 10.0))
    time_limit = max(0.001, min(time_limit, 600.0))
    try:
        res = solver.solve(inst, g, must, time_limit=time_limit)
    except ValueError as e:
        return 400, {"error": str(e)}

    cand = {
        "id": uuid.uuid4().hex[:12],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": inst.fingerprint,
        "must_keep": sorted(must),
        "status": res.status,
        "backend": res.backend,
        "weight": res.weight,
        "bound": res.bound,
        "gap": res.gap,
        "kept": sorted(res.kept),
        "seconds": round(res.seconds, 3),
        "note": res.note,
        "comp_details": res.comp_details,
        "crosscheck": res.crosscheck,
        "alternates_count": res.alternates_count,
        "alternates_examples": res.alternates_examples,
    }
    if res.status == "infeasible":
        def feasible_fn(subset):
            r = solver.solve(inst, g, subset, time_limit=10.0)
            return r.status != "infeasible"
        cand["diagnosis"] = explain.diagnose_infeasible(inst, g, must, feasible_fn)
    else:
        cand["explanations"] = explain.explain_quarantine(inst, g, res.kept)
        cand["kept_roles"] = explain.kept_roles(inst, g, res.kept)
    STATE["candidates"][cand["id"]] = cand
    if len(STATE["candidates"]) > 32:  # 只保留近期候选
        for k in list(STATE["candidates"])[:-32]:
            STATE["candidates"].pop(k, None)
    return 200, cand


def do_publish(body):
    inst = STATE["inst"]
    cand = STATE["candidates"].get(body.get("candidate_id", ""))
    if cand is None:
        return 404, {"error": "候选不存在（可能服务已重启），请重新求解"}
    try:
        manifest = publisher.publish(inst, cand, cand.get("explanations", {}),
                                     RELEASES_DIR, VERIFIER)
    except publisher.StaleCandidate as e:
        return 409, {"error": str(e), "stale": True}
    except publisher.VerificationFailed as e:
        return 422, {"error": str(e), "report": e.report}
    except publisher.NotPublishable as e:
        return 400, {"error": str(e)}
    return 200, {"manifest": manifest,
                 "releases": publisher.list_releases(RELEASES_DIR)}


class Handler(BaseHTTPRequestHandler):
    server_version = "RepairWorkbench/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _send_json(self, code, obj):
        blob = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def _send_file(self, path, content_type):
        try:
            with open(path, "rb") as f:
                blob = f.read()
        except OSError:
            self._send_json(404, {"error": "not found"})
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        self.wfile.write(blob)

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/":
            return self._send_file(os.path.join(WEB_DIR, "index.html"), "text/html; charset=utf-8")
        if path == "/app.js":
            return self._send_file(os.path.join(WEB_DIR, "app.js"), "text/javascript; charset=utf-8")
        if path == "/style.css":
            return self._send_file(os.path.join(WEB_DIR, "style.css"), "text/css; charset=utf-8")
        if path == "/api/state":
            return self._send_json(200, state_payload())
        if path.startswith("/releases/"):
            rel = os.path.normpath(path[len("/releases/"):])
            full = os.path.join(RELEASES_DIR, rel)
            if not os.path.abspath(full).startswith(os.path.abspath(RELEASES_DIR)):
                return self._send_json(403, {"error": "forbidden"})
            ctype = "text/csv; charset=utf-8" if full.endswith(".csv") else "application/json; charset=utf-8"
            return self._send_file(full, ctype)
        return self._send_json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._send_json(400, {"error": "请求体不是合法 JSON"})
        if STATE["load_error"] and path != "/api/reload":
            return self._send_json(500, {"error": "数据载入失败: " + STATE["load_error"]})
        if path == "/api/solve":
            code, obj = do_solve(body)
            return self._send_json(code, obj)
        if path == "/api/publish":
            code, obj = do_publish(body)
            return self._send_json(code, obj)
        if path == "/api/reload":
            reload_data()
            return self._send_json(200, state_payload())
        return self._send_json(404, {"error": "not found"})


def main():
    port = int(os.environ.get("PORT", "8074"))
    reload_data()
    if STATE["load_error"]:
        print("数据载入失败:", STATE["load_error"], file=sys.stderr)
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"入仓修复工作台: http://127.0.0.1:{port}/  (求解后端: {solver.BACKEND})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
