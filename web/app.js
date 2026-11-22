/* 入仓修复工作台前端 */
"use strict";

const S = {
  state: null,
  mustKeep: new Set(),
  candidate: null,
  dirty: false,        // 必留选择在求解后被修改
  selected: null,
  tab: "graph",
  altIndex: -1,        // 正在查看的等价方案下标（-1 = 规范解）
};

const TABLE_COLORS = ["var(--series-1)", "var(--series-2)", "var(--series-3)"];
let tableColor = {};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtW = (w) => (Math.round(w * 1e6) / 1e6).toString();

async function api(path, body) {
  const opt = body === undefined ? {} : {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  };
  const r = await fetch(path, opt);
  const j = await r.json();
  if (!r.ok) { const e = new Error(j.error || r.statusText); e.payload = j; e.status = r.status; throw e; }
  return j;
}

function rowOf(rid) {
  const [t, line] = rid.split("#");
  const tab = S.state.tables.find((x) => x.name === t);
  return tab ? tab.rows[Number(line) - 1] : null;
}
function labelOf(rid) {
  const r = rowOf(rid);
  return r ? `${r.table}:${r.pk || "∅"}(行${r.line})` : rid;
}

/* ================= 状态载入 ================= */

async function loadState(reload) {
  const st = reload ? await api("/api/reload", {}) : await api("/api/state");
  if (st.ok === false) {
    document.querySelector("main").innerHTML =
      `<div class="alert err">数据载入失败：${esc(st.load_error)}</div>`;
    return;
  }
  if (S.state && S.candidate && st.fingerprint !== S.state.fingerprint) {
    // 数据已变化：旧候选失效
    S.candidate.stale = true;
  }
  S.state = st;
  S.state.tables.forEach((t, i) => { tableColor[t.name] = TABLE_COLORS[i % TABLE_COLORS.length]; });
  renderAll();
}

/* ================= 渲染：总 ================= */

function renderAll() {
  $("backend").textContent = "求解后端 " + S.state.backend;
  $("fingerprint").textContent = "指纹 " + S.state.fingerprint.slice(0, 12) + "…";
  $("fingerprint").title = "输入指纹 " + S.state.fingerprint + "（修改权重或约束会使已生成候选失效）";
  renderLegend();
  renderGraph();
  renderRowDetail();
  renderPlan();
  renderPublish();
  updateMustkeepCount();
}

function updateMustkeepCount() {
  $("mustkeep-count").textContent = `必留 ${S.mustKeep.size} 行`;
}

/* ================= 渲染：图例 ================= */

function renderLegend() {
  const items = S.state.tables.map((t, i) =>
    `<span class="item"><span class="sw" style="background:${tableColor[t.name]}"></span>${esc(t.name)}</span>`);
  items.push(`<span class="item"><svg class="ln" width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" class="edge-conflict"/></svg>冲突（FD/主键）</span>`);
  items.push(`<span class="item"><svg class="ln" width="22" height="6"><line x1="0" y1="3" x2="22" y2="3" class="edge-fk"/></svg>外键依赖（子→父）</span>`);
  items.push(`<span class="item"><span class="status-pill kept">✓ 保留</span></span>`);
  items.push(`<span class="item"><span class="status-pill out">✗ 隔离</span></span>`);
  items.push(`<span class="item">★ 必留</span>`);
  items.push(`<span class="item">⚠ 强制隔离（空主键/孤儿）</span>`);
  $("legend").innerHTML = items.join("");
}

/* ================= 渲染：冲突图谱 ================= */

const COL_W = 200, ROW_H = 46, TOP = 40, LEFT = 24, NODE_W = 158, NODE_H = 34;

function nodeStatus(rid) {
  const forced = S.state.graph.forced_out[rid];
  if (S.candidate && !S.dirty && !S.candidate.stale && S.candidate.status !== "infeasible") {
    if (S.candidate.kept.includes(rid)) return forced ? "forced" : "kept";
    return forced ? "forced" : "out";
  }
  return forced ? "forced" : "idle";
}

