# -*- coding: utf-8 -*-
"""整数规划求解：最大权重可行子集。

模型（0-1 整数规划）：
  x_i ∈ {0,1}
  冲突边 (i,j)      : x_i + x_j <= 1
  外键析取 c->G     : x_c <= Σ_{p∈G} x_p     （G 为匹配父候选集合）
  强制隔离          : x_i = 0                （空主键 / 孤儿，图构建期已剔除）
  必留              : x_i = 1
  目标              : max Σ w_i x_i

平局裁决：第一阶段求出最优权重 W* 后，第二阶段在 Σ w_i x_i >= W* - tol
下最大化固定行身份偏好 Σ pref_i x_i（pref 由 (表, 源行号) 顺序决定），
得到唯一规范解；其余等价最优解不丢弃，单独计数并给出示例。

后端：优先 scipy.optimize.milp（HiGHS）；不可用时退到内置分支定界。
两者都在超时时返回当前可行解与上界——未证明最优绝不声称最优。
"""
import time
from dataclasses import dataclass, field

TOL = 1e-6
BRUTE_MAX = 18  # 穷举校对的分量行数上限

try:
    import numpy as _np
    from scipy.optimize import milp as _milp, LinearConstraint as _LC, Bounds as _B

    HAVE_SCIPY = True
except Exception:
    HAVE_SCIPY = False

BACKEND = "scipy-milp(HiGHS)" if HAVE_SCIPY else "builtin-bnb(分支定界)"


# ---------------------------------------------------------------- 分量

class Comp:
    """一个弱连通分量上的子问题。"""

    def __init__(self, rids, rank_of, weight_of, conflict_pairs, fk_groups, must):
        self.rids = list(rids)
        self.n = len(self.rids)
        self.idx = {r: i for i, r in enumerate(self.rids)}
        self.w = [weight_of[r] for r in self.rids]
        n_global = len(rank_of)
        self.pref = [float(n_global - rank_of[r]) for r in self.rids]
        self.conflicts = [(self.idx[a], self.idx[b]) for a, b in conflict_pairs]
        self.fk_groups = {self.idx[c]: [[self.idx[p] for p in grp] for grp in groups]
                          for c, groups in fk_groups.items()}
        self.must = {self.idx[m] for m in must}


@dataclass
class CompResult:
    status: str                 # optimal | feasible | no_feasible | infeasible
    x: list = None              # 0/1 列表
    obj: float = 0.0            # 当前解的目标权重
    bound: float = None         # 该分量最优值上界（未证明最优时给出）
    tie_broken: bool = False


@dataclass
class SolveResult:
    status: str                 # optimal | feasible | no_feasible | infeasible
    kept: set = field(default_factory=set)
    weight: float = 0.0
    bound: float = None
    gap: float = None
    backend: str = BACKEND
    comp_details: list = field(default_factory=list)
    crosscheck: dict = None
    alternates_count: object = None   # None = 未知
    alternates_examples: list = field(default_factory=list)
    seconds: float = 0.0
    note: str = ""


# ---------------------------------------------------------------- scipy 后端

