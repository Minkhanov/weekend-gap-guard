/* Weekend Gap Guard — static dashboard. Reads only ./data/*.json produced by `gapguard site`. */
"use strict";

const PROVIDER_COLOR = { Ondo: "var(--ondo)", xStocks: "var(--xstock)", bStocks: "var(--bstock)" };
const SLIPPAGE = { open: 0.5, pre_close: 0.75, closed: 1, pre_open: 1 };
const USDT = "0x55d398326f99059fF775485246999027B3197955";
const STALE_H = 2;   // replay: an hourly candle that closed more than 2 h ago is not a live quote

const $ = (id) => document.getElementById(id);
const el = (tag, attrs = {}, html) => {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const svgEl = (tag, attrs = {}) => {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  return n;
};
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const usd = (n, d = 0) => n == null ? "—" : "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const sgnUsd = (n) => n == null ? "—" : (n < 0 ? "−" : "+") + "$" + Math.abs(n).toLocaleString("en-US", { maximumFractionDigits: 0 });
const pct = (x, d = 2) => x == null ? "—" : (x >= 0 ? "+" : "−") + Math.abs(x * 100).toFixed(d) + "%";
const upct = (x, d = 2) => x == null ? "—" : (x * 100).toFixed(d) + "%";
const px = (x) => x == null ? "—" : Number(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const fmtET = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false });
const fmtETd = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", weekday: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false });
const fmtUTC = (iso) => new Date(iso).toISOString().slice(0, 16).replace("T", " ") + " UTC";
const et = (iso) => fmtET.format(new Date(iso)) + " ET";
const etd = (iso) => fmtETd.format(new Date(iso)) + " ET";
const PHASE = { open: "market open", pre_close: "last hour before the close", closed: "stock market closed", pre_open: "final 2 h before the reopen" };

async function getJSON(path) {
  const r = await fetch(path, { cache: "no-cache" });
  if (!r.ok) throw new Error(path + ": HTTP " + r.status);
  return r.json();
}

const state = { meta: null, tl: null, t: null, sym: null, live: null, cycle: 0 };

/* ------------------------------------------------------------------ boot */
(async function init() {
  try {
    state.meta = await getJSON("data/meta.json");
  } catch (e) {
    $("kpis").innerHTML = `<div class="kpi">Data not built yet. Run <span class="mono">gapguard site</span>. (${esc(e.message)})</div>`;
    return;
  }
  const m = state.meta;
  $("order-usd").textContent = usd(m.order_usd);
  $("built").textContent = "data built " + fmtUTC(m.built_at);
  const [calib, stress, live] = await Promise.all([
    getJSON("data/calibration.json").catch(() => null),
    getJSON("data/stress.json").catch(() => null),
    m.live ? getJSON("data/live.json").catch(() => null) : null,
  ]);
  state.live = live;
  renderKpis(m, stress, live);
  renderTabs(m.scenarios);
  if (m.scenarios.length) loadScenario(0);
  renderEvidence(calib, stress);
  renderLive();
  window.addEventListener("resize", () => state.tl && drawChart());
})();

function renderKpis(m, stress, live) {
  const k = [];
  const c = m.calibration_total;
  k.push(`<div class="kpi"><div class="num">${c.exceed_q99}<small> / ${c.ticker_weekends}</small></div>
    <div class="lbl">ticker-weekends where Monday's gap exceeded the guard's 99 % estimate — out-of-sample, 10 tickers, Sep 2025 → Oct 2026</div></div>`);
  if (stress && stress.n) {
    k.push(`<div class="kpi"><div class="num">${sgnUsd(stress.raw.worst)} <small>→</small> ${sgnUsd(stress.guarded.worst)}</div>
      <div class="lbl">worst Monday loss on a ${usd(stress.order_usd)} Saturday buy across ${stress.n} real weekends — raw vs guarded</div></div>`);
  }
  if (live) {
    const toks = Object.values(live.latest.tokens);
    const blocked = toks.filter((t) => t.verdict === "BLOCK").length;
    k.push(`<div class="kpi"><div class="num">${blocked}<small> / ${toks.length}</small></div>
      <div class="lbl">BSC stock tokens the guard would not buy at ${fmtUTC(live.latest.ts)} — stale pools or prices off the cross-issuer median</div></div>`);
  }
  $("kpis").innerHTML = k.join("");
  const first = Object.values(m.coverage).flatMap((g) => Object.values(g)).map((x) => x.first).sort()[0];
  $("data-note").textContent = `Data: hourly K-lines of ${Object.keys(m.coverage).length} tickers × 3 issuers on BSC (chain 56) since ${first ? first.slice(0, 10) : "?"}, ` +
    `pulled from the public Binance Wallet Skills endpoints; live board recorded by our own poller. Demo account: equity ${usd(m.account.equity)}, ` +
    `static floor ${usd(m.account.floor)}, daily loss limit ${usd(m.account.daily_loss_limit)}, profit target ${usd(m.account.profit_target)}.`;
}