function renderGraph() {
  const svg = $("graph");
  const tables = S.state.tables;
  const pos = {};
  let maxRows = 0;
  tables.forEach((t, ci) => {
    maxRows = Math.max(maxRows, t.rows.length);
    t.rows.forEach((r, ri) => {
      pos[r.rid] = { x: LEFT + ci * COL_W, y: TOP + ri * ROW_H };
    });
  });
  const W = LEFT * 2 + tables.length * COL_W;
  const H = TOP + maxRows * ROW_H + 20;
  svg.setAttribute("width", W);
  svg.setAttribute("height", H);
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);

  const parts = [];
  parts.push(`<defs><marker id="arr" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
    <path d="M0,0 L8,4 L0,8 z" fill="var(--text-muted)"/></marker></defs>`);

  // 表标题
  tables.forEach((t, ci) => {
    parts.push(`<text class="col-title" x="${LEFT + ci * COL_W}" y="22">${esc(t.name)}（主键 ${esc(t.pk)}）</text>`);
  });

  const center = (rid) => {
    const p = pos[rid];
    return { x: p.x + NODE_W / 2, y: p.y + NODE_H / 2 };
  };

  // 边：先画（压在节点下）
  const sel = S.selected;
  for (const e of S.state.graph.conflicts) {
    if (!pos[e.a] || !pos[e.b]) continue;
    const a = center(e.a), b = center(e.b);
    const hl = sel && (e.a === sel || e.b === sel) ? " edge-hl" : "";
    parts.push(`<line class="edge-conflict${hl}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}">
      <title>${esc(e.reasons.join("\n"))}</title></line>`);
  }
  for (const f of S.state.graph.fk) {
    if (!pos[f.child]) continue;
    const a = center(f.child);
    for (const m of f.matches) {
      if (!pos[m]) continue;
      const b = center(m);
      const hl = sel && (f.child === sel || m === sel) ? " edge-hl" : "";
      parts.push(`<line class="edge-fk${hl}" x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}" marker-end="url(#arr)">
        <title>${esc(f.fk)}（值 ${esc(f.value)}）</title></line>`);
    }
  }

  // 节点
  for (const t of tables) {
    for (const r of t.rows) {
      const p = pos[r.rid];
      const st = nodeStatus(r.rid);
      const fill = st === "kept" ? "var(--node-kept)" : st === "out" || st === "forced" ? "var(--node-out)" : "var(--surface-1)";
      const selCls = S.selected === r.rid ? " selected" : "";
      const must = S.mustKeep.has(r.rid);
      const forced = S.state.graph.forced_out[r.rid];
      parts.push(`<g class="node${selCls}" data-rid="${esc(r.rid)}" transform="translate(${p.x},${p.y})">
        <title>${esc(r.table)} 行${r.line} · 权重 ${fmtW(r.weight)}${forced ? "\n⚠ " + forced : ""}</title>
        <rect class="body" width="${NODE_W}" height="${NODE_H}" rx="4"
              fill="${fill}" stroke="${tableColor[t.name]}"/>
        <text class="pk" x="8" y="14">${esc(r.pk || "∅（空主键）")}</text>
        <text class="wt" x="8" y="27">w=${fmtW(r.weight)} · 行${r.line}</text>
        ${must ? `<text x="${NODE_W - 16}" y="14" font-size="12" fill="var(--series-2)">★</text>` : ""}
        ${forced ? `<text x="${NODE_W - 16}" y="27" font-size="11" fill="var(--serious)">⚠</text>` : ""}
        ${st === "kept" ? `<text x="${NODE_W - 30}" y="14" font-size="11" fill="var(--good-ink)">✓</text>` : ""}
        ${st === "out" ? `<text x="${NODE_W - 30}" y="14" font-size="11" fill="var(--critical)">✗</text>` : ""}
      </g>`);
    }
  }
  svg.innerHTML = parts.join("");
  svg.querySelectorAll(".node").forEach((g) => {
    g.addEventListener("click", () => {
      S.selected = g.getAttribute("data-rid");
      renderGraph();
      renderRowDetail();
    });
  });
}

/* ================= 渲染：行详情 ================= */

