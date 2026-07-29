/* ICRISAT Data Hub — Asset Explorer
   All figures computed at runtime from data/*.json — no hand-typed numbers. */

"use strict";

const GREEN = "#0E7E44", GREEN_MID = "#3D9B6A", GREEN_LIGHT = "#7BC49A";
const ORANGE = "#F58B31", ORANGE_LIGHT = "#F8AD6B";
const RED = "#C0392B", GREY = "#98A2B3", BLUE = "#2C5F9E";

const ACCESS_COLORS = { open: GREEN, internal: ORANGE, restricted: RED, unspecified: GREY };
const CHANNEL_COLORS = { inbox: GREEN, web: ORANGE, sidecar: BLUE, unspecified: GREY };
const CHANNEL_LABELS = { inbox: "Drop folder", web: "Web form", sidecar: "Sidecar metadata" };
const PALETTE = [GREEN, ORANGE, GREEN_MID, ORANGE_LIGHT, GREEN_LIGHT, "#FBC894", "#2F5233", GREY];

const FILTER_DIMS = [
  ["team", "Team"],
  ["file_type", "File type"],
  ["status", "Status"],
  ["access", "Access"],
  ["domain", "Domain"],
];

const state = {
  assets: [], sources: [], summary: {},
  filters: { q: "", team: new Set(), file_type: new Set(), status: new Set(), access: new Set(), domain: new Set() },
  sortKey: "created_at", sortDir: -1,
  charts: {},
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const label = (v) => (v === null || v === undefined || v === "" ? "unspecified" : String(v));

/* ---------------- data ---------------- */

async function load() {
  const [assetsPayload, sourcesPayload, summary] = await Promise.all([
    fetch("data/assets.json").then((r) => r.json()),
    fetch("data/sources.json").then((r) => r.json()),
    fetch("data/summary.json").then((r) => r.json()),
  ]);
  state.assets = assetsPayload.assets || [];
  state.sources = sourcesPayload.sources || [];
  state.summary = summary;
  $("#genStamp").textContent = assetsPayload.generated_at || "";

  buildFilterRail();
  bindEvents();
  renderAll();
}

/* ---------------- filtering ---------------- */

function passesFilters(a, exceptDim = null) {
  const f = state.filters;
  if (f.q) {
    const hay = [a.title, a.description, a.team, (a.tags || []).join(" "),
      ...(a.tables || []).flatMap((t) => (t.columns || []).map((c) => c.name)),
      ...(a.tables || []).map((t) => t.text_preview || "")].join(" ").toLowerCase();
    if (!f.q.toLowerCase().split(/\s+/).every((w) => hay.includes(w))) return false;
  }
  for (const [dim] of FILTER_DIMS) {
    if (dim === exceptDim) continue;
    const sel = f[dim];
    if (sel.size && !sel.has(label(a[dim]))) return false;
  }
  return true;
}

const filtered = () => state.assets.filter((a) => passesFilters(a));

function distinctValues(dim) {
  const s = new Set(state.assets.map((a) => label(a[dim])));
  s.delete("unspecified");
  return [...s].sort();
}

/* ---------------- chrome: rail, chips, KPIs ---------------- */

function buildFilterRail() {
  const host = $("#filterGroups");
  host.innerHTML = "";
  for (const [dim, title] of FILTER_DIMS) {
    const values = distinctValues(dim);
    if (!values.length) continue;
    const group = document.createElement("div");
    group.className = "fgroup";
    group.dataset.dim = dim;
    group.innerHTML = `<div class="fgroup-title">${esc(title)}</div>` +
      values.map((v) => {
        const dotColor = dim === "access" ? ACCESS_COLORS[v] || GREY : null;
        return `<label class="fopt">
          <input type="checkbox" value="${esc(v)}" data-dim="${esc(dim)}">
          ${dotColor ? `<span class="dot" style="background:${dotColor}"></span>` : ""}
          <span>${esc(v === "needs_review" ? "needs review" : v)}</span>
          <span class="count" data-count-for="${esc(dim)}:${esc(v)}"></span>
        </label>`;
      }).join("");
    host.appendChild(group);
  }
}

function refreshFilterCounts() {
  document.querySelectorAll("[data-count-for]").forEach((el) => {
    const [dim, v] = el.dataset.countFor.split(":");
    const n = state.assets.filter((a) => passesFilters(a, dim) && label(a[dim]) === v).length;
    el.textContent = n;
  });
}

function renderActiveChips() {
  const host = $("#activeFilters");
  const chips = [];
  const f = state.filters;
  if (f.q) chips.push({ dim: "q", v: f.q, text: `“${f.q}”` });
  for (const [dim] of FILTER_DIMS)
    for (const v of f[dim]) chips.push({ dim, v, text: v === "needs_review" ? "needs review" : v });
  host.innerHTML = chips.length
    ? chips.map((c) => `<button class="chip-x" data-dim="${esc(c.dim)}" data-v="${esc(c.v)}">${esc(c.text)} ✕</button>`).join("")
    : '<span class="empty">none — whole portfolio in view</span>';
  host.querySelectorAll(".chip-x").forEach((b) =>
    b.addEventListener("click", () => {
      if (b.dataset.dim === "q") { state.filters.q = ""; $("#searchInput").value = ""; }
      else {
        state.filters[b.dataset.dim].delete(b.dataset.v);
        const box = document.querySelector(`input[data-dim="${b.dataset.dim}"][value="${CSS.escape(b.dataset.v)}"]`);
        if (box) box.checked = false;
      }
      renderAll();
    })
  );
}

function renderKPIs(view) {
  const total = state.assets.length;
  const open = view.filter((a) => a.access === "open").length;
  const pub = view.filter((a) => a.status === "published").length;
  const rev = view.filter((a) => a.status === "needs_review").length;
  const teams = new Set(view.map((a) => a.team).filter(Boolean)).size;
  $("#kpiAssets").textContent = view.length;
  $("#kpiAssetsFoot").textContent = view.length === total ? "whole portfolio" : `of ${total} total`;
  $("#kpiTeams").textContent = teams;
  $("#kpiOpen").textContent = view.length ? Math.round((open / view.length) * 100) + "%" : "—";
  $("#kpiPublished").textContent = pub;
  $("#kpiReview").textContent = rev;
}

/* ---------------- charts registry ---------------- */

function makeChart(id, config) {
  if (state.charts[id]) state.charts[id].destroy();
  state.charts[id] = new Chart($("#" + id), config);
}

function doughnut(id, labels, values, colors) {
  makeChart(id, {
    type: "doughnut",
    data: { labels, datasets: [{ data: values, backgroundColor: colors, borderWidth: 2, borderColor: "#fff" }] },
    options: { maintainAspectRatio: false, cutout: "58%",
      plugins: { legend: { position: "right", labels: { boxWidth: 12, font: { size: 11 } } } } },
  });
}

function countsBy(view, dim) {
  const m = {};
  for (const a of view) { const k = label(a[dim]); m[k] = (m[k] || 0) + 1; }
  return Object.entries(m).sort((x, y) => y[1] - x[1]);
}

/* ---------------- OVERVIEW ---------------- */

function renderOverview(view) {
  // coverage matrix: team x file type
  const teams = [...new Set(view.map((a) => label(a.team)))].sort();
  const types = ["csv", "excel", "word"].filter((t) => view.some((a) => a.file_type === t));
  const max = Math.max(1, ...teams.flatMap((t) => types.map((ty) => view.filter((a) => label(a.team) === t && a.file_type === ty).length)));
  $("#gapMatrix").innerHTML = teams.length ? `<table>
      <tr><th></th>${types.map((t) => `<th>${esc(t)}</th>`).join("")}<th>total</th></tr>
      ${teams.map((t) => {
        const cells = types.map((ty) => {
          const n = view.filter((a) => label(a.team) === t && a.file_type === ty).length;
          const op = n ? 0.18 + 0.82 * (n / max) : 0;
          return n
            ? `<td class="cell" style="background:rgba(14,126,68,${op.toFixed(2)});${op > 0.55 ? "color:#fff" : ""}" data-team="${esc(t)}" data-type="${esc(ty)}" title="${esc(t)} · ${esc(ty)} — ${n} asset${n > 1 ? "s" : ""}. Click to explore.">${n}</td>`
            : `<td class="cell zero">0</td>`;
        }).join("");
        const tot = view.filter((a) => label(a.team) === t).length;
        return `<tr><td class="rowh">${esc(t)}</td>${cells}<td class="cell" style="background:#F2F4F7" data-team="${esc(t)}" data-type="">${tot}</td></tr>`;
      }).join("")}</table>`
    : '<p class="muted">No assets in view.</p>';
  document.querySelectorAll("#gapMatrix td.cell:not(.zero)").forEach((td) =>
    td.addEventListener("click", () => {
      resetFilters(false);
      const t = td.dataset.team, ty = td.dataset.type;
      state.filters.team.add(t);
      const teamBox = [...document.querySelectorAll('input[data-dim="team"]')].find((b) => b.value === t);
      if (teamBox) teamBox.checked = true;
      if (ty) {
        state.filters.file_type.add(ty);
        const typeBox = [...document.querySelectorAll('input[data-dim="file_type"]')].find((b) => b.value === ty);
        if (typeBox) typeBox.checked = true;
      }
      switchView("explore");
    })
  );

  // insights
  const insights = [];
  if (view.length) {
    const open = view.filter((a) => a.access === "open").length;
    const restr = view.filter((a) => a.access === "restricted").length;
    const rev = view.filter((a) => a.status === "needs_review").length;
    const [topTeam, topN] = countsBy(view, "team")[0] || [];
    const [topType, topTypeN] = countsBy(view, "file_type")[0] || [];
    const newest = [...view].sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)))[0];
    insights.push(`<b>${open} of ${view.length}</b> assets (${Math.round((open / view.length) * 100)}%) are <b>open access</b>${restr ? `; <b>${restr}</b> restricted` : ""}.`);
    if (rev) insights.push(`<b>${rev}</b> asset${rev > 1 ? "s" : ""} still need${rev > 1 ? "" : "s"} <b>metadata review</b> — completing them lifts the whole catalogue to published.`);
    else insights.push(`Every asset in view has <b>complete metadata</b> — the catalogue is fully published.`);
    if (topTeam && topTeam !== "unspecified") insights.push(`<b>${esc(topTeam)}</b> is the largest contributor in view with <b>${topN}</b> asset${topN > 1 ? "s" : ""}.`);
    if (topType) insights.push(`<b>${esc(topType)}</b> is the most common format (<b>${topTypeN}</b> asset${topTypeN > 1 ? "s" : ""}).`);
    if (newest) insights.push(`Newest addition: <b>${esc(newest.title)}</b> (${String(newest.created_at || "").slice(0, 10)}, via ${esc(CHANNEL_LABELS[newest.upload_channel] || label(newest.upload_channel))}).`);
  } else {
    insights.push("No assets match the current filters.");
  }
  $("#insightList").innerHTML = insights.map((t, i) => `<div class="insight ${i === 1 ? "warn" : ""}">${t}</div>`).join("");

  // team bars (stacked pub/rev)
  const byTeam = countsBy(view, "team");
  const maxTeam = Math.max(1, ...byTeam.map(([, n]) => n));
  $("#teamBars").innerHTML = byTeam.map(([t, n]) => {
    const pubN = view.filter((a) => label(a.team) === t && a.status === "published").length;
    const revN = n - pubN;
    const w = (k) => Math.max(0, (k / maxTeam) * 100);
    return `<div class="strength-row" data-team="${esc(t)}" title="${esc(t)} — ${pubN} published, ${revN} needs review. Click to explore.">
      <span class="strength-name">${esc(t)}</span>
      <span class="strength-track">
        <span class="strength-seg pub" style="width:${w(pubN)}%"></span>
        <span class="strength-seg rev" style="width:${w(revN)}%"></span>
      </span>
      <span class="strength-num">${n}</span></div>`;
  }).join("") || '<p class="muted">No assets in view.</p>';
  document.querySelectorAll(".strength-row").forEach((row) =>
    row.addEventListener("click", () => {
      resetFilters(false);
      const t = row.dataset.team;
      state.filters.team.add(t);
      const box = [...document.querySelectorAll('input[data-dim="team"]')].find((b) => b.value === t);
      if (box) box.checked = true;
      switchView("explore");
    })
  );

  // domain mix + access + type charts
  const dom = countsBy(view, "domain");
  doughnut("domainChart", dom.map((d) => d[0]), dom.map((d) => d[1]), PALETTE);
  const accOrder = ["open", "internal", "restricted", "unspecified"].filter((k) => view.some((a) => label(a.access) === k));
  doughnut("accessChart", accOrder, accOrder.map((k) => view.filter((a) => label(a.access) === k).length), accOrder.map((k) => ACCESS_COLORS[k]));
  const typ = countsBy(view, "file_type");
  doughnut("typeChart", typ.map((d) => d[0]), typ.map((d) => d[1]), [GREEN, ORANGE, BLUE]);
}

/* ---------------- EXPLORE ---------------- */

function accessBadge(v) {
  const k = label(v);
  return `<span class="badge b-${esc(k)}">${esc(k)}</span>`;
}

function renderExplore(view) {
  const rows = [...view].sort((a, b) => {
    const va = a[state.sortKey] ?? "", vb = b[state.sortKey] ?? "";
    const cmp = Array.isArray(va) ? va.join(",").localeCompare((vb || []).join(",")) : String(va).localeCompare(String(vb));
    return cmp * state.sortDir;
  });
  $("#exploreCount").textContent = `(${rows.length})`;
  $("#assetTable tbody").innerHTML = rows.map((a) => `
    <tr data-id="${esc(a.asset_id)}">
      <td><span class="t-title">${esc(a.title)}</span><br><span class="t-desc">${esc(a.description || "").slice(0, 120)}</span></td>
      <td>${esc(a.team || "—")}</td>
      <td>${esc(a.file_type)}</td>
      <td>${accessBadge(a.access)}</td>
      <td>${a.status === "published" ? '<span class="badge b-pub">published</span>' : '<span class="badge b-rev">needs review</span>'}</td>
      <td>${(a.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}</td>
      <td>${esc(String(a.created_at || "").slice(0, 10))}</td>
    </tr>`).join("");
  document.querySelectorAll("#assetTable tbody tr").forEach((tr) =>
    tr.addEventListener("click", () => openDrawer(tr.dataset.id))
  );
  document.querySelectorAll("#assetTable th.sortable").forEach((th) => {
    th.classList.toggle("sorted-asc", th.dataset.sort === state.sortKey && state.sortDir === 1);
    th.classList.toggle("sorted-desc", th.dataset.sort === state.sortKey && state.sortDir === -1);
  });
}

/* ---------------- drawer ---------------- */

function openDrawer(assetId) {
  const a = state.assets.find((x) => x.asset_id === assetId);
  if (!a) return;
  const meta = [
    ["Team", a.team], ["Owner", a.owner], ["Contact", a.contact],
    ["File", `${a.file_name} (${((a.size_bytes || 0) / 1024).toFixed(1)} KB)`],
    ["Upload channel", CHANNEL_LABELS[a.upload_channel] || label(a.upload_channel)],
    ["License", a.license], ["Domain", a.domain], ["Hub role", a.hub_role],
    ["Spatial coverage", a.spatial_coverage], ["Temporal coverage", a.temporal_coverage],
    ["Added", String(a.created_at || "").slice(0, 10)],
  ].filter(([, v]) => v);
  const schema = (a.tables || []).map((t) => {
    if (t.kind === "document")
      return `<div class="d-schema"><b>${esc(t.name)}</b> — document, ${esc(t.n_rows)} paragraphs${t.text_preview ? `<div class="d-doc">${esc(t.text_preview.slice(0, 800))}${t.text_preview.length > 800 ? "…" : ""}</div>` : ""}</div>`;
    const cols = (t.columns || []).map((c) =>
      `<div class="d-col"><b>${esc(c.name)}</b> <span class="samples">${esc(c.dtype)}${c.samples?.length ? ` — e.g. ${esc(c.samples.slice(0, 4).join(", "))}` : ""}</span></div>`
    ).join("");
    return `<div class="d-schema"><b>${esc(t.name)}</b> — ${esc(t.n_rows)} rows × ${esc(t.n_cols)} columns${cols}</div>`;
  }).join("");
  const missing = (a.missing_fields || []).length
    ? `<div class="insight warn">Missing metadata: <b>${esc(a.missing_fields.join(", "))}</b> — complete the review template next to the processed file.</div>` : "";
  $("#drawerBody").innerHTML = `
    <h2>${esc(a.title)}</h2>
    <div style="display:flex;gap:.3rem;flex-wrap:wrap;margin:.3rem 0 .2rem">
      ${a.status === "published" ? '<span class="badge b-pub">published</span>' : '<span class="badge b-rev">needs review</span>'}
      ${accessBadge(a.access)}
      <span class="badge b-${esc(label(a.upload_channel))}">${esc(CHANNEL_LABELS[a.upload_channel] || label(a.upload_channel))}</span>
      ${(a.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}
    </div>
    ${a.description ? `<p style="font-size:.85rem;color:#444">${esc(a.description)}</p>` : ""}
    ${missing}
    <dl class="d-meta">${meta.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    <div class="d-section"><h3>Schema</h3>${schema || '<p class="muted">No tables profiled.</p>'}</div>`;
  $("#drawer").classList.add("open");
  $("#drawer").setAttribute("aria-hidden", "false");
  $("#drawerBackdrop").hidden = false;
}

function closeDrawer() {
  $("#drawer").classList.remove("open");
  $("#drawer").setAttribute("aria-hidden", "true");
  $("#drawerBackdrop").hidden = true;
}

/* ---------------- UPLOAD ---------------- */

function renderUpload(view) {
  // channel doughnut
  const order = ["inbox", "web", "sidecar"].filter((k) => view.some((a) => label(a.upload_channel) === k));
  const labels = order.map((k) => CHANNEL_LABELS[k]);
  const values = order.map((k) => view.filter((a) => label(a.upload_channel) === k).length);
  makeChart("channelChart", {
    type: "bar",
    data: { labels, datasets: [{ data: values, backgroundColor: order.map((k) => CHANNEL_COLORS[k]), borderRadius: 6 }] },
    options: { maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (c) => ` ${c.raw} asset${c.raw === 1 ? "" : "s"} via ${c.label}` } } },
      scales: { y: { ticks: { precision: 0 }, beginAtZero: true } } },
  });

  // completeness
  const pub = view.filter((a) => a.status === "published").length;
  const rev = view.length - pub;
  const pct = (k) => (view.length ? Math.round((k / view.length) * 100) : 0);
  $("#completeness").innerHTML = view.length ? `
    <div class="comp-track">
      ${pub ? `<div class="comp-seg pub" style="width:${pct(pub)}%">${pct(pub)}%</div>` : ""}
      ${rev ? `<div class="comp-seg rev" style="width:${pct(rev)}%">${pct(rev)}%</div>` : ""}
    </div>
    <div class="comp-legend">
      <span><span class="dot" style="background:${GREEN}"></span>published (${pub})</span>
      <span><span class="dot" style="background:${ORANGE}"></span>needs review (${rev})</span>
    </div>` : '<p class="muted">No assets in view.</p>';

  // which fields are most often missing — where upload friction sits
  const mf = {};
  for (const a of view) for (const f of a.missing_fields || []) mf[f] = (mf[f] || 0) + 1;
  const mfRows = Object.entries(mf).sort((x, y) => y[1] - x[1]);
  const mfMax = Math.max(1, ...mfRows.map(([, n]) => n));
  $("#missingFields").innerHTML = mfRows.length ? `
    <h4>Most-missing metadata — where teams need the least friction</h4>
    ${mfRows.map(([f, n]) => `<div class="mf-row"><span>${esc(f)}</span>
      <span class="mf-track"><span class="mf-seg" style="width:${(n / mfMax) * 100}%"></span></span>
      <span class="mf-num">${n}</span></div>`).join("")}`
    : view.length ? "<h4>No metadata gaps in view — everything fully documented.</h4>" : "";

  // timeline (cumulative)
  const byDate = {};
  for (const a of view) {
    const d = String(a.created_at || "").slice(0, 10);
    if (d) byDate[d] = (byDate[d] || 0) + 1;
  }
  const dates = Object.keys(byDate).sort();
  let cum = 0;
  const cumVals = dates.map((d) => (cum += byDate[d]));
  makeChart("timelineChart", {
    type: "line",
    data: { labels: dates, datasets: [{ data: cumVals, borderColor: GREEN, backgroundColor: "rgba(14,126,68,.14)", fill: true, tension: .3, pointRadius: 4, pointBackgroundColor: GREEN }] },
    options: { maintainAspectRatio: false,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: (c) => ` ${c.raw} assets catalogued by ${c.label}` } } },
      scales: { y: { ticks: { precision: 0 }, beginAtZero: true } } },
  });
}

/* ---------------- SOURCES ---------------- */

function renderSources() {
  $("#srcCount").textContent = `(${state.sources.length})`;
  $("#sourcesList").innerHTML = state.sources.map((s) => `
    <div class="src-card">
      <h4>${esc(s.title)}</h4>
      <a class="src-url" href="${esc(s.url)}" target="_blank" rel="noopener">${esc(s.url)}</a>
      <div class="src-meta">
        <span class="tag">${esc(s.type)}</span>
        <span class="badge b-${esc(label(s.access))}">${esc(label(s.access))}</span>
        ${s.update_frequency ? `<span class="tag">${esc(s.update_frequency)}</span>` : ""}
        ${(s.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}
      </div>
      ${s.description ? `<p>${esc(s.description)}</p>` : ""}
    </div>`).join("") || '<p class="muted">No federated sources registered yet.</p>';

  const defs = state.summary.domain_definitions || {};
  $("#domTable tbody").innerHTML = Object.entries(defs).map(([d, def]) =>
    `<tr><td>${esc(d)}</td><td>${esc(def)}</td></tr>`).join("") ||
    '<tr><td class="muted">No domain vocabulary configured yet.</td></tr>';
}

/* ---------------- CSV export ---------------- */

function exportCsv() {
  const cols = ["asset_id", "title", "team", "owner", "file_type", "status", "access", "domain", "upload_channel", "tags", "file_name", "created_at"];
  const q = (v) => `"${String(Array.isArray(v) ? v.join("; ") : v ?? "").replace(/"/g, '""')}"`;
  const csv = [cols.join(",")]
    .concat(filtered().map((a) => cols.map((c) => q(a[c])).join(",")))
    .join("\r\n");
  const blob = new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "icrisat-data-hub-catalogue.csv";
  link.click();
  URL.revokeObjectURL(link.href);
}

/* ---------------- wiring ---------------- */

function switchView(name) {
  document.querySelectorAll(".viewtab").forEach((t) => t.classList.toggle("is-active", t.dataset.view === name));
  document.querySelectorAll(".view").forEach((v) => v.classList.toggle("is-active", v.dataset.view === name));
  renderAll();
}

function resetFilters(rerender = true) {
  state.filters.q = "";
  $("#searchInput").value = "";
  for (const [dim] of FILTER_DIMS) state.filters[dim].clear();
  document.querySelectorAll('#filterGroups input[type="checkbox"]').forEach((b) => (b.checked = false));
  if (rerender) renderAll();
}

function renderAll() {
  const view = filtered();
  refreshFilterCounts();
  renderActiveChips();
  renderKPIs(view);
  renderOverview(view);
  renderExplore(view);
  renderUpload(view);
  renderSources();
}

function bindEvents() {
  $("#searchInput").addEventListener("input", (e) => { state.filters.q = e.target.value.trim(); renderAll(); });
  $("#resetFilters").addEventListener("click", () => resetFilters(true));
  document.querySelectorAll("#filterGroups input[type=checkbox]").forEach((box) =>
    box.addEventListener("change", () => {
      const set = state.filters[box.dataset.dim];
      box.checked ? set.add(box.value) : set.delete(box.value);
      renderAll();
    })
  );
  document.querySelectorAll(".viewtab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
  document.querySelectorAll("#assetTable th.sortable").forEach((th) =>
    th.addEventListener("click", () => {
      const k = th.dataset.sort;
      if (state.sortKey === k) state.sortDir *= -1; else { state.sortKey = k; state.sortDir = 1; }
      renderExplore(filtered());
    })
  );
  $("#kpiPublishedCard").addEventListener("click", () => {
    resetFilters(false);
    state.filters.status.add("published");
    const box = [...document.querySelectorAll('input[data-dim="status"]')].find((b) => b.value === "published");
    if (box) box.checked = true;
    switchView("explore");
  });
  $("#kpiReviewCard").addEventListener("click", () => {
    resetFilters(false);
    state.filters.status.add("needs_review");
    const box = [...document.querySelectorAll('input[data-dim="status"]')].find((b) => b.value === "needs_review");
    if (box) box.checked = true;
    switchView("explore");
  });
  $("#downloadCsv").addEventListener("click", exportCsv);
  $("#drawerClose").addEventListener("click", closeDrawer);
  $("#drawerBackdrop").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
}

load().catch((e) => {
  $("#kpiAssets").textContent = "!";
  $("#kpiAssetsFoot").textContent = "failed to load catalogue: " + e;
});