/* ------------------------------------------------------------------ scenarios */
function renderTabs(list) {
  const box = $("scenario-tabs");
  box.innerHTML = "";
  list.forEach((s, i) => {
    const b = el("button", { role: "tab", "aria-selected": "false", "data-i": i },
      `<span class="t">${esc(s.ticker)}</span> ${esc(s.friday.slice(5))}${s.kind !== "weekend" ? " · " + esc(s.kind) : ""}`);
    b.onclick = () => loadScenario(i);
    box.append(b);
  });
}

async function loadScenario(i) {
  const s = state.meta.scenarios[i];
  [...$("scenario-tabs").children].forEach((b, j) => b.setAttribute("aria-selected", String(i === j)));
  $("scenario-blurb").textContent = s.blurb;
  const tl = await getJSON("data/" + s.file);
  state.tl = tl;
  tl._keys = Object.keys(tl.decisions).sort();
  state.sym = tl.symbols.find((x) => tl.providers[x] === "Ondo") || tl.symbols[0];
  const sat = new Date(new Date(tl.closure.start).getTime() + 20 * 3600e3).toISOString();
  state.t = nearestKey(sat);
  renderLegend();
  renderIssuerTabs();
  renderMoments();
  renderModel();
  renderOutcome();
  drawChart();
  renderDecision();
}

function nearestKey(iso) {
  const t = new Date(iso).getTime();
  let best = state.tl._keys[0], bd = Infinity;
  for (const k of state.tl._keys) {
    const d = Math.abs(new Date(k).getTime() - t);
    if (d < bd) { bd = d; best = k; }
  }
  return best;
}

function renderLegend() {
  const tl = state.tl;
  const items = tl.symbols.map((s) => `<span><i style="background:${PROVIDER_COLOR[tl.providers[s]]}"></i>${esc(s)} <span class="mono">(${esc(tl.providers[s])})</span></span>`);
  items.push(`<span><i class="dash"></i>Friday close = frozen reference</span>`);
  items.push(`<span><i class="band"></i>± q99 weekend gap</span>`);
  items.push(`<span><i class="shut"></i>stock market closed</span>`);
  $("legend").innerHTML = items.join("");
}

function renderIssuerTabs() {
  const tl = state.tl, box = $("issuer-tabs");
  box.innerHTML = "";
  tl.symbols.forEach((s) => {
    const b = el("button", { role: "tab", "aria-selected": String(s === state.sym) },
      `<span class="dot" style="background:${PROVIDER_COLOR[tl.providers[s]]}"></span>${esc(s)}`);
    b.onclick = () => { state.sym = s; renderIssuerTabs(); renderDecision(); };
    box.append(b);
  });
}

function renderMoments() {
  const tl = state.tl, s = new Date(tl.closure.start).getTime(), e = new Date(tl.closure.end).getTime();
  const iso = (ms) => new Date(ms).toISOString();
  const ms = [
    ["Friday, market open", s - 4 * 3600e3],
    ["Last hour before the close", s - 1 * 3600e3],
    ["Saturday noon", s + 20 * 3600e3],
    ["Sunday evening", e - 14 * 3600e3],
    ["Monday pre-open", e - 1.5 * 3600e3],
    ["After the reopen", e + 2.5 * 3600e3],
  ];
  const box = $("moments");
  box.innerHTML = "";
  for (const [label, t] of ms) {
    const k = nearestKey(iso(t));
    const b = el("button", { "data-k": k }, `${esc(label)}<small>${esc(et(k))}</small>`);
    b.onclick = () => select(k);
    box.append(b);
  }
  markMoment();
}

