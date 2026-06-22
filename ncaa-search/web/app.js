// NCAA Player Ranking Search — single-page frontend.
// Talks to /api/meta and /api/players. Builds weight sliders dynamically from
// the metric registry, applies presets, renders a Tabulator table, and supports
// CSV export + shareable URL (query string holds all filters & weights).

let META = null;
let TABLE = null;
let DEBOUNCE = null;

const $ = (id) => document.getElementById(id);

async function init() {
  META = await fetch("/api/meta").then((r) => r.json());
  buildSeason();
  buildDivisions();
  buildClasses();
  buildConferences();
  buildPresets();
  buildSliders();
  buildTable();
  applyUrlState();          // restore from share link if present
  wireEvents();
  refresh();
}

function buildSeason() {
  const sel = $("season");
  sel.innerHTML = (META.seasons || []).map((s) => `<option value="${s}">${s}</option>`).join("");
}

function buildDivisions() {
  const wrap = $("divisions");
  const divs = META.divisions.length ? META.divisions : ["D1", "D2"];
  wrap.innerHTML = divs.map((d) =>
    `<label class="flex items-center gap-1"><input type="checkbox" class="divChk" value="${d}" ${d === "D1" ? "checked" : ""}/> ${d}</label>`
  ).join("");
}

function buildClasses() {
  const wrap = $("classes");
  const order = ["Fr", "So", "Jr", "Sr"];
  const present = order.filter((c) => META.classes.includes(c));
  wrap.innerHTML = present.map((c) =>
    `<label class="flex items-center gap-1"><input type="checkbox" class="clsChk" value="${c}" ${c === "Sr" ? "checked" : ""}/> ${c}</label>`
  ).join("") + `<label class="flex items-center gap-1 text-slate-400"><input type="checkbox" class="clsChk" value="all"/> All</label>`;
}

function buildConferences() {
  const sel = $("conference");
  const opts = (META.conferences || []).map((c) => {
    const str = c.strength_rating == null ? "—" : c.strength_rating.toFixed(2);
    return `<option value="${c.conference}">[${c.division}] ${c.conference} (str ${str})</option>`;
  });
  sel.innerHTML = opts.join("");
}

function buildPresets() {
  const wrap = $("presets");
  const labels = {
    balanced: "Balanced", scoring_big: "Scoring big", three_and_d: "3-and-D wing",
    floor_general: "Floor general", rim_protector: "Rim protector",
    rebounder: "Rebounder", efficiency: "Efficiency",
  };
  let html = Object.keys(META.presets).map((k) =>
    `<button data-preset="${k}" class="chip px-2 py-1 rounded text-xs hover:bg-slate-700">${labels[k] || k}</button>`
  ).join("");
  html += `<button data-preset="__reset" class="chip px-2 py-1 rounded text-xs hover:bg-slate-700">Reset</button>`;
  wrap.innerHTML = html;
  wrap.querySelectorAll("button").forEach((b) =>
    b.addEventListener("click", () => {
      if (b.dataset.preset === "__reset") setWeights({});
      else setWeights(META.presets[b.dataset.preset] || {});
      refresh();
    })
  );
}

function buildSliders() {
  const wrap = $("sliders");
  wrap.innerHTML = META.metrics.map((m) => {
    const arrow = m.higher_is_better ? "↑ better" : "↓ better (inverted)";
    return `<div class="slider-row">
      <div class="flex justify-between text-xs">
        <span title="${arrow}">${m.label} <span class="text-slate-500">${m.higher_is_better ? "" : "↓"}</span></span>
        <span id="wv_${m.key}" class="text-slate-400">0</span>
      </div>
      <input type="range" min="0" max="100" value="0" class="wslider" data-key="${m.key}" />
    </div>`;
  }).join("");
  wrap.querySelectorAll(".wslider").forEach((s) =>
    s.addEventListener("input", () => {
      $(`wv_${s.dataset.key}`).textContent = s.value;
      debouncedRefresh();
    })
  );
  setWeights(META.default_weights || {});
}