def _solve_comp_scipy(comp, deadline):
    n = comp.n
    rows_a, rows_lb, rows_ub = [], [], []

    def add_row(coefs, lb, ub):
        rows_a.append(list(coefs))
        rows_lb.append(lb)
        rows_ub.append(ub)

    for (i, j) in comp.conflicts:
        row = [0.0] * n
        row[i] = row[j] = 1.0
        add_row(row, -_np.inf, 1.0)
    for c, groups in comp.fk_groups.items():
        for g in groups:
            row = [0.0] * n
            row[c] = 1.0
            for p in g:
                row[p] = -1.0
            add_row(row, -_np.inf, 0.0)

    lb = _np.zeros(n)
    ub = _np.ones(n)
    for i in comp.must:
        lb[i] = ub[i] = 1.0

    def run(obj, extra=None, time_left=None):
        A = list(rows_a)
        lo = list(rows_lb)
        hi = list(rows_ub)
        if extra:
            for coefs, elb, eub in extra:
                A.append(list(coefs))
                lo.append(elb)
                hi.append(eub)
        if A:
            con = _LC(_np.array(A), _np.array(lo), _np.array(hi))
            constraints = [con]
        else:
            constraints = None
        tl = max(0.0, deadline - time.monotonic()) if deadline else None
        return _milp(c=_np.array(obj), constraints=constraints,
                     integrality=_np.ones(n), bounds=_B(lb, ub),
                     options={"time_limit": tl, "mip_rel_gap": 0.0, "disp": False})

    w = comp.w
    res = run([-v for v in w])
    if res.status == 2:
        return CompResult(status="infeasible")
    if res.status not in (0, 1) or res.x is None:
        return CompResult(status="no_feasible")

    x1 = [1 if v >= 0.5 else 0 for v in res.x]
    W = sum(w[i] for i in range(n) if x1[i])
    if res.status != 0:
        bound = None
        db = getattr(res, "mip_dual_bound", None)
        if db is not None and _np.isfinite(db):
            bound = -float(db)
        return CompResult(status="feasible", x=x1, obj=W, bound=bound)

    # 第二阶段：平局按固定行身份裁决
    tol = TOL * max(1.0, abs(W))
    row = list(w)
    res2 = run([-v for v in comp.pref], extra=[(row, W - tol, _np.inf)])
    if res2.status in (0, 1) and res2.x is not None:
        x2 = [1 if v >= 0.5 else 0 for v in res2.x]
        W2 = sum(w[i] for i in range(n) if x2[i])
        if abs(W2 - W) <= tol * 10:
            return CompResult(status="optimal", x=x2, obj=W2, tie_broken=(res2.status == 0))
    return CompResult(status="optimal", x=x1, obj=W, tie_broken=False)


# ---------------------------------------------------------------- 内置分支定界

class _Timeout(Exception):
    pass


def _bnb(comp, obj, extra_lin, deadline):
    """最大化 Σ obj_i x_i。extra_lin: [(coefs, lb)] 形如 Σ coefs_i x_i >= lb。
    返回 (status, x, best_obj, upper_bound)。"""
    n = comp.n
    conf_adj = [set() for _ in range(n)]
    for i, j in comp.conflicts:
        conf_adj[i].add(j)
        conf_adj[j].add(i)
    fk = comp.fk_groups
    member_of = [[] for _ in range(n)]
    for c, groups in fk.items():
        for gi, g in enumerate(groups):
            for p in g:
                member_of[p].append((c, gi))

    state = [-1] * n
    best = {"x": None, "obj": float("-inf")}
    live = []          # 当前递归栈上各节点的上界（用于超时时的全局上界）
    counter = [0]

    def propagate(seed):
        trail = []
        ok = True
        stack = list(seed)
        while stack and ok:
            i, v = stack.pop()
            if state[i] != -1:
                if state[i] != v:
                    ok = False
                continue
            state[i] = v
            trail.append(i)
            if v == 1:
                for j in conf_adj[i]:
                    stack.append((j, 0))
                for g in fk.get(i, ()):
                    if any(state[m] == 1 for m in g):
                        continue
                    und = [m for m in g if state[m] == -1]
                    if not und:
                        ok = False
                        break
                    if len(und) == 1:
                        stack.append((und[0], 1))
            else:
                for (c, gi) in member_of[i]:
                    g = fk[c][gi]
                    if any(state[m] == 1 for m in g):
                        continue
                    und = [m for m in g if state[m] == -1]
                    if not und:
                        stack.append((c, 0))
                    elif len(und) == 1 and state[c] == 1:
                        stack.append((und[0], 1))
        if not ok:
            for t in trail:
                state[t] = -1
            return None
        return trail

    def node_bound():
        inc = 0.0
        for i in range(n):
            if state[i] == 1:
                inc += obj[i]
        for coefs, lb in extra_lin:
            s = 0.0
            for i in range(n):
                if state[i] == 1:
                    s += coefs[i]
                elif state[i] == -1 and coefs[i] > 0:
                    s += coefs[i]
            if s < lb - 1e-9:
                return float("-inf")
        und = [i for i in range(n) if state[i] == -1 and obj[i] > 0]
        # 团覆盖界：把未决行划分为冲突图中的团（=补图着色），
        # 独立集在每个团中至多取一个，故上界 = 各团最大权重之和。
        members_of = []   # 每个团的成员
        max_of = []       # 每个团的最大权重
        for i in sorted(und, key=lambda t: -obj[t]):
            placed = False
            for k, members in enumerate(members_of):
                if all(j in conf_adj[i] for j in members):
                    members.append(i)
                    if obj[i] > max_of[k]:
                        max_of[k] = obj[i]
                    placed = True
                    break
            if not placed:
                members_of.append([i])
                max_of.append(obj[i])
        return inc + sum(max_of)

    def search():
        counter[0] += 1
        if counter[0] % 512 == 0 and time.monotonic() > deadline:
            raise _Timeout()
        b = node_bound()
        if b <= best["obj"] + 1e-9:
            return
        live.append(b)
        try:
            # 仅当“未决行全部取 0”是合法完成时，已包含集合才可记为可行解
            completable = True
            for c, groups in fk.items():
                if state[c] == 1:
                    for g in groups:
                        if not any(state[m] == 1 for m in g):
                            completable = False
                            break
                if not completable:
                    break
            if completable:
                inc = sum(obj[i] for i in range(n) if state[i] == 1)
                if inc > best["obj"] + 1e-9:
                    best["obj"] = inc
                    best["x"] = [1 if state[i] == 1 else 0 for i in range(n)]
            pick = -1
            pick_obj = -1.0
            for i in range(n):
                if state[i] == -1 and obj[i] > pick_obj:
                    pick_obj = obj[i]
                    pick = i
            if pick == -1:
                return
            order = (1, 0) if obj[pick] > 0 else (0, 1)
            for v in order:
                trail = propagate([(pick, v)])
                if trail is not None:
                    search()
                    for t in trail:
                        state[t] = -1
        finally:
            live.pop()

    root = propagate([(i, 1) for i in comp.must])
    if root is None:
        return "infeasible", None, 0.0, None
    try:
        search()
    except _Timeout:
        ub = max(live) if live else None
        if best["x"] is None:
            return "no_feasible", None, 0.0, ub
        return "feasible", best["x"], best["obj"], ub
    if best["x"] is None:
        return "no_feasible", None, 0.0, None
    return "optimal", best["x"], best["obj"], None


