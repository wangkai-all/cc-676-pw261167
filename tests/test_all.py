# -*- coding: utf-8 -*-
"""端到端与单元测试。运行: python3 tests/test_all.py （有 scipy 时会额外校验 milp 后端）"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "workbench"))

import explain
import graph as graph_mod
import model
import publisher
import solver
from solver import Comp, _solve_comp, _solve_comp_bnb, brute_comp, HAVE_SCIPY

sys.path.insert(0, os.path.join(ROOT, "workbench"))
import verifier  # noqa: E402  独立校验器（仅借用其 verify 函数做断言）

DATA = os.path.join(ROOT, "data")


def load_demo():
    inst = model.load_instance(DATA)
    return inst, graph_mod.Graph(inst)


class DemoSolveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inst, cls.g = load_demo()
        cls.res = solver.solve(cls.inst, cls.g, [], time_limit=30.0)

    def test_optimal_and_weight(self):
        self.assertEqual(self.res.status, "optimal")
        self.assertAlmostEqual(self.res.weight, 114.0, places=6)

    def test_expected_quarantine_set(self):
        kept = self.res.kept
        expected_out = {"categories#2", "categories#4", "products#3", "products#5",
                        "products#13", "order_items#4", "order_items#6", "order_items#14"}
        actual_out = {r.rid for r in self.inst.rows} - kept
        self.assertEqual(actual_out, expected_out)

    def test_orphan_forced_out(self):
        self.assertIn("order_items#14", self.g.forced_out)
        self.assertNotIn("order_items#14", self.res.kept)

    def test_tiebreak_canonical(self):
        # P001 两行权重相同（8），规范解保留行身份靠前者 products#1
        self.assertIn("products#1", self.res.kept)
        self.assertNotIn("products#13", self.res.kept)
        # 等价最优解存在且数量为 2，另一个方案被保留展示
        self.assertEqual(self.res.alternates_count, 2)
        self.assertEqual(len(self.res.alternates_examples), 1)
        alt = set(self.res.alternates_examples[0])
        self.assertIn("products#13", alt)
        self.assertNotIn("products#1", alt)

    def test_crosscheck_passes(self):
        self.assertTrue(self.res.crosscheck["ok"], self.res.crosscheck)
        self.assertEqual(self.res.crosscheck["skipped"], 0)

    def test_deterministic(self):
        res2 = solver.solve(self.inst, self.g, [], time_limit=30.0)
        self.assertEqual(self.res.kept, res2.kept)

    def test_explanations(self):
        ex = explain.explain_quarantine(self.inst, self.g, self.res.kept)
        self.assertIn("孤儿", ex["order_items#14"]["reasons"][0])
        # products#3 (P003) 需要 C02，而 C02 与保留的 C01 冲突 -> 链式原因
        self.assertTrue(ex["products#3"]["blocks"] or ex["products#3"]["needs"])
        # products#13 与保留的 products#1 主键冲突
        blocks = ex["products#13"]["blocks"]
        self.assertEqual([b["rid"] for b in blocks], ["products#1"])


class MustKeepTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inst, cls.g = load_demo()

    def test_mustkeep_changes_plan(self):
        res = solver.solve(self.inst, self.g, ["products#5"], time_limit=30.0)
        self.assertEqual(res.status, "optimal")
        self.assertIn("products#5", res.kept)
        self.assertNotIn("products#4", res.kept)   # FD 冲突让位
        self.assertNotIn("order_items#5", res.kept)  # L005 失去父行
        self.assertIn("order_items#6", res.kept)     # L006 随 P005 入仓

    def test_mustkeep_cascade(self):
        res = solver.solve(self.inst, self.g, ["categories#2"], time_limit=30.0)
        self.assertEqual(res.status, "optimal")
        self.assertAlmostEqual(res.weight, 98.0, places=6)
        self.assertIn("products#3", res.kept)
        self.assertNotIn("categories#1", res.kept)
        self.assertNotIn("products#1", res.kept)

    def test_infeasible_conflict_pair(self):
        res = solver.solve(self.inst, self.g,
                           ["categories#1", "categories#2"], time_limit=30.0)
        self.assertEqual(res.status, "infeasible")
        diag = explain.diagnose_infeasible(
            self.inst, self.g, ["categories#1", "categories#2"],
            lambda sub: solver.solve(self.inst, self.g, sub, time_limit=30.0).status != "infeasible")
        self.assertEqual(sorted(diag["core"]), ["categories#1", "categories#2"])
        self.assertTrue(any(f["type"] == "conflict_pair" for f in diag["findings"]))

    def test_infeasible_orphan_mustkeep(self):
        res = solver.solve(self.inst, self.g, ["order_items#14"], time_limit=30.0)
        self.assertEqual(res.status, "infeasible")
        diag = explain.diagnose_infeasible(
            self.inst, self.g, ["order_items#14"],
            lambda sub: solver.solve(self.inst, self.g, sub, time_limit=30.0).status != "infeasible")
        self.assertEqual(diag["core"], ["order_items#14"])
        self.assertTrue(any(f["type"] == "forced_out" for f in diag["findings"]))


def random_comp(rng, n):
    rids = [f"t#{i}" for i in range(n)]
    rank_of = {r: i for i, r in enumerate(rids)}
    weight_of = {r: rng.choice([0, 1, 2, 3, 5, 8]) for r in rids}
    pairs = []
    for i in range(n):
        for j in range(i + 1, n):
            if rng.random() < 0.18:
                pairs.append((rids[i], rids[j]))
    fkg = {}
    for _ in range(rng.randint(0, max(1, n // 3))):
        c = rng.randrange(n)
        groups = []
        for _ in range(rng.randint(1, 2)):
            g = [rng.randrange(n) for _ in range(rng.randint(1, 2))]
            g = [p for p in g if p != c]
            if g:
                groups.append([rids[p] for p in g])
        if groups:
            fkg.setdefault(rids[c], []).extend(groups)
    must = {rids[i] for i in range(n) if rng.random() < 0.08}
    return Comp(rids, rank_of, weight_of, pairs, fkg, must)


class RandomCrossCheckTest(unittest.TestCase):
    """随机小实例：两个求解后端分别与穷举对拍。"""

    def check_one(self, comp):
        bw, cnt, _ = brute_comp(comp)
        for solve_fn in ([_solve_comp, _solve_comp_bnb] if HAVE_SCIPY else [_solve_comp_bnb]):
            cr = solve_fn(comp, None)
            if bw is None:
                self.assertEqual(cr.status, "infeasible")
                continue
            self.assertEqual(cr.status, "optimal")
            got = sum(comp.w[i] for i in range(comp.n) if cr.x[i])
            self.assertAlmostEqual(got, bw, places=6)
            for i in comp.must:
                self.assertEqual(cr.x[i], 1)
            for (i, j) in comp.conflicts:
                self.assertFalse(cr.x[i] and cr.x[j])
            for c, groups in comp.fk_groups.items():
                if cr.x[c]:
                    for g in groups:
                        self.assertTrue(any(cr.x[p] for p in g))

    def test_random_small(self):
        rng = random.Random(20261122)
        for _ in range(40):
            self.check_one(random_comp(rng, rng.randint(2, 12)))


class TimeoutContractTest(unittest.TestCase):
    """超时路径：只允许声称 feasible/no_feasible；可行解必须附带不小于其权重的上界。"""

    def test_tiny_deadline(self):
        rng = random.Random(7)
        comp = random_comp(rng, 1)  # 占位，下面构造真正的大分量
        n = 90
        rids = [f"t#{i}" for i in range(n)]
        rank_of = {r: i for i, r in enumerate(rids)}
        weight_of = {r: rng.randint(1, 10) for r in rids}
        pairs = []
        for i in range(n):
            for j in range(i + 1, n):
                if rng.random() < 0.12:
                    pairs.append((rids[i], rids[j]))
        big = Comp(rids, rank_of, weight_of, pairs, {}, set())
        import time
        cr = _solve_comp(big, time.monotonic() + 0.05)
        self.assertIn(cr.status, ("optimal", "feasible", "no_feasible"))
        if cr.status == "feasible":
            self.assertIsNotNone(cr.x)
            if cr.bound is not None:
                self.assertGreaterEqual(cr.bound + 1e-6, cr.obj)
        if cr.status == "optimal":  # 机器够快时也必须自洽
            bw_check = sum(big.w[i] for i in range(big.n) if cr.x[i])
            self.assertAlmostEqual(bw_check, cr.obj, places=6)


class VerifierTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.inst, cls.g = load_demo()
        cls.res = solver.solve(cls.inst, cls.g, [], time_limit=30.0)

    def _candidate(self, kept):
        return {"fingerprint": self.inst.fingerprint, "must_keep": [],
                "kept": sorted(kept), "weight": sum(r.weight for r in self.inst.rows
                                                    if r.rid in kept)}

    def test_good_candidate_passes(self):
        rep = verifier.verify(DATA, self._candidate(self.res.kept))
        self.assertTrue(rep["ok"], rep["violations"])

    def test_tampered_drop_parent_fails(self):
        bad = set(self.res.kept)
        bad.discard("products#4")  # L005/L016 的父行
        rep = verifier.verify(DATA, self._candidate(bad))
        self.assertFalse(rep["ok"])
        self.assertTrue(any("FK" in v for v in rep["violations"]))

    def test_tampered_add_conflicting_fails(self):
        bad = set(self.res.kept)
        bad.add("categories#2")  # 与 categories#1 FD 冲突
        rep = verifier.verify(DATA, self._candidate(bad))
        self.assertFalse(rep["ok"])

    def test_wrong_weight_fails(self):
        cand = self._candidate(self.res.kept)
        cand["weight"] += 1.0
        rep = verifier.verify(DATA, cand)
        self.assertFalse(rep["ok"])

    def test_stale_fingerprint_fails(self):
        cand = self._candidate(self.res.kept)
        cand["fingerprint"] = "0" * 64
        rep = verifier.verify(DATA, cand)
        self.assertFalse(rep["ok"])

    def test_cli_subprocess(self):
        cand = self._candidate(self.res.kept)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cand, f)
            path = f.name
        try:
            proc = subprocess.run(
                [sys.executable, "-I", os.path.join(ROOT, "workbench", "verifier.py"),
                 DATA, path], capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
            self.assertTrue(json.loads(proc.stdout)["ok"])
        finally:
            os.unlink(path)


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.inst, self.g = load_demo()
        self.res = solver.solve(self.inst, self.g, [], time_limit=30.0)
        self.tmp = tempfile.mkdtemp(prefix="releases_")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.cand = {
            "fingerprint": self.inst.fingerprint, "must_keep": [],
            "status": self.res.status, "backend": self.res.backend,
            "weight": self.res.weight, "bound": self.res.bound,
            "gap": self.res.gap, "kept": sorted(self.res.kept),
            "alternates_count": self.res.alternates_count,
        }
        self.reasons = explain.explain_quarantine(self.inst, self.g, self.res.kept)
        self.verifier_path = os.path.join(ROOT, "workbench", "verifier.py")

    def test_publish_writes_release(self):
        m = publisher.publish(self.inst, self.cand, self.reasons, self.tmp,
                              self.verifier_path)
        self.assertEqual(m["version"], 1)
        self.assertTrue(m["verifier_ok"])
        wdir = os.path.join(self.tmp, "v1", "warehouse")
        qdir = os.path.join(self.tmp, "v1", "quarantine")
        for t in ("categories", "products", "order_items"):
            self.assertTrue(os.path.isfile(os.path.join(wdir, t + ".csv")))
            self.assertTrue(os.path.isfile(os.path.join(qdir, t + ".csv")))
        with open(os.path.join(wdir, "products.csv"), encoding="utf-8") as f:
            body = f.read()
        self.assertIn("P001", body)
        self.assertNotIn("P005", body)
        with open(os.path.join(qdir, "order_items.csv"), encoding="utf-8") as f:
            qbody = f.read()
        self.assertIn("quarantine_reason", qbody)
        self.assertIn("孤儿", qbody)
        # 再发布一次 -> v2，v1 保留
        m2 = publisher.publish(self.inst, self.cand, self.reasons, self.tmp,
                               self.verifier_path)
        self.assertEqual(m2["version"], 2)
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "v1")))
        self.assertTrue(os.path.isdir(os.path.join(self.tmp, "v2")))

    def test_stale_candidate_rejected(self):
        tmp_data = tempfile.mkdtemp(prefix="data_")
        self.addCleanup(shutil.rmtree, tmp_data)
        for fn in os.listdir(DATA):
            shutil.copy(os.path.join(DATA, fn), os.path.join(tmp_data, fn))
        # 修改权重 -> 指纹变化 -> 旧候选失效
        p = os.path.join(tmp_data, "products.csv")
        with open(p, encoding="utf-8") as f:
            txt = f.read()
        txt = txt.replace("P002,香蕉,C01,王五,5", "P002,香蕉,C01,王五,9")
        with open(p, "w", encoding="utf-8") as f:
            f.write(txt)
        inst2 = model.load_instance(tmp_data)
        self.assertNotEqual(inst2.fingerprint, self.inst.fingerprint)
        with self.assertRaises(publisher.StaleCandidate):
            publisher.publish(inst2, self.cand, self.reasons, self.tmp,
                              self.verifier_path)
        # 约束修改同样使候选失效
        tmp_data2 = tempfile.mkdtemp(prefix="data_")
        self.addCleanup(shutil.rmtree, tmp_data2)
        for fn in os.listdir(DATA):
            shutil.copy(os.path.join(DATA, fn), os.path.join(tmp_data2, fn))
        with open(os.path.join(tmp_data2, "constraints.json"), encoding="utf-8") as f:
            cons = json.load(f)
        cons["fds"] = cons["fds"][:1]
        with open(os.path.join(tmp_data2, "constraints.json"), "w", encoding="utf-8") as f:
            json.dump(cons, f, ensure_ascii=False)
        inst3 = model.load_instance(tmp_data2)
        self.assertNotEqual(inst3.fingerprint, self.inst.fingerprint)


class FingerprintTest(unittest.TestCase):
    def test_rows_and_constraints_covered(self):
        inst, _ = load_demo()
        self.assertEqual(len(inst.fingerprint), 64)
        inst2, _ = load_demo()
        self.assertEqual(inst.fingerprint, inst2.fingerprint)  # 同输入同指纹


if __name__ == "__main__":
    unittest.main(verbosity=2)