function setWeights(weights) {
  document.querySelectorAll(".wslider").forEach((s) => {
    const v = weights[s.dataset.key] || 0;
    s.value = v;
    $(`wv_${s.dataset.key}`).textContent = v;
  });
}

function getWeights() {
  const w = {};
  document.querySelectorAll(".wslider").forEach((s) => {
    if (+s.value > 0) w[s.dataset.key] = +s.value;
  });
  return w;
}

function fmt(v, d = 1) {
  if (v === null || v === undefined || v === "") return "—";
  return typeof v === "number" ? v.toFixed(d) : v;
}

function buildTable() {
  const pctTip = (cell) => {
    const data = cell.getRow().getData();
    const p = (data.percentiles || {})[cell.getColumn().getField()];
    return p == null ? "" : `pctile: ${p}`;
  };
  TABLE = new Tabulator("#table", {
    height: "calc(100vh - 150px)",
    layout: "fitDataFill",
    placeholder: "No players match the current filters.",
    columns: [
      { title: "#", formatter: "rownum", width: 45, headerSort: false },
      { title: "Composite", field: "composite_score", sorter: "number", width: 100,
        formatter: (c) => `<b>${fmt(c.getValue(), 1)}</b>` },
      { title: "Player", field: "name", width: 150, frozen: true },
      { title: "Tm", field: "team", width: 110 },
      { title: "Conf", field: "conference", width: 130 },
      { title: "Str", field: "conf_strength", width: 60, sorter: "number",
        formatter: (c) => fmt(c.getValue(), 2) },
      { title: "Div", field: "division", width: 55,
        formatter: (c) => {
          const d = c.getValue();
          const col = d === "D1" ? "bg-emerald-700" : "bg-indigo-700";
          return `<span class="px-1 rounded text-white text-[10px] ${col}">${d}</span>`;
        } },
      { title: "Cl", field: "class", width: 45 },
      { title: "Pos", field: "position", width: 80 },
      { title: "GP", field: "gp", width: 50, sorter: "number" },
      { title: "MPG", field: "min_pg", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()) },
      { title: "PPG", field: "pts_pg", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()), tooltip: pctTip },
      { title: "RPG", field: "reb_pg", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()), tooltip: pctTip },
      { title: "APG", field: "ast_pg", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()), tooltip: pctTip },
      { title: "SPG", field: "stl_pg", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()) },
      { title: "BPG", field: "blk_pg", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()) },
      { title: "TOV", field: "tov_pg", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()) },
      { title: "FG%", field: "fg_pct", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()) },
      { title: "3P%", field: "fg3_pct", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()) },
      { title: "FT%", field: "ft_pct", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()) },
      { title: "TS%", field: "ts_pct", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()) },
      { title: "Usg", field: "usage", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()) },
      { title: "ORtg", field: "ortg", width: 65, sorter: "number", formatter: (c) => fmt(c.getValue()) },
      { title: "BPM", field: "bpm", width: 60, sorter: "number", formatter: (c) => fmt(c.getValue()) },
      { title: "Src", field: "source", width: 130 },
    ],
  });
  TABLE.on("rowClick", (e, row) => toggleDetail(row));
}

function toggleDetail(row) {
  const d = row.getData();
  const advanced = ["min_pct", "orb_pct", "drb_pct", "ast_pct", "to_pct", "blk_pct",
    "stl_pct", "fg2_pct", "efg_pct", "fg3a_rate", "fta_rate", "drtg", "bpm", "usage", "ortg"];
  const missing = advanced.filter((k) => d[k] === null || d[k] === undefined);
  const note = missing.length
    ? `<div class="text-amber-400 text-xs mt-1">Missing advanced metrics (${d.division}): ${missing.join(", ")}</div>` : "";
  const line = `<div class="p-2 text-xs bg-slate-800 rounded">
    <b>${d.name}</b> — ${d.team} · ${d.conference} · ${d.division} · ${d.class || "?"} · ${d.position || "?"}<br/>
    Updated: ${d.updated_at || "?"} · Source: ${d.source}
    ${note}</div>`;
  alert(line.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim());
}