def _solve_comp_bnb(comp, deadline):
    if deadline is None:
        deadline = float("inf")
    w = comp.w
    status, x, W, ub = _bnb(comp, w, [], deadline)
    if status != "optimal":
        return CompResult(status=status, x=x, obj=W if x else 0.0, bound=ub)
    tol = TOL * max(1.0, abs(W))
    st2, x2, _, _ = _bnb(comp, comp.pref, [(list(w), W - tol)], deadline)
    if st2 in ("optimal", "feasible") and x2 is not None:
        W2 = sum(w[i] for i in range(comp.n) if x2[i])
        if abs(W2 - W) <= tol * 10:
            return CompResult(status="optimal", x=x2, obj=W2, tie_broken=(st2 == "optimal"))
    return CompResult(status="optimal", x=x, obj=W, tie_broken=False)


# ---------------------------------------------------------------- 分量求解入口

def _solve_comp(comp, deadline):
    if not comp.conflicts and not comp.fk_groups:
        # 无约束分量：解析解（非负权重全留，必留必留）
        x = [1 if (comp.w[i] >= 0 or i in comp.must) else 0 for i in range(comp.n)]
        return CompResult(status="optimal", x=x,
                          obj=sum(comp.w[i] for i in range(comp.n) if x[i]),
                          tie_broken=True)
    if HAVE_SCIPY:
        return _solve_comp_scipy(comp, deadline)
    return _solve_comp_bnb(comp, deadline)


# ---------------------------------------------------------------- 穷举校对 / 等价解计数