function markMoment() {
  [...$("moments").children].forEach((b) => b.classList.toggle("on", b.dataset.k === state.t));
}

function renderModel() {
  const tl = state.tl, g = tl.gap_model || {}, c = tl.closure;
  const rows = [
    ["Closure", `${c.kind}, ${c.hours} h`],
    ["Shut", `${etd(c.start)} → ${etd(c.end)}`],
    ["q99 weekend gap", upct(g.q99)],
    ["q95 weekend gap", upct(g.q95)],
    ["σ daily (close→close)", upct(g.sigma_daily)],
    ["σ weekend move", upct(g.sigma_gap)],
    ["Weekends in the fit", g.weekends_observed ?? "—"],
  ];
  $("model").innerHTML = rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("");
}

function renderOutcome() {
  const o = state.tl.outcome;
  if (!o) { $("outcome").innerHTML = ""; return; }
  const parts = [
    `Friday close <b>${px(o.friday_close)}</b>`,
    o.weekend_last != null ? `Sunday 20:00 ET <b>${px(o.weekend_last)}</b> (${pct(o.drift)})` : `no weekend on-chain print at Sunday 20:00 ET`,
    `Monday 10:00 ET <b>${px(o.monday_ref)}</b>`,
    `Monday gap <b>${pct(o.gap)}</b> vs q99 <b>${upct(o.q99_before)}</b> ${o.exceeded_q99 ? "— exceeded" : "— inside"}`,
  ];
  $("outcome").innerHTML = parts.map((p) => `<span>${p}</span>`).join("");
}

function select(k) {
  state.t = k;
  markMoment();
  drawChart();
  renderDecision();
}

