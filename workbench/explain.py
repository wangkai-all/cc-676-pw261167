# -*- coding: utf-8 -*-
"""解释引擎：为什么这行被舍弃；必留不可行时给出具体冲突。"""


def explain_quarantine(inst, graph, kept):
    """为每个未保留行生成原因。返回 {rid: {"reasons": [...], "needs": [...],
    "blocks": [...], "weight": w}}。"""
    kept = set(kept)
    out = {}
    for r in inst.rows:
        rid = r.rid
        if rid in kept:
            continue
        info = {"weight": r.weight, "reasons": [], "needs": [], "blocks": []}
        if rid in graph.forced_out:
            info["reasons"].append(graph.forced_out[rid])
            out[rid] = info
            continue
        # 贪心构造“启用集”：要保留本行，还需同时保留哪些父行
        enabling = {rid}
        queue = [rid]
        unmet = []
        blocked_parents = {}   # 被阻塞的父候选 -> 阻塞它的已保留行
        while queue:
            u = queue.pop()
            for need in graph.fk_needs.get(u, []):
                live_matches = [m for m in need["matches"]
                                if m not in graph.forced_out]
                if any(m in kept or m in enabling for m in live_matches):
                    continue
                chosen = None
                for m in sorted(live_matches,
                                key=lambda m: (-_w(inst, m), m)):
                    clash = [x for (x, _) in graph.conflicts_of(m)
                             if x in kept or x in enabling]
                    if not clash:
                        chosen = m
                        break
                    blocked_parents.setdefault(m, []).extend(clash)
                if chosen is None:
                    unmet.append(f"外键 {need['fk']}（值 {need['value']}）的全部"
                                 f"父候选都与保留集冲突或被强制隔离：{live_matches}")
                else:
                    enabling.add(chosen)
                    queue.append(chosen)
        blocks = {}
        for e in enabling:
            for (other, reasons) in graph.conflicts_of(e):
                if other in kept:
                    blocks.setdefault(other, []).extend(reasons)
        for m, clashers in blocked_parents.items():
            for c in clashers:
                if c in kept:
                    for (cc, reasons) in graph.conflicts_of(m):
                        if cc == c:
                            blocks.setdefault(c, []).extend(
                                [f"（经父候选 {m}）{r}" for r in reasons])
        extra = sorted(enabling - {rid})
        if extra:
            info["needs"] = extra
        if unmet:
            info["reasons"].extend(unmet)
        if blocks:
            info["blocks"] = [
                {"rid": b, "weight": _w(inst, b),
                 "reasons": sorted(set(rs))} for b, rs in sorted(blocks.items())]
            tot = sum(_w(inst, b) for b in blocks)
            tie = any(abs(_w(inst, b) - r.weight) <= 1e-9 for b in blocks)
            tie_note = "；双方权重相同，规范解按固定行身份裁决" if tie else ""
            if extra:
                info["reasons"].append(
                    f"保留本行（权重 {_fmt(r.weight)}）需要同时保留 {extra}，"
                    f"但这会与已保留行冲突；冲突方权重合计 {_fmt(tot)}，"
                    f"为最大化总权重舍弃本行{tie_note}")
            elif blocked_parents:
                parents = sorted(blocked_parents)
                info["reasons"].append(
                    f"保留本行（权重 {_fmt(r.weight)}）需要父候选 {parents}，"
                    f"而它们与已保留行冲突；冲突方权重合计 {_fmt(tot)}，"
                    f"为最大化总权重舍弃本行{tie_note}")
            else:
                info["reasons"].append(
                    f"本行（权重 {_fmt(r.weight)}）与已保留行直接冲突；"
                    f"冲突方权重合计 {_fmt(tot)}，为最大化总权重舍弃本行{tie_note}")
        elif not unmet:
            info["reasons"].append(
                f"本行及其依赖不与任何保留行冲突，未入选属于权重权衡"
                f"（本行权重 {_fmt(r.weight)}）或等价最优解的规范选择")
        out[rid] = info
    return out


def kept_roles(inst, graph, kept):
    """每个保留行支撑了哪些保留的子行（外键被其满足）。"""
    kept = set(kept)
    roles = {rid: [] for rid in kept}
    for rid in kept:
        for need in graph.fk_needs.get(rid, []):
            for m in need["matches"]:
                if m in kept:
                    roles[m].append(rid)
    return roles


def diagnose_infeasible(inst, graph, must_keep, feasible_fn):
    """必留导致不可行时的具体诊断。
    feasible_fn(subset_of_must) -> bool。返回 {"core": [...], "findings": [...]}。"""
    must = list(dict.fromkeys(must_keep))
    findings = []

    # 1) 必留行本身被强制隔离（空主键 / 孤儿）
    for rid in must:
        if rid in graph.forced_out:
            findings.append({
                "type": "forced_out", "rows": [rid],
                "detail": f"必留行 {rid} 被强制隔离：{graph.forced_out[rid]}"})

    # 2) 必留行两两直接冲突
    must_set = set(must)
    for rid in must:
        for (other, reasons) in graph.conflicts_of(rid):
            if other in must_set and other > rid:
                findings.append({
                    "type": "conflict_pair", "rows": [rid, other],
                    "detail": f"必留行 {rid} 与 {other} 直接冲突：{reasons[0]}"})

    # 3) 沿唯一父候选传播，找出“必留 -> 被迫保留 -> 冲突”的链
    forced = set(must_set)
    why = {}  # rid <- 由哪个子行迫使
    changed = True
    while changed:
        changed = False
        for rid in list(forced):
            for need in graph.fk_needs.get(rid, []):
                live = [m for m in need["matches"] if m not in graph.forced_out]
                if len(live) == 1 and live[0] not in forced:
                    forced.add(live[0])
                    why[live[0]] = (rid, need["fk"])
                    changed = True
    reported = set()
    for rid in sorted(forced):
        for (other, reasons) in graph.conflicts_of(rid):
            if other in forced and other > rid and (rid, other) not in reported:
                if rid in must_set and other in must_set:
                    continue  # 已在第 2 类报告
                reported.add((rid, other))
                chain = _chain(why, rid) + _chain(why, other)
                findings.append({
                    "type": "chain", "rows": sorted({rid, other} | set(chain)),
                    "detail": (f"必留约束经外键传递迫使 {rid} 与 {other} 同时保留，"
                               f"但二者冲突：{reasons[0]}；传递链：{'、'.join(chain) or '无'}")})

    # 4) 极小不可行核心（与删除顺序有关的极小集，不一定最小）
    core = list(must)
    for m in list(core):
        rest = [x for x in core if x != m]
        if not feasible_fn(rest):
            core = rest
    if not findings:
        findings.append({
            "type": "core", "rows": core,
            "detail": "这些必留行组合不可行（涉及析取父候选的组合冲突），"
                      "请取消其中部分必留"})
    return {"core": core, "findings": findings}


def _chain(why, rid):
    out = []
    while rid in why:
        child, fk = why[rid]
        out.append(f"{child} -[{fk}]-> {rid}")
        rid = child
    return out


def _w(inst, rid):
    t, line = rid.split("#")
    return inst.tables[t].rows[int(line) - 1].weight


def _fmt(w):
    return f"{w:g}"