def brute_comp(comp, max_examples=6):
    """对分量穷举全部子集，返回 (最优权重, 最优解个数, 最优解示例[mask])。
    孤立行（无冲突、非外键双方、非必留）解析处理：正权必留、负权必弃、
    零权使最优解数翻倍。"""
    n = comp.n
    constrained = set(comp.must)
    for i, j in comp.conflicts:
        constrained.add(i)
        constrained.add(j)
    for c, groups in comp.fk_groups.items():
        constrained.add(c)
        for g in groups:
            constrained.update(g)
    free = [i for i in range(n) if i not in constrained]
    core = sorted(constrained)
    base_w = 0.0
    zero_free = 0
    free_in = []
    for i in free:
        if comp.w[i] > TOL:
            base_w += comp.w[i]
            free_in.append(i)
        elif abs(comp.w[i]) <= TOL:
            zero_free += 1
    core_conf = [(i, j) for (i, j) in comp.conflicts]
    core_fk = {c: groups for c, groups in comp.fk_groups.items()}
    must_core = [i for i in core if i in comp.must]

    best = float("-inf")
    count = 0
    examples = []
    tol = TOL * 8
    for mask_bits in range(1 << len(core)):
        chosen = set()
        ok = True
        for k, i in enumerate(core):
            if (mask_bits >> k) & 1:
                chosen.add(i)
        if any(i not in chosen for i in must_core):
            continue
        for (i, j) in core_conf:
            if i in chosen and j in chosen:
                ok = False
                break
        if not ok:
            continue
        for c, groups in core_fk.items():
            if c not in chosen:
                continue
            for g in groups:
                if not any((p in chosen) or (p in free_in) for p in g):
                    ok = False
                    break
            if not ok:
                break
        if not ok:
            continue
        wsum = base_w + sum(comp.w[i] for i in chosen)
        if wsum > best + tol:
            best = wsum
            count = 1
            examples = [chosen]
        elif abs(wsum - best) <= tol:
            count += 1
            if len(examples) < max_examples:
                examples.append(chosen)
    if best == float("-inf"):
        return None, 0, []
    total = count * (2 ** zero_free)
    full_examples = []
    for ch in examples:
        mask = set(ch) | set(free_in)
        full_examples.append(mask)
    return best, total, full_examples


# ---------------------------------------------------------------- 顶层求解

def solve(inst, graph, must_keep, time_limit=10.0, brute_max=BRUTE_MAX):
    """联合求解全部连通分量，返回 SolveResult。"""
    t0 = time.monotonic()
    deadline = t0 + time_limit if time_limit else None
    weight_of = {r.rid: r.weight for r in inst.rows}
    rank_of = {r.rid: k for k, r in enumerate(inst.rows)}
    must = set(must_keep)
    unknown = must - set(rank_of)
    if unknown:
        raise ValueError(f"必留行不存在: {sorted(unknown)}")
    if must & set(graph.forced_out):
        return SolveResult(status="infeasible",
                           note="必留行包含被强制隔离的行（空主键/孤儿）")

    result = SolveResult(status="optimal")
    comp_results = []
    comps = []
    for comp_rids in graph.components:
        cset = set(comp_rids)
        pairs = [(a, b) for (a, b) in graph.conflict_reasons if a in cset]
        fkg = {}
        for rid in comp_rids:
            groups = [[m for m in need["matches"] if m not in graph.forced_out]
                      for need in graph.fk_needs.get(rid, [])]
            groups = [g for g in groups if g]
            if groups:
                fkg[rid] = groups
        comp = Comp(comp_rids, rank_of, weight_of, pairs, fkg, must & cset)
        comps.append(comp)
        cr = _solve_comp(comp, deadline)
        comp_results.append((comp, cr))
        if cr.status == "infeasible":
            result.status = "infeasible"
            result.note = "必留约束相互冲突或与其依赖冲突"
            return result
        if cr.status == "no_feasible":
            if comp.must:
                result.status = "no_feasible"
                result.note = "求解超时且未找到满足必留约束的可行解"
                return result
            cr.x = [0] * comp.n  # 空集必可行
            cr.status = "feasible"

    total_w = 0.0
    total_ub = 0.0
    any_unproven = False
    for comp, cr in comp_results:
        kept_local = {comp.rids[i] for i in range(comp.n) if cr.x and cr.x[i]}
        result.kept |= kept_local
        w = sum(weight_of[r] for r in kept_local)
        total_w += w
        if cr.status != "optimal":
            any_unproven = True
            total_ub += cr.bound if cr.bound is not None else float("inf")
        else:
            total_ub += w
        result.comp_details.append({
            "key": comp.rids[0], "size": comp.n, "weight": w,
            "status": cr.status,
            "bound": cr.bound if cr.status != "optimal" else w,
        })
    result.weight = total_w
    result.status = "feasible" if any_unproven else "optimal"
    if any_unproven:
        result.bound = total_ub if total_ub != float("inf") else None
        if result.bound is not None:
            result.gap = (result.bound - total_w) / max(1.0, abs(total_w))

    # 穷举校对 + 等价解计数（小分量精确；大分量用 ILP 排除约束探测存在性）
    check = {"ok": True, "details": [], "skipped": 0}
    alt_count = 1
    alt_known = True
    alt_examples = []
    for comp, cr in comp_results:
        canonical = {i for i in range(comp.n) if cr.x and cr.x[i]}
        if comp.n <= brute_max:
            bw, cnt, ex = brute_comp(comp)
            comp_w = sum(comp.w[i] for i in canonical)
            ok = bw is not None and abs(bw - comp_w) <= TOL * 10 * max(1.0, abs(bw))
            check["details"].append({
                "component": comp.rids[0], "size": comp.n,
                "solver": comp_w, "brute": bw, "ok": ok})
            if not ok:
                check["ok"] = False
            alt_count *= cnt
            if cnt > 1 and len(alt_examples) < 5:
                canon_rids = {comp.rids[i] for i in canonical}
                for mask in ex:
                    alt_rids = {comp.rids[i] for i in mask}
                    if alt_rids != canon_rids:
                        swapped = (result.kept - canon_rids) | alt_rids
                        alt_examples.append(sorted(swapped))
                        break
        else:
            check["skipped"] += 1
            if HAVE_SCIPY:
                ex = _alternates_scipy(comp, cr, deadline)
                if ex is None:
                    alt_known = False
                elif ex:
                    alt_known = False  # 存在等价解但数量未知
                    canon_rids = {comp.rids[i] for i in canonical}
                    for alt in ex:
                        if len(alt_examples) >= 5:
                            break
                        alt_rids = {comp.rids[i] for i, v in enumerate(alt) if v}
                        swapped = (result.kept - canon_rids) | alt_rids
                        alt_examples.append(sorted(swapped))
                # ex == []：该分量最优解唯一
            else:
                alt_known = False
    result.crosscheck = check
    result.alternates_count = alt_count if alt_known else None
    result.alternates_examples = alt_examples[:5]
    result.seconds = time.monotonic() - t0
    return result