function renderRowDetail() {
  const el = $("row-detail");
  const rid = S.selected;
  if (!rid) { el.innerHTML = "<h2>行详情</h2><p class='muted'>点击图中节点查看</p>"; return; }
  const r = rowOf(rid);
  if (!r) { el.innerHTML = "<h2>行详情</h2><p class='muted'>行不存在</p>"; return; }
  const g = S.state.graph;
  const forced = g.forced_out[rid];
  const conflicts = g.conflicts.filter((e) => e.a === rid || e.b === rid);
  const needs = g.fk.filter((f) => f.child === rid);
  const children = g.fk.filter((f) => f.matches.includes(rid));
  const must = S.mustKeep.has(rid);

  const cells = Object.entries(r.cells)
    .map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v === "" ? "∅" : v)}</dd>`).join("");
  const confHtml = conflicts.length
    ? conflicts.map((e) => {
        const other = e.a === rid ? e.b : e.a;
        return `<div><span class="chip" data-rid="${esc(other)}">${esc(labelOf(other))}</span>
          <div class="muted">${esc(e.reasons[0])}</div></div>`;
      }).join("")
    : "<span class='muted'>无冲突边</span>";
  const needHtml = needs.length
    ? needs.map((n) => `<div>${esc(n.fk)} = <b>${esc(n.value)}</b> → 父候选 ${
        n.matches.map((m) => `<span class="chip" data-rid="${esc(m)}">${esc(labelOf(m))}</span>`).join("") || "（无）"
      }</div>`).join("")
    : "<span class='muted'>无外键引用</span>";
  const childHtml = children.length
    ? children.map((c) => `<span class="chip" data-rid="${esc(c.child)}">${esc(labelOf(c.child))}</span>`).join("")
    : "<span class='muted'>无子行引用本行</span>";

  el.innerHTML = `<h2>行详情 ${esc(r.rid)}</h2>
    <dl class="kv">${cells}</dl>
    ${forced ? `<div class="alert err">⚠ ${esc(forced)}</div>` : ""}
    <div class="row"><button id="btn-must" class="btn small">${must ? "取消必留 ★" : "标记必留 ☆"}</button>
    <span class="muted">权重 ${fmtW(r.weight)}</span></div>
    <h2>冲突（${conflicts.length}）</h2>${confHtml}
    <h2>外键依赖</h2>${needHtml}
    <h2>被引用（${children.length}）</h2>${childHtml}`;
  el.querySelectorAll(".chip").forEach((c) =>
    c.addEventListener("click", () => { S.selected = c.getAttribute("data-rid"); renderGraph(); renderRowDetail(); }));
  $("btn-must").addEventListener("click", () => {
    if (S.mustKeep.has(rid)) S.mustKeep.delete(rid); else S.mustKeep.add(rid);
    if (S.candidate) S.dirty = true;
    updateMustkeepCount();
    renderGraph();
    renderRowDetail();
    renderPlan();
    renderPublish();
  });
}

/* ================= 求解 ================= */

async function solve() {
  const btn = $("btn-solve");
  btn.disabled = true;
  $("solve-hint").textContent = "求解中…";
  try {
    const cand = await api("/api/solve", {
      must_keep: [...S.mustKeep],
      time_limit: Number($("time-limit").value) || 10,
    });
    S.candidate = cand;
    S.dirty = false;
    S.altIndex = -1;
    $("solve-hint").textContent = `耗时 ${cand.seconds}s · ${cand.backend}`;
    switchTab("plan");
  } catch (e) {
    $("solve-hint").textContent = "求解失败：" + e.message;
  } finally {
    btn.disabled = false;
    renderAll();
  }
}

/* ================= 渲染：方案 ================= */

function currentKeptSet() {
  if (!S.candidate) return null;
  if (S.altIndex >= 0 && S.candidate.alternates_examples[S.altIndex]) {
    return new Set(S.candidate.alternates_examples[S.altIndex]);
  }
  return new Set(S.candidate.kept);
}

function renderPlan() {
  const banner = $("plan-banner");
  const cand = S.candidate;
  if (!cand) {
    banner.innerHTML = `<div class="banner warn"><div class="big">尚未求解</div>
      <div class="sub">先在“① 冲突关系”页查看冲突，再点击“联合求解”。</div></div>`;
    $("kept-list").innerHTML = $("drop-list").innerHTML = "";
    $("kept-count").textContent = $("drop-count").textContent = "";
    $("why-panel").innerHTML = "<h2>为什么被舍弃</h2><p class='muted'>点击隔离清单中的一行</p>";
    return;
  }

  if (cand.status === "infeasible") {
    const d = cand.diagnosis || { findings: [], core: [] };
    banner.innerHTML = `<div class="banner bad"><div class="big">✗ 必留约束不可行</div>
      <div class="sub">不会悄悄取消必留。极小不可行核心：${d.core.map((r) => esc(labelOf(r))).join("、") || "（空）"}</div></div>`;
    const rows = d.findings.map((f) =>
      `<li>${esc(f.detail)}<div>${f.rows.map((r) => `<span class="chip" data-rid="${esc(r)}">${esc(labelOf(r))}</span>`).join("")}</div></li>`).join("");
    $("kept-list").innerHTML = "";
    $("drop-list").innerHTML = "";
    $("kept-count").textContent = $("drop-count").textContent = "";
    $("why-panel").innerHTML = `<h2>具体冲突</h2><ul class="reason-list">${rows}</ul>`;
    $("why-panel").querySelectorAll(".chip").forEach((c) =>
      c.addEventListener("click", () => { S.selected = c.getAttribute("data-rid"); switchTab("graph"); }));
    return;
  }

  const stale = cand.stale || (S.state && cand.fingerprint !== S.state.fingerprint);
  const dirty = S.dirty;
  const optimal = cand.status === "optimal";
  const cc = cand.crosscheck || { ok: false, details: [], skipped: 0 };
  const ccText = cc.ok
    ? `✓ 穷举校对通过（${cc.details.length} 个分量${cc.skipped ? `，${cc.skipped} 个过大跳过` : ""}）`
    : "✗ 穷举校对不一致！";
  let altText;
  if (cand.alternates_count === 1) altText = "最优解唯一（穷举确认）";
  else if (cand.alternates_count != null) altText = `共 ${cand.alternates_count} 个等价最优解，同权重平局已按固定行身份裁决，其他等价方案保留可查看`;
  else if (cand.alternates_examples.length) altText = "存在其他等价最优解（数量未精确计数），当前展示规范解";
  else altText = "等价最优解数量未知（存在过大分量，未穷举计数）";

  const statusLine = optimal
    ? `<div class="big">✓ 最优解（已证明） · 总权重 ${fmtW(cand.weight)}</div>`
    : `<div class="big">◐ 可行解（未证明最优） · 当前总权重 ${fmtW(cand.weight)}</div>
       <div class="sub">上界 ${cand.bound == null ? "未知" : fmtW(cand.bound)} · 差距 ${
         cand.gap == null ? "未知" : (cand.gap * 100).toFixed(1) + "%"} · 求解超时，未证明最优，不声称最优</div>`;

  banner.innerHTML = `<div class="banner ${optimal ? "good" : "warn"}">
    ${statusLine}
    <div class="sub">${esc(ccText)} · ${esc(altText)}</div>
    ${stale ? `<div class="alert err">⚠ 权重或约束已在求解后被修改，本候选已失效，请重新求解。</div>"` : ""}
    ${dirty ? `<div class="alert warn">⚠ 必留选择已变更，需重新求解。</div>` : ""}
    ${cand.alternates_examples.length ? `<div class="sub" id="alt-switch"></div>` : ""}
  </div>`;

  if (cand.alternates_examples.length) {
    const btns = [`<button class="btn small ${S.altIndex === -1 ? "primary" : ""}" data-alt="-1">规范解</button>`];
    cand.alternates_examples.forEach((_, i) =>
      btns.push(`<button class="btn small ${S.altIndex === i ? "primary" : ""}" data-alt="${i}">等价方案 ${i + 1}</button>`));
    $("alt-switch").innerHTML = "查看：" + btns.join(" ");
    $("alt-switch").querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", () => { S.altIndex = Number(b.getAttribute("data-alt")); renderPlan(); renderGraph(); }));
  }

  const keptSet = currentKeptSet();
  const byTable = {};
  S.state.tables.forEach((t) => { byTable[t.name] = { kept: [], out: [] }; });
  for (const t of S.state.tables) {
    for (const r of t.rows) {
      (keptSet.has(r.rid) ? byTable[t.name].kept : byTable[t.name].out).push(r);
    }
  }
  let keptTotal = 0, dropTotal = 0;
  const keptHtml = [], dropHtml = [];
  for (const t of S.state.tables) {
    const g = byTable[t.name];
    keptTotal += g.kept.length;
    dropTotal += g.out.length;
    keptHtml.push(`<div class="group-title"><span class="sw" style="background:${tableColor[t.name]}"></span>${esc(t.name)}（${g.kept.length}）</div>`);
    keptHtml.push(rowsTable(g.kept, true));
    dropHtml.push(`<div class="group-title"><span class="sw" style="background:${tableColor[t.name]}"></span>${esc(t.name)}（${g.out.length}）</div>`);
    dropHtml.push(rowsTable(g.out, false));
  }
  $("kept-list").innerHTML = keptHtml.join("");
  $("drop-list").innerHTML = dropHtml.join("");
  $("kept-count").textContent = `共 ${keptTotal} 行`;
  $("drop-count").textContent = `共 ${dropTotal} 行`;
  document.querySelectorAll("#drop-list tr.rrow").forEach((tr) =>
    tr.addEventListener("click", () => renderWhy(tr.getAttribute("data-rid"))));
  document.querySelectorAll("#kept-list tr.rrow").forEach((tr) =>
    tr.addEventListener("click", () => { S.selected = tr.getAttribute("data-rid"); switchTab("graph"); }));
  renderWhy(S._whyRid);
}

function rowsTable(rows, kept) {
  if (!rows.length) return "<p class='muted'>（无）</p>";
  const trs = rows.map((r) => {
    const forced = S.state.graph.forced_out[r.rid];
    const must = S.mustKeep.has(r.rid);
    const pill = statusPill(r.rid, kept, forced);
    return `<tr class="rrow" data-rid="${esc(r.rid)}">
      <td>${esc(r.pk || "∅")}</td><td>行${r.line}</td>
      <td style="text-align:right">${fmtW(r.weight)}</td>
      <td>${must ? "★" : ""}${pill}</td></tr>`;
  }).join("");
  return `<table class="rows"><thead><tr><th>主键</th><th>源行</th><th style="text-align:right">权重</th><th>状态</th></tr></thead><tbody>${trs}</tbody></table>`;
}
function statusPill(rid, kept, forced) {
  if (forced) return `<span class="status-pill forced">⚠ 强制隔离</span>`;
  return kept ? `<span class="status-pill kept">✓ 保留</span>` : `<span class="status-pill out">✗ 隔离</span>`;
}

/* ================= 渲染：为什么被舍弃 ================= */

function renderWhy(rid) {
  const el = $("why-panel");
  if (!rid || !S.candidate || !S.candidate.explanations || !S.candidate.explanations[rid]) {
    if (!rid) el.innerHTML = "<h2>为什么被舍弃</h2><p class='muted'>点击隔离清单中的一行</p>";
    return;
  }
  S._whyRid = rid;
  const ex = S.candidate.explanations[rid];
  const reasons = ex.reasons.map((x) => `<li>${esc(x)}</li>`).join("");
  const needs = (ex.needs || []).length
    ? `<h2>保留本行还需同时保留</h2><div>${ex.needs.map((r) =>
        `<span class="chip" data-rid="${esc(r)}">${esc(labelOf(r))}</span>`).join("")}</div>` : "";
  const blocks = (ex.blocks || []).length
    ? `<h2>与已保留行冲突</h2>${ex.blocks.map((b) =>
        `<div><span class="chip" data-rid="${esc(b.rid)}">${esc(labelOf(b.rid))}</span>
         <span class="muted">权重 ${fmtW(b.weight)}</span>
         <div class="muted">${esc(b.reasons[0] || "")}</div></div>`).join("")}` : "";
  el.innerHTML = `<h2>为什么被舍弃：${esc(labelOf(rid))}</h2>
    <div class="muted">权重 ${fmtW(ex.weight)}</div>
    <ul class="reason-list">${reasons}</ul>${needs}${blocks}
    <div class="row"><button class="btn small" id="why-goto">在图中查看</button></div>`;
  el.querySelectorAll(".chip").forEach((c) =>
    c.addEventListener("click", () => { S.selected = c.getAttribute("data-rid"); switchTab("graph"); }));
  $("why-goto").addEventListener("click", () => { S.selected = rid; switchTab("graph"); });
}

/* ================= 渲染：发布 ================= */

function renderPublish() {
  const el = $("publish-candidate");
  const cand = S.candidate;
  if (!cand) {
    el.innerHTML = "<h2>当前候选</h2><p class='muted'>先在“冲突关系”页求解生成候选</p>";
  } else {
    const stale = cand.stale || cand.fingerprint !== S.state.fingerprint;
    const canPublish = !stale && !S.dirty && (cand.status === "optimal" || cand.status === "feasible");
    el.innerHTML = `<h2>当前候选</h2>
      <dl class="kv">
        <dt>候选 ID</dt><dd>${esc(cand.id)}</dd>
        <dt>生成时间</dt><dd>${esc(cand.created_at)}</dd>
        <dt>状态</dt><dd>${cand.status === "optimal" ? "最优（已证明）" : cand.status === "feasible" ? "可行（未证明最优）" : esc(cand.status)}</dd>
        <dt>总权重</dt><dd>${fmtW(cand.weight)}${cand.bound != null ? `（上界 ${fmtW(cand.bound)}）` : ""}</dd>
        <dt>保留/隔离</dt><dd>${cand.kept.length} / ${S.state.graph.summary.rows - cand.kept.length}</dd>
        <dt>必留</dt><dd>${cand.must_keep.length} 行</dd>
        <dt>输入指纹</dt><dd>${stale ? "✗ 与当前数据不一致（已失效）" : "✓ 与当前数据一致"}</dd>
      </dl>
      ${stale ? `<div class="alert err">候选已失效：权重或约束在求解后被修改。源文件与旧发布版均保留，请重新求解。</div>` : ""}
      ${S.dirty ? `<div class="alert warn">必留选择已变更，需重新求解后再发布。</div>` : ""}
      <button id="btn-publish" class="btn primary" ${canPublish ? "" : "disabled"}>独立校验器复核并发布</button>
      <div class="hint">发布流程：候选清单 → 独立校验器重新核验（不经过求解器代码）→ 写出仓表与隔离文件（新版本目录，旧版本保留）。</div>
      <div id="publish-result"></div>`;
    $("btn-publish").addEventListener("click", publish);
  }

  const rel = S.state.releases || [];
  $("releases").innerHTML = rel.length ? rel.map((m) => `
    <div class="release-card">
      <h3>v${m.version} · 总权重 ${fmtW(m.weight)} · ${m.solver_status === "optimal" ? "最优" : "可行"}</h3>
      <div class="muted">${esc(m.created_at)} · 校验器 ${m.verifier_ok ? "✓ 通过" : "✗ 未通过"} · 保留 ${m.kept_total} / 隔离 ${m.quarantined_total}</div>
      <div class="files">${Object.keys(m.per_table || {}).map((t) =>
        `<a href="/releases/v${m.version}/warehouse/${t}.csv" target="_blank">${t}(仓)</a>
         <a href="/releases/v${m.version}/quarantine/${t}.csv" target="_blank">${t}(隔离)</a>`).join("")}
        <a href="/releases/v${m.version}/manifest.json" target="_blank">manifest</a>
        <a href="/releases/v${m.version}/verifier_report.json" target="_blank">校验报告</a>
      </div>
    </div>`).join("") : "<p class='muted'>尚无发布版本</p>";
}

async function publish() {
  const out = $("publish-result");
  out.innerHTML = `<div class="alert warn">校验并发布中…</div>`;
  try {
    const r = await api("/api/publish", { candidate_id: S.candidate.id });
    S.state.releases = r.releases;
    out.innerHTML = `<div class="alert ok">✓ 校验通过，已发布 v${r.manifest.version}（仓表与隔离文件已写出，旧版本保留）</div>`;
    renderPublish();
    $("publish-result") && ($("publish-result").innerHTML = `<div class="alert ok">✓ 校验通过，已发布 v${r.manifest.version}</div>`);
  } catch (e) {
    const p = e.payload || {};
    let html = `<div class="alert err">✗ ${esc(e.message)}</div>`;
    if (p.report) html += `<pre class="dump">${esc(JSON.stringify(p.report, null, 2))}</pre>`;
    out.innerHTML = html;
  }
}

/* ================= 事件 ================= */

function switchTab(name) {
  S.tab = name;
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tabpane").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "graph") renderGraph();
}

document.querySelectorAll(".tab").forEach((t) =>
  t.addEventListener("click", () => switchTab(t.dataset.tab)));
$("btn-solve").addEventListener("click", solve);
$("btn-reload").addEventListener("click", async () => {
  await loadState(true);
  if (S.candidate && S.candidate.fingerprint !== S.state.fingerprint) S.candidate.stale = true;
  renderAll();
});
$("btn-theme").addEventListener("click", () => {
  const cur = document.documentElement.getAttribute("data-theme");
  document.documentElement.setAttribute("data-theme", cur === "dark" ? "light" : "dark");
});

loadState(false);
