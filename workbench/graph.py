# -*- coding: utf-8 -*-
"""冲突图构建：主键重复、函数依赖冲突、外键蕴含、孤儿行、连通分量。"""
from collections import defaultdict

from model import NULL


class Graph:
    def __init__(self, inst):
        self.inst = inst
        self.conflict_reasons = defaultdict(list)   # (ridA, ridB) 有序对 -> [原因]
        self.fk_needs = defaultdict(list)           # rid -> [{"fk": fk, "value": v, "matches": [rid]}]
        self.forced_out = {}                        # rid -> 原因（空主键 / 孤儿）
        self.components = []                        # [ [rid] ]，仅含非强制隔离行
        self._build()

    def _add_conflict(self, a, b, reason):
        if a == b:
            return
        key = (a, b) if a < b else (b, a)
        self.conflict_reasons[key].append(reason)

    def _build(self):
        inst = self.inst
        # 1) 空主键 -> 强制隔离
        for r in inst.rows:
            if r.pk == NULL:
                self.forced_out[r.rid] = "主键为空：按固定语义不可入仓"

        # 2) 主键重复 -> 两两冲突
        for t in inst.tables.values():
            by_pk = defaultdict(list)
            for r in t.rows:
                if r.pk != NULL:
                    by_pk[r.pk].append(r)
            for pkval, rs in by_pk.items():
                for i in range(len(rs)):
                    for j in range(i + 1, len(rs)):
                        self._add_conflict(
                            rs[i].rid, rs[j].rid,
                            f"主键重复：{t.pk_col}={pkval} 在表 {t.name} 中出现 {len(rs)} 次")

        # 3) 函数依赖冲突
        for fd in inst.constraints.get("fds", []):
            t = inst.tables[fd["table"]]
            det, dep = fd["det"], fd["dep"]
            label = fd.get("label") or f"{t.name}: {det} -> {dep}"
            groups = defaultdict(list)
            for r in t.rows:
                key = tuple(r.cells.get(c, NULL) for c in det)
                if NULL in key:
                    continue  # det 含空值的行不参与该 FD
                groups[key].append(r)
            for key, rs in groups.items():
                by_dep = defaultdict(list)
                for r in rs:
                    dv = tuple(r.cells.get(c, NULL) for c in dep)
                    by_dep[dv].append(r)
                vals = list(by_dep)
                for i in range(len(vals)):
                    for j in range(i + 1, len(vals)):
                        for ra in by_dep[vals[i]]:
                            for rb in by_dep[vals[j]]:
                                self._add_conflict(
                                    ra.rid, rb.rid,
                                    f"函数依赖[{label}]：{list(zip(det, key))} 相同，"
                                    f"但 {dep} 分别为 {list(vals[i])} 与 {list(vals[j])}")

        # 4) 外键：匹配父候选；无匹配 -> 孤儿强制隔离
        pk_index = {}  # (table, pkval) -> [rid]
        for t in inst.tables.values():
            for r in t.rows:
                if r.pk != NULL:
                    pk_index.setdefault((t.name, r.pk), []).append(r.rid)
        for fk in inst.constraints.get("fks", []):
            ct = inst.tables[fk["child_table"]]
            label = fk.get("label") or f"{fk['child_table']}.{fk['child_col']} -> {fk['parent_table']}.{fk['parent_col']}"
            for r in ct.rows:
                v = r.cells.get(fk["child_col"], NULL)
                if v == NULL:
                    continue  # 空外键：无引用要求
                matches = list(pk_index.get((fk["parent_table"], v), []))
                self.fk_needs[r.rid].append({"fk": label, "value": v, "matches": matches})
                if not matches and r.rid not in self.forced_out:
                    self.forced_out[r.rid] = (
                        f"孤儿行：外键 {fk['child_col']}={v} 在父表 "
                        f"{fk['parent_table']} 的候选中不存在（{label}）")

        # 4b) 级联：父候选全部被强制隔离的行也随之强制隔离
        changed = True
        while changed:
            changed = False
            for rid, needs in self.fk_needs.items():
                if rid in self.forced_out:
                    continue
                for need in needs:
                    if need["matches"] and all(m in self.forced_out
                                               for m in need["matches"]):
                        self.forced_out[rid] = (
                            f"外键 {need['fk']}（值 {need['value']}）的父候选行"
                            f"均被强制隔离，本行随之隔离")
                        changed = True
                        break

        # 5) 弱连通分量（冲突边 + 外键边），仅含可参与求解的行
        adj = defaultdict(set)
        for (a, b) in self.conflict_reasons:
            adj[a].add(b)
            adj[b].add(a)
        for rid, needs in self.fk_needs.items():
            for need in needs:
                for m in need["matches"]:
                    adj[rid].add(m)
                    adj[m].add(rid)
        live = [r.rid for r in inst.rows if r.rid not in self.forced_out]
        seen = set()
        for rid in live:
            if rid in seen:
                continue
            comp, stack = [], [rid]
            seen.add(rid)
            while stack:
                u = stack.pop()
                comp.append(u)
                for v in adj.get(u, ()):
                    if v not in seen and v not in self.forced_out:
                        seen.add(v)
                        stack.append(v)
            comp.sort()
            self.components.append(comp)
        self.components.sort(key=lambda c: (len(c), c))

    # ---- 查询接口 ----
    def conflicts_of(self, rid):
        out = []
        for (a, b), reasons in self.conflict_reasons.items():
            if a == rid:
                out.append((b, reasons))
            elif b == rid:
                out.append((a, reasons))
        return out

    def summary(self):
        return {
            "rows": len(self.inst.rows),
            "conflict_edges": len(self.conflict_reasons),
            "fk_refs": sum(len(v) for v in self.fk_needs.values()),
            "forced_out": len(self.forced_out),
            "components": [len(c) for c in self.components],
        }