def _alternates_scipy(comp, cr, deadline, max_examples=5):
    """大分量的等价最优解探测：逐次加入排除约束。
    返回 None=未知（超时），[]=唯一，[x...]=等价解示例。"""
    if cr.status != "optimal":
        return None
    n = comp.n
    w = comp.w
    W = sum(w[i] for i in range(n) if cr.x[i])
    tol = TOL * max(1.0, abs(W))
    rows_a, rows_lb, rows_ub = [], [], []
    for (i, j) in comp.conflicts:
        row = [0.0] * n
        row[i] = row[j] = 1.0
        rows_a.append(row)
        rows_lb.append(-_np.inf)
        rows_ub.append(1.0)
    for c, groups in comp.fk_groups.items():
        for g in groups:
            row = [0.0] * n
            row[c] = 1.0
            for p in g:
                row[p] = -1.0
            rows_a.append(row)
            rows_lb.append(-_np.inf)
            rows_ub.append(0.0)
    row = list(w)
    rows_a.append(row)
    rows_lb.append(W - tol)
    rows_ub.append(_np.inf)
    lb = _np.zeros(n)
    ub = _np.ones(n)
    for i in comp.must:
        lb[i] = ub[i] = 1.0

    examples = []
    seen = []
    for _ in range(max_examples + 1):
        A = list(rows_a)
        lo = list(rows_lb)
        hi = list(rows_ub)
        for s in seen:
            rowx = [0.0] * n
            for i in range(n):
                rowx[i] = 1.0 if s[i] else -1.0
            A.append(rowx)
            lo.append(-_np.inf)
            hi.append(sum(s) - 1.0)
        tl = max(0.0, deadline - time.monotonic()) if deadline else None
        res = _milp(c=-_np.array(w), constraints=[_LC(_np.array(A), _np.array(lo), _np.array(hi))],
                    integrality=_np.ones(n), bounds=_B(lb, ub),
                    options={"time_limit": tl, "mip_rel_gap": 0.0, "disp": False})
        if res.status == 2:
            break
        if res.status not in (0, 1) or res.x is None:
            return None
        x = [1 if v >= 0.5 else 0 for v in res.x]
        examples.append(x)
        seen.append(x)
    return examples