function getDivisions() {
  return [...document.querySelectorAll(".divChk:checked")].map((c) => c.value);
}
function getClasses() {
  return [...document.querySelectorAll(".clsChk:checked")].map((c) => c.value);
}

function buildQuery() {
  const p = new URLSearchParams();
  p.set("season", $("season").value);
  getDivisions().forEach((d) => p.append("division", d));
  getClasses().forEach((c) => p.append("class", c));
  [...$("conference").selectedOptions].forEach((o) => p.append("conference", o.value));
  if ($("position").value) p.set("position", $("position").value);
  ["min_gp", "min_minutes", "min_conf_strength", "null_policy"].forEach((k) => {
    if ($(k).value) p.set(k, $(k).value);
  });
  p.set("division_factor_D1", $("df_D1").value || "1.0");
  p.set("division_factor_D2", $("df_D2").value || "0.85");
  const w = getWeights();
  Object.entries(w).forEach(([k, v]) => p.set(`w_${k}`, v));
  return p;
}

async function refresh() {
  const p = buildQuery();
  p.set("page_size", "500");
  $("status").textContent = "Loading…";
  try {
    const data = await fetch("/api/players?" + p.toString()).then((r) => r.json());
    TABLE.replaceData(data.rows || []);
    $("status").textContent = `${data.total} players · season ${data.season} · sorted by Composite (desc). Click a row for full stat line.`;
    const fresh = (data.rows || []).map((r) => r.updated_at).filter(Boolean).sort().pop();
    $("freshness").textContent = fresh ? `Data freshness (latest updated_at): ${fresh}` : "";
    // reflect state in URL (shareable) without reloading
    history.replaceState(null, "", "?" + p.toString());
  } catch (e) {
    $("status").textContent = "Error: " + e;
  }
}

function debouncedRefresh() {
  clearTimeout(DEBOUNCE);
  DEBOUNCE = setTimeout(refresh, 300);
}

function wireEvents() {
  ["season", "position", "min_gp", "min_minutes", "min_conf_strength", "null_policy", "df_D1", "df_D2", "conference"]
    .forEach((id) => $(id).addEventListener("change", refresh));
  document.querySelectorAll(".divChk, .clsChk").forEach((c) => c.addEventListener("change", refresh));
  $("resetWeights").addEventListener("click", () => { setWeights({}); refresh(); });
  $("exportBtn").addEventListener("click", () => {
    const p = buildQuery();
    window.location = "/api/export.csv?" + p.toString();
  });
  $("shareBtn").addEventListener("click", () => {
    navigator.clipboard.writeText(window.location.href);
    $("shareBtn").textContent = "✓ Copied";
    setTimeout(() => ($("shareBtn").textContent = "🔗 Copy share link"), 1500);
  });
}

// Restore filters + weights from URL query string (shareable links).
function applyUrlState() {
  const p = new URLSearchParams(window.location.search);
  if (![...p.keys()].length) return;
  if (p.get("season")) $("season").value = p.get("season");
  const divs = p.getAll("division");
  if (divs.length) document.querySelectorAll(".divChk").forEach((c) => (c.checked = divs.includes(c.value)));
  const cls = p.getAll("class");
  if (cls.length) document.querySelectorAll(".clsChk").forEach((c) => (c.checked = cls.includes(c.value)));
  ["position", "min_gp", "min_minutes", "min_conf_strength", "null_policy"].forEach((k) => {
    if (p.get(k)) $(k).value = p.get(k);
  });
  if (p.get("division_factor_D1")) $("df_D1").value = p.get("division_factor_D1");
  if (p.get("division_factor_D2")) $("df_D2").value = p.get("division_factor_D2");
  const w = {};
  for (const [k, v] of p.entries()) if (k.startsWith("w_")) w[k.slice(2)] = +v;
  if (Object.keys(w).length) setWeights(w);
}

init();
