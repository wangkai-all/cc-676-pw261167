#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立校验器：不导入修复器/求解器任何代码，重新核验候选清单。

用法：
    python3 -I verifier.py DATA_DIR CANDIDATE_JSON

退出码：0 = 全部通过；1 = 存在违规；2 = 用法或读取错误。
结果以 JSON 打印到 stdout。
"""
import csv
import hashlib
import json
import sys

NULL = ""
WEIGHT_COL = "weight"
TOL = 1e-6


def _load(data_dir):
    with open(f"{data_dir}/constraints.json", encoding="utf-8") as f:
        constraints = json.load(f)
    tables = {}
    for tspec in constraints["tables"]:
        name, pk, fname = tspec["name"], tspec["pk"], tspec["file"]
        with open(f"{data_dir}/{fname}", newline="", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            columns = reader.fieldnames or []
            rows = []
            for i, raw in enumerate(reader, start=1):
                cells = {c: (raw.get(c) or "") for c in columns}
                rows.append({"rid": f"{name}#{i}", "cells": cells,
                             "weight": float(cells[WEIGHT_COL])})
        tables[name] = {"pk": pk, "columns": list(columns), "rows": rows}
    return tables, constraints


def _fingerprint(tables, constraints):
    payload = {
        "tables": {
            name: {
                "pk": t["pk"],
                "columns": t["columns"],
                "rows": [[r["cells"].get(c, NULL) for c in t["columns"]]
                         for r in t["rows"]],
            } for name, t in sorted(tables.items())
        },
        "constraints": constraints,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def verify(data_dir, candidate):
    tables, constraints = _load(data_dir)
    by_rid = {}
    for t in tables.values():
        for r in t["rows"]:
            by_rid[r["rid"]] = r
    violations = []
    checks = {}

    kept = list(dict.fromkeys(candidate.get("kept", [])))
    kept_set = set(kept)
    if len(kept) != len(kept_set):
        violations.append("候选清单中存在重复行号")

    # 0) 输入指纹一致（候选针对当前数据生成）
    fp = _fingerprint(tables, constraints)
    checks["fingerprint"] = fp
    if candidate.get("fingerprint") != fp:
        violations.append("候选指纹与当前数据/约束不一致（权重或约束已被修改）")

    # 1) 行存在
    for rid in kept:
        if rid not in by_rid:
            violations.append(f"候选行不存在: {rid}")

    # 2) 主键：非空且表内唯一
    for name, t in tables.items():
        seen = {}
        for r in t["rows"]:
            if r["rid"] not in kept_set:
                continue
            pk = r["cells"].get(t["pk"], NULL)
            if pk == NULL:
                violations.append(f"{r['rid']}: 主键为空却入仓")
            elif pk in seen:
                violations.append(
                    f"表 {name}: 主键 {t['pk']}={pk} 重复入仓（{seen[pk]} 与 {r['rid']}）")
            else:
                seen[pk] = r["rid"]
    checks["pk_unique"] = True

    # 3) 函数依赖
    for fd in constraints.get("fds", []):
        t = tables[fd["table"]]
        det, dep = fd["det"], fd["dep"]
        groups = {}
        for r in t["rows"]:
            if r["rid"] not in kept_set:
                continue
            key = tuple(r["cells"].get(c, NULL) for c in det)
            if NULL in key:
                continue
            dv = tuple(r["cells"].get(c, NULL) for c in dep)
            if key in groups and groups[key][0] != dv:
                violations.append(
                    f"FD[{fd.get('label', fd)}] 被违反: {fd['table']} 中 {det}={list(key)} "
                    f"的行 {groups[key][1]} 与 {r['rid']} 的 {dep} 不一致")
            else:
                groups[key] = (dv, r["rid"])
    checks["fds"] = len(constraints.get("fds", []))

    # 4) 外键：每个非空引用至少有一行保留的父行
    pk_index = {}
    for name, t in tables.items():
        for r in t["rows"]:
            if r["rid"] in kept_set:
                pk_index.setdefault((name, r["cells"].get(t["pk"], NULL)), []).append(r["rid"])
    for fk in constraints.get("fks", []):
        ct = tables[fk["child_table"]]
        for r in ct["rows"]:
            if r["rid"] not in kept_set:
                continue
            v = r["cells"].get(fk["child_col"], NULL)
            if v == NULL:
                continue
            if not pk_index.get((fk["parent_table"], v)):
                violations.append(
                    f"FK[{fk.get('label', fk)}] 被违反: {r['rid']} 引用 "
                    f"{fk['parent_table']}.{fk['parent_col']}={v}，但无保留的父行")
    checks["fks"] = len(constraints.get("fks", []))

    # 5) 必留行全部保留
    for rid in candidate.get("must_keep", []):
        if rid not in kept_set:
            violations.append(f"必留行 {rid} 未被保留")

    # 6) 总权重复核
    wsum = sum(by_rid[r]["weight"] for r in kept_set if r in by_rid)
    checks["weight"] = wsum
    claimed = candidate.get("weight")
    if claimed is not None and abs(wsum - claimed) > TOL * max(1.0, abs(wsum)):
        violations.append(f"总权重不符：候选声称 {claimed}，复核为 {wsum}")

    return {"ok": not violations, "violations": violations, "checks": checks}


def main(argv):
    if len(argv) != 3:
        print("用法: verifier.py DATA_DIR CANDIDATE_JSON", file=sys.stderr)
        return 2
    data_dir, cand_path = argv[1], argv[2]
    try:
        with open(cand_path, encoding="utf-8") as f:
            candidate = json.load(f)
        report = verify(data_dir, candidate)
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
        print(json.dumps({"ok": False, "violations": [f"校验器读取失败: {e}"]},
                         ensure_ascii=False))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
