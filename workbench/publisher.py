# -*- coding: utf-8 -*-
"""发布器：候选先经独立校验器（子进程）复核，通过后才写出仓表与隔离文件。
每个版本写入 releases/v<N>/，旧版本永不覆盖。"""
import csv
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone


class StaleCandidate(Exception):
    """权重或约束已变化，候选失效。"""


class VerificationFailed(Exception):
    def __init__(self, report):
        super().__init__("独立校验器复核未通过")
        self.report = report


class NotPublishable(Exception):
    pass


def _next_version(releases_dir):
    n = 0
    if os.path.isdir(releases_dir):
        for name in os.listdir(releases_dir):
            if name.startswith("v") and name[1:].isdigit():
                n = max(n, int(name[1:]))
    return n + 1


def list_releases(releases_dir):
    out = []
    if not os.path.isdir(releases_dir):
        return out
    for name in sorted(os.listdir(releases_dir)):
        mpath = os.path.join(releases_dir, name, "manifest.json")
        if os.path.isfile(mpath):
            try:
                with open(mpath, encoding="utf-8") as f:
                    out.append(json.load(f))
            except (OSError, json.JSONDecodeError):
                continue
    out.sort(key=lambda m: m.get("version", 0))
    return out


def publish(inst, candidate, quarantine_reasons, releases_dir, verifier_path):
    """发布候选方案。inst 为当前已载入数据（用于指纹比对与取行内容）。"""
    if candidate.get("fingerprint") != inst.fingerprint:
        raise StaleCandidate(
            "候选已失效：权重或约束在求解后被修改，请重新求解")
    if candidate.get("status") not in ("optimal", "feasible"):
        raise NotPublishable(f"状态为 {candidate.get('status')} 的候选不可发布")

    # 1) 独立校验器复核（隔离进程，不经过求解器代码）
    with tempfile.NamedTemporaryFile(
            "w", suffix=".json", prefix="candidate_", delete=False,
            encoding="utf-8") as tf:
        json.dump(candidate, tf, ensure_ascii=False)
        tmp_path = tf.name
    try:
        proc = subprocess.run(
            [sys.executable, "-I", verifier_path, inst.source_dir, tmp_path],
            capture_output=True, text=True, timeout=120)
        try:
            report = json.loads(proc.stdout)
        except json.JSONDecodeError:
            report = {"ok": False,
                      "violations": [f"校验器输出无法解析: {proc.stdout[:500]} {proc.stderr[:500]}"]}
    finally:
        os.unlink(tmp_path)
    if not report.get("ok"):
        raise VerificationFailed(report)

    # 2) 分配版本目录（不覆盖旧版本）
    version = _next_version(releases_dir)
    rel_dir = os.path.join(releases_dir, f"v{version}")
    os.makedirs(os.path.join(rel_dir, "warehouse"))
    os.makedirs(os.path.join(rel_dir, "quarantine"))

    kept = set(candidate["kept"])
    per_table = {}
    for name, table in inst.tables.items():
        kept_rows = [r for r in table.rows if r.rid in kept]
        drop_rows = [r for r in table.rows if r.rid not in kept]
        per_table[name] = {"kept": len(kept_rows), "quarantined": len(drop_rows)}

        wpath = os.path.join(rel_dir, "warehouse", f"{name}.csv")
        with open(wpath, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=table.columns)
            w.writeheader()
            for r in kept_rows:
                w.writerow(r.cells)

        qpath = os.path.join(rel_dir, "quarantine", f"{name}.csv")
        with open(qpath, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=table.columns + ["quarantine_reason"])
            w.writeheader()
            for r in drop_rows:
                row = dict(r.cells)
                reasons = quarantine_reasons.get(r.rid, {}).get("reasons", [])
                row["quarantine_reason"] = "；".join(reasons)
                w.writerow(row)

    manifest = {
        "version": version,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "fingerprint": inst.fingerprint,
        "solver_status": candidate.get("status"),
        "solver_backend": candidate.get("backend"),
        "weight": candidate.get("weight"),
        "bound": candidate.get("bound"),
        "gap": candidate.get("gap"),
        "alternates_count": candidate.get("alternates_count"),
        "must_keep": candidate.get("must_keep", []),
        "kept_total": len(kept),
        "quarantined_total": len(inst.rows) - len(kept),
        "per_table": per_table,
        "verifier_ok": report.get("ok", False),
    }
    with open(os.path.join(rel_dir, "candidate.json"), "w", encoding="utf-8") as f:
        json.dump(candidate, f, ensure_ascii=False, indent=2)
    with open(os.path.join(rel_dir, "verifier_report.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(rel_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    return manifest
