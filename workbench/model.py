# -*- coding: utf-8 -*-
"""数据模型与载入：业务表、约束、行身份、输入指纹。

固定语义（主键与空值，一经确定不再变动）：
  * 空值 = CSV 中的空字符串。
  * 每表一个单列主键；主键为空的候选行一律强制隔离。
  * 同表主键值重复的行两两冲突（不可同时保留）。
  * 函数依赖 det -> dep（均可多列）：det 含空值的行不参与该 FD；
    其余行 det 相同则 dep 元组必须完全一致（空值按普通值参与比较）。
  * 单列外键：子行外键列为空 -> 无引用要求；非空 -> 至少保留一行
    父表主键等于该值的父行；无任何匹配父候选 -> 孤儿行，强制隔离。
  * 行身份 = (表名, 源文件数据行号)，与单元格内容无关，用于平局裁决。
"""
import csv
import hashlib
import json
import os
from dataclasses import dataclass, field

NULL = ""
WEIGHT_COL = "weight"


class LoadError(Exception):
    pass


@dataclass
class Row:
    rid: str            # "table#line"，line 为 1 起始的数据行号
    table: str
    line: int
    cells: dict         # 原始单元格（含 weight 列），内容永不被修改
    weight: float
    pk: str             # 主键值（可能为空串）

    def label(self):
        return f"{self.table}:{self.pk or '∅'}(行{self.line})"


@dataclass
class Table:
    name: str
    pk_col: str
    columns: list
    rows: list = field(default_factory=list)


@dataclass
class Instance:
    tables: dict        # name -> Table
    constraints: dict   # constraints.json 原文
    rows: list          # 全部 Row，载入顺序即固定行身份顺序
    fingerprint: str
    source_dir: str


def _read_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        columns = reader.fieldnames or []
        rows = [dict(r) for r in reader]
    return columns, rows


def load_instance(data_dir):
    """从目录载入 constraints.json 与各业务表，返回 Instance。失败抛 LoadError。"""
    errors = []
    cpath = os.path.join(data_dir, "constraints.json")
    try:
        with open(cpath, encoding="utf-8") as f:
            constraints = json.load(f)
    except FileNotFoundError:
        raise LoadError(f"缺少约束文件: {cpath}")
    except json.JSONDecodeError as e:
        raise LoadError(f"constraints.json 不是合法 JSON: {e}")

    tables = {}
    all_rows = []
    for tspec in constraints.get("tables", []):
        name = tspec.get("name")
        pk_col = tspec.get("pk")
        fname = tspec.get("file")
        if not name or not pk_col or not fname:
            errors.append(f"表声明缺少 name/pk/file: {tspec}")
            continue
        fpath = os.path.join(data_dir, fname)
        try:
            columns, raw_rows = _read_csv(fpath)
        except FileNotFoundError:
            errors.append(f"表 {name}: 找不到文件 {fname}")
            continue
        if pk_col not in columns:
            errors.append(f"表 {name}: 主键列 {pk_col} 不在文件列中 {columns}")
            continue
        if WEIGHT_COL not in columns:
            errors.append(f"表 {name}: 缺少保留权重列 {WEIGHT_COL}")
            continue
        table = Table(name=name, pk_col=pk_col, columns=list(columns))
        for i, raw in enumerate(raw_rows, start=1):
            wtxt = (raw.get(WEIGHT_COL) or "").strip()
            try:
                weight = float(wtxt)
            except ValueError:
                errors.append(f"表 {name} 第 {i} 行: 权重 {wtxt!r} 不是数值")
                continue
            cells = {c: (raw.get(c) or "") for c in columns}
            row = Row(rid=f"{name}#{i}", table=name, line=i,
                      cells=cells, weight=weight, pk=cells.get(pk_col, NULL))
            table.rows.append(row)
            all_rows.append(row)
        tables[name] = table

    # 约束引用校验
    for fd in constraints.get("fds", []):
        t = tables.get(fd.get("table"))
        if t is None:
            errors.append(f"FD 引用了未声明的表: {fd.get('table')}")
            continue
        for col in fd.get("det", []) + fd.get("dep", []):
            if col not in t.columns:
                errors.append(f"FD({fd.get('label', '')}) 列 {col} 不在表 {t.name} 中")
    for fk in constraints.get("fks", []):
        ct = tables.get(fk.get("child_table"))
        pt = tables.get(fk.get("parent_table"))
        if ct is None or pt is None:
            errors.append(f"FK 引用了未声明的表: {fk}")
            continue
        if fk.get("child_col") not in ct.columns:
            errors.append(f"FK 子列 {fk.get('child_col')} 不在表 {ct.name} 中")
        if fk.get("parent_col") not in pt.columns:
            errors.append(f"FK 父列 {fk.get('parent_col')} 不在表 {pt.name} 中")
        if fk.get("parent_col") != pt.pk_col:
            errors.append(f"FK({fk.get('label', '')}) 父列必须引用父表主键 {pt.pk_col}")

    if errors:
        raise LoadError("；".join(errors))

    fp = fingerprint(tables, constraints)
    return Instance(tables=tables, constraints=constraints, rows=all_rows,
                    fingerprint=fp, source_dir=data_dir)


def fingerprint(tables, constraints):
    """输入指纹：行身份 + 单元格 + 权重 + 约束 的 SHA-256。
    修改权重或约束必然改变指纹，从而使已生成的候选失效。"""
    payload = {
        "tables": {
            name: {
                "pk": t.pk_col,
                "columns": t.columns,
                "rows": [[r.cells.get(c, NULL) for c in t.columns] for r in t.rows],
            } for name, t in sorted(tables.items())
        },
        "constraints": constraints,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()