/* ------------------------------------------------------------------ chart */
function drawChart() {
  const tl = state.tl, svg = $("chart");
  const W = Math.max(320, svg.clientWidth || 1000), H = svg.clientHeight || 380;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.innerHTML = "";
  const narrow = W < 640;
  const L = 58, R = W - 14, T = 14, STRIP_H = 12, STRIP_GAP = 4, nS = tl.symbols.length;
  const B = H - 34 - nS * (STRIP_H + STRIP_GAP) - 10;     // bottom of the price plot
  const pts = tl.points, t0 = new Date(pts[0].t).getTime(), t1 = new Date(pts[pts.length - 1].t).getTime();
  const x = (iso) => L + (new Date(iso).getTime() - t0) / (t1 - t0) * (R - L);
  const ref = tl.outcome ? tl.outcome.friday_close : pts.find((p) => p.reference)?.reference;
  const q = tl.gap_model?.q99 || 0.05;

  // y-domain: real prices of all issuers that sit within ±30 % of the reference; outliers become edge markers
  const vals = [];
  for (const p of pts) for (const s of tl.symbols) {
    const k = p.tokens[s], v = k.per_share;
    const fresh = k.quote_age_h != null && k.quote_age_h <= STALE_H;
    if (v != null && fresh && (!ref || Math.abs(v / ref - 1) < 0.3)) vals.push(v);
  }
  if (tl.outcome) vals.push(tl.outcome.monday_ref);
  if (ref) vals.push(ref);
  let lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.15 || hi * 0.01;
  lo -= pad; hi += pad;
  const y = (v) => B - (v - lo) / (hi - lo) * (B - T);
  const clipId = "clip" + Math.random().toString(36).slice(2);
  const defs = svgEl("defs");
  const cp = svgEl("clipPath", { id: clipId });
  cp.append(svgEl("rect", { x: L, y: T, width: R - L, height: B - T }));
  defs.append(cp);
  svg.append(defs);
  const plot = svgEl("g", { "clip-path": `url(#${clipId})` });

  // closure shading
  const cs = x(tl.closure.start), ce = x(tl.closure.end);
  plot.append(svgEl("rect", { x: cs, y: T, width: ce - cs, height: B - T, fill: "var(--closure)" }));
  // q99 band around the frozen reference, during the closure (edges drawn only when they fit the scale)
  let bandNote = "";
  if (ref) {
    const up = ref * (1 + q), dn = ref * (1 - q);
    if (up < hi || dn > lo)       // fill only when at least one edge is on the scale, otherwise it is just a tint
      plot.append(svgEl("rect", { x: cs, y: y(Math.min(up, hi)), width: R - cs, height: Math.max(0, y(Math.max(dn, lo)) - y(Math.min(up, hi))), fill: "var(--band)" }));
    for (const edge of [up, dn]) if (edge < hi && edge > lo)
      plot.append(svgEl("line", { x1: cs, x2: R, y1: y(edge), y2: y(edge), stroke: "var(--accent)", "stroke-dasharray": "2 4", opacity: .7 }));
    if (up >= hi || dn <= lo) bandNote = narrow ? `± q99 ${upct(q)}: off-scale` : `± q99 ${upct(q)} = ${px(dn)} … ${px(up)} — wider than this scale`;
  }
  // grid + y labels
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const v = lo + (hi - lo) * i / ticks, yy = y(v);
    svg.append(svgEl("line", { x1: L, x2: R, y1: yy, y2: yy, stroke: "var(--grid)" }));
    const t = svgEl("text", { x: L - 8, y: yy + 4, "text-anchor": "end" });
    t.textContent = px(v);
    svg.append(t);
  }
  // x labels at 00:00 and 12:00 US Eastern
  const etHour = new Intl.DateTimeFormat("en-US", { timeZone: "America/New_York", hour: "numeric", hourCycle: "h23" });
  for (let tt = Math.ceil(t0 / 3600e3) * 3600e3; tt <= t1; tt += 3600e3) {
    if (Number(etHour.format(new Date(tt))) % (narrow ? 24 : 12) !== 0) continue;
    const iso = new Date(tt).toISOString(), xx = x(iso);
    svg.append(svgEl("line", { x1: xx, x2: xx, y1: T, y2: B, stroke: "var(--grid)" }));
    const lab = svgEl("text", { x: xx, y: H - 10, "text-anchor": "middle" });
    lab.textContent = narrow ? et(iso).slice(0, 3) : et(iso).replace(" ET", "");
    svg.append(lab);
  }
  const etTag = svgEl("text", { x: R, y: H - 24, "text-anchor": "end" });
  etTag.textContent = "US Eastern time";
  svg.append(etTag);
  if (bandNote) {
    const bn = svgEl("text", { x: R - 6, y: T + 14, "text-anchor": "end", class: "lbl-strong" });
    bn.textContent = bandNote;
    svg.append(bn);
  }

  // reference line during the closure
  if (ref) {
    plot.append(svgEl("line", { x1: cs, x2: R, y1: y(ref), y2: y(ref), stroke: "var(--text)", "stroke-dasharray": "5 4", "stroke-width": 1.2, opacity: .75 }));
    const lab = svgEl("text", { x: R - 6, y: y(ref) + 15, class: "lbl-strong", "text-anchor": "end" });
    lab.textContent = narrow ? `Fri close ${px(ref)}` : `Friday close ${px(ref)} (reference)`;
    plot.append(lab);
  }

  // price lines: connect consecutive fresh quotes only; stale hours leave gaps
  for (const s of tl.symbols) {
    const color = PROVIDER_COLOR[tl.providers[s]];
    let d = "", prevFresh = false;
    const dots = [];
    for (const p of pts) {
      const k = p.tokens[s];
      const fresh = k.per_share != null && k.quote_age_h != null && k.quote_age_h <= STALE_H;
      const inRange = k.per_share != null && (!ref || Math.abs(k.per_share / ref - 1) < 0.3);
      if (fresh && inRange) {
        d += (prevFresh ? "L" : "M") + x(p.t).toFixed(1) + " " + y(k.per_share).toFixed(1) + " ";
        dots.push([x(p.t), y(k.per_share)]);
        prevFresh = true;
      } else {
        prevFresh = false;
        if (fresh && !inRange) {                              // a real trade far off the stock: edge marker
          const top = k.per_share > ref;
          const yy = top ? T + 6 : B - 6, xx = x(p.t);
          svg.append(svgEl("path", { d: top ? `M${xx - 5} ${yy + 4} L${xx + 5} ${yy + 4} L${xx} ${yy - 4} Z` : `M${xx - 5} ${yy - 4} L${xx + 5} ${yy - 4} L${xx} ${yy + 4} Z`, fill: color }));
          const lab = svgEl("text", { x: xx + 8, y: yy + 4 });
          lab.textContent = `${s} ${px(k.per_share)} (${pct(k.per_share / ref - 1, 0)})`;
          svg.append(lab);
        }
      }
    }
    if (d) plot.append(svgEl("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    if (dots.length < pts.length / 3) for (const [a, b] of dots) plot.append(svgEl("circle", { cx: a, cy: b, r: 3.2, fill: color }));
  }
  // Monday 10:00 ET reference point
  if (tl.outcome) {
    const mt = new Date(new Date(tl.closure.end).getTime() + 30 * 60e3).toISOString();
    plot.append(svgEl("circle", { cx: x(mt), cy: y(tl.outcome.monday_ref), r: 5, fill: "none", stroke: "var(--text)", "stroke-width": 2 }));
    const nearRight = x(mt) > R - 130;
    const lab = svgEl("text", { x: x(mt) + (nearRight ? -10 : 8), y: y(tl.outcome.monday_ref) + (nearRight ? -10 : 4),
      class: "lbl-strong", "text-anchor": nearRight ? "end" : "start" });
    lab.textContent = `Mon 10:00 ${pct(tl.outcome.gap)}`;
    plot.append(lab);
  }
  svg.append(plot);
  svg.append(svgEl("line", { x1: L, x2: R, y1: B, y2: B, stroke: "var(--border-2)" }));

  // verdict strips
  const cw = (R - L) / pts.length;
  tl.symbols.forEach((s, j) => {
    const yy = B + 10 + j * (STRIP_H + STRIP_GAP);
    const lab = svgEl("text", { x: L - 8, y: yy + 10, "text-anchor": "end" });
    lab.textContent = tl.providers[s];
    svg.append(lab);
    pts.forEach((p, i) => {
      const v = p.tokens[s].verdict;
      svg.append(svgEl("rect", { x: L + i * cw + .5, y: yy, width: Math.max(1, cw - 1), height: STRIP_H, rx: 2,
        fill: v === "ALLOW" ? "var(--allow)" : v === "REDUCE" ? "var(--reduce)" : "var(--block)", opacity: .85 }));
    });
  });

  // selection
  if (state.t) {
    const sx = x(state.t);
    svg.append(svgEl("line", { x1: sx, x2: sx, y1: T, y2: B + 10 + nS * (STRIP_H + STRIP_GAP), stroke: "var(--accent)", "stroke-width": 1.5 }));
  }

  // interaction
  const idxAt = (evt) => {
    const r = svg.getBoundingClientRect();
    const xx = (evt.clientX - r.left) / r.width * W;
    const f = Math.min(1, Math.max(0, (xx - L) / (R - L)));
    return Math.round(f * (pts.length - 1));
  };
  svg.onmousemove = (evt) => {
    const i = idxAt(evt), p = pts[i], tip = $("tip");
    const rows = tl.symbols.map((s) => {
      const k = p.tokens[s];
      const age = k.quote_age_h == null ? "" : k.quote_age_h > STALE_H ? ` <span style="color:var(--faint)">stale ${k.quote_age_h.toFixed(0)}h</span>` : "";
      return `<div><span style="color:${PROVIDER_COLOR[tl.providers[s]]}">●</span> ${esc(s)} ${px(k.per_share)}${age} <span class="pill v-${k.verdict}">${k.verdict}</span> ${usd(k.allowed_usd)}</div>`;
    }).join("");
    tip.innerHTML = `<b>${esc(etd(p.t))}</b> · ${esc(fmtUTC(p.t))}<br>${esc(PHASE[p.phase] || p.phase)}${p.reference_age_h ? ", reference " + p.reference_age_h.toFixed(0) + " h old" : ""}<div style="margin-top:6px">${rows}</div>`;
    tip.hidden = false;
    const r = svg.getBoundingClientRect();
    const left = evt.clientX - r.left + 14;
    tip.style.left = Math.min(left, r.width - tip.offsetWidth - 6) + "px";
    tip.style.top = Math.max(6, evt.clientY - r.top - 20) + "px";
  };
  svg.onmouseleave = () => { $("tip").hidden = true; };
  svg.onclick = (evt) => select(nearestKey(pts[idxAt(evt)].t));
}

/* ------------------------------------------------------------------ decision */
function renderDecision() {
  const tl = state.tl, d = tl.decisions[state.t]?.[state.sym];
  $("when").textContent = `${etd(state.t)} · ${fmtUTC(state.t)}`;
  if (!d) { $("decision-body").innerHTML = "<p class='sub'>No decision recorded for this hour.</p>"; return; }
  const m = d.metrics || {};
  const checks = d.checks.map((c) => `<li class="${c.name === d.binding || (d.binding === "account" && c.name === "account.cap") ? "bind" : ""}">
      <span class="st st-${c.status}">${c.status.toUpperCase()}</span>
      <span class="nm">${esc(c.name)}</span>
      <span class="dt">${esc(c.detail)}</span>
      <span class="cap">${c.cap_usd != null ? "cap " + usd(c.cap_usd) : ""}</span></li>`).join("");
  $("decision-body").innerHTML = `
    <div class="verdict"><span class="big v-${d.verdict}">${d.verdict}</span>
      <span class="sz">${d.verdict === "ALLOW" ? usd(d.allowed_usd, 2) : `<s>${usd(d.requested_usd, 2)}</s> → ${usd(d.allowed_usd, 2)}`}</span>
      <span class="sub">${esc(PHASE[d.phase] || d.phase)}${m.reference_age_h ? " · reference " + Number(m.reference_age_h).toFixed(1) + " h old" : ""}</span></div>
    <p class="summary">${esc(d.summary)}</p>
    <ul class="checks">${checks}</ul>`;
  renderBaw(d);
}

function renderBaw(d) {
  const tl = state.tl;
  if (d.verdict === "BLOCK" || !d.allowed_usd) {
    $("baw").textContent = `# guard: BLOCK — no order is built\n# ${d.binding}: ${(d.checks.find((c) => c.name === d.binding) || {}).detail || d.summary}`;
    return;
  }
  const slip = SLIPPAGE[d.phase] ?? 1;
  const qty = Number(d.allowed_usd.toFixed(2)).toString();
  $("baw").textContent = `# guard: ${d.verdict} ${usd(d.requested_usd)} → ${usd(d.allowed_usd, 2)} (${d.phase})\n` +
    `baw market-order quote \\\n  --fromTokenQty ${qty} --fromToken ${USDT} \\\n  --toToken ${tl.addresses[d.symbol]} \\\n  --binanceChainId 56 --slippage ${slip} --json\n` +
    `# swap only after the user confirms the quote`;
}

/* ------------------------------------------------------------------ live board */
function renderLive() {
  const live = state.live, board = $("board");
  if (!live) { $("live-sub").textContent = "No live recording bundled in this build."; board.innerHTML = ""; return; }
  const n = live.cycles.length;
  state.cycle = n - 1;
  const nav = $("cycle-nav");
  nav.innerHTML = "";
  if (n > 1) {
    const r = el("input", { type: "range", min: 0, max: n - 1, value: n - 1, "aria-label": "recorded cycle" });
    const lab = el("span", { class: "mono" });
    r.oninput = () => { state.cycle = +r.value; drawBoard(); lab.textContent = `${state.cycle + 1}/${n}`; };
    lab.textContent = `${n}/${n}`;
    nav.append(r, lab);
  }
  drawBoard();
}

function drawBoard() {
  const live = state.live, cyc = live.cycles[state.cycle];
  const ms = cyc.market_status || {};
  $("live-sub").innerHTML = `Recorded <b class="mono">${esc(fmtUTC(cyc.ts))}</b> (${esc(etd(cyc.ts))}) from the public Binance Wallet endpoints · ` +
    `Ondo session: <span class="mono">${esc(ms.marketStatus || (ms.openState ? "open" : "closed"))}</span>` +
    `${ms.offhours ? ` · weekend window ${ms.offhours.openState ? "open" : "closed"}` : ""} · ${live.n_cycles} cycle(s) since ${esc(fmtUTC(live.first_ts))}. Order tested: buy ${usd(state.meta.order_usd)}.`;
  const by = {};
  for (const [sym, t] of Object.entries(cyc.tokens)) (by[t.ticker] ||= {})[t.type] = [sym, t];
  const head = `<tr><th>Stock</th><th class="num">Ondo (…on)</th><th class="num">xStocks (…x)</th><th class="num">bStocks (…B)</th></tr>`;
  const cell = (pair) => {
    if (!pair) return `<td class="num">—</td>`;
    const [sym, t] = pair;
    const stale = t.quote_age_s != null && t.quote_age_s > 900;
    const title = `${sym}: ${t.verdict}${t.binding ? " — " + t.binding : ""}`;
    return `<td class="num" title="${esc(title)}"><div class="cell"><span class="meta">${stale ? "stale ≥24h" : "Δ median " + upct(t.dispersion, 2)}</span>
      <span class="px">${px(t.per_share)}</span><span class="pill v-${t.verdict}">${t.verdict}</span><span class="meta">${usd(t.allowed_usd)}</span></div></td>`;
  };
  const rows = Object.keys(by).sort().map((tk) => `<tr><td class="tk">${esc(tk)}</td>${cell(by[tk][1])}${cell(by[tk][2])}${cell(by[tk][3])}</tr>`);
  $("board").innerHTML = head + rows.join("");
}

/* ------------------------------------------------------------------ evidence */
function renderEvidence(calib, stress) {
  if (calib) {
    const head = `<tr><th>Ticker</th><th class="num">Weekends</th><th class="num">Mean |gap|</th><th class="num">Max |gap|</th>
      <th class="num">Median q99</th><th class="num">&gt; q99</th><th class="num" title="weekends with on-chain weekend trading">Wkd trading</th><th class="num" title="share of Monday's gap already shown by Sunday 20:00 ET">Priced in</th></tr>`;
    const rows = calib.map((c) => `<tr><td class="tk">${esc(c.ticker)}</td><td class="num">${c.weekends}</td><td class="num">${upct(c.mean_abs_gap)}</td>
      <td class="num">${upct(c.max_abs_gap)}</td><td class="num">${upct(c.median_q99)}</td><td class="num">${c.exceed_q99}</td>
      <td class="num">${c.weekends_with_weekend_trading}</td><td class="num">${c.priced_in_share == null ? "—" : pct(c.priced_in_share, 0)}</td></tr>`);
    $("calib").innerHTML = head + rows.join("");
    $("calib-note").textContent = "Gap = Friday 16:00 ET close → Monday 10:00 ET, per token, from hourly on-chain candles. " +
      "q99 is conservative by design (σ of daily closes × 2.576 × 1.25 fat-tail factor, never below the largest gap already seen): " +
      "no Monday gap exceeded it, at the cost of sizing weekends more cautiously than strictly necessary. " +
      "'Priced in by Sunday' = 1 − mean|Sun 20:00 ET → Mon 10:00 ET| ÷ mean|Fri close → Mon 10:00 ET|, on weekends that had on-chain weekend trading (Ondo since June 2026): " +
      "for single stocks the weekend price explained little of Monday's open, which is why the guard does not treat it as a mark.";
  }
  if (stress && stress.n) {
    const r = stress.raw, g = stress.guarded, maxAbs = Math.max(Math.abs(r.worst), Math.abs(g.worst), 1);
    const bar = (v, cls) => `<div class="bar ${cls}" style="width:${(Math.abs(v) / maxAbs * 100).toFixed(1)}%"></div>`;
    $("stress").innerHTML = `<h3>Saturday buys, marked at Monday's open</h3>
      <p class="sub">${stress.n} real weekends × the Ondo token of each ticker: buy ${usd(stress.order_usd)} at Saturday 12:00 ET, value at Monday 10:00 ET.</p>
      <div class="bar-row"><span>Raw worst</span>${bar(r.worst, "")}<span class="v">${sgnUsd(r.worst)}</span></div>
      <div class="bar-row"><span>Guarded</span>${bar(g.worst, "g")}<span class="v">${sgnUsd(g.worst)}</span></div>
      <div class="bar-row"><span>Raw 5 %</span>${bar(r.p05, "")}<span class="v">${sgnUsd(r.p05)}</span></div>
      <div class="bar-row"><span>Guarded 5 %</span>${bar(g.p05, "g")}<span class="v">${sgnUsd(g.p05)}</span></div>
      <p class="sub">Average guarded size ${usd(g.avg_size)}. Mean result ${sgnUsd(r.mean)} raw vs ${sgnUsd(g.mean)} guarded — the guard cuts the upside too;
      it is a risk control, not an alpha claim.</p>`;
  }
}
