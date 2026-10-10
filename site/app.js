// Mirko dashboard v2 — vanilla JS, no build step. Same-origin API.
const API_BASE_URL = "";
const POLL_INTERVAL_MS = 10_000;
const $ = id => document.getElementById(id);

function esc(s) { const d = document.createElement("div"); d.textContent = s ?? ""; return d.innerHTML; }
function num(v, d = 4) { return (v === null || v === undefined || isNaN(v)) ? "—" : Number(v).toFixed(d); }
function sol(v, d = 4) { return v == null ? "—" : `${v > 0 ? "+" : ""}${num(v, d)} SOL`; }
const solPx = () => { const a = ((LEDGER.market || {}).assets || []).find(x => x.name === "SOL"); return a ? a.price : null; };
function usdc(vSol, signed = true) { const p = solPx(); return vSol == null || !p ? "—" : usd(vSol * p, signed); }
function solPlain(v, d = 3) { return v == null ? "—" : `${num(v, d)} SOL`; }
function usd(v, signed = false) { if (v == null) return "—"; const s = signed && v > 0 ? "+" : ""; return `${s}${v < 0 ? "-" : ""}$${Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`; }
function pct(v) { return v == null ? "—" : `${v > 0 ? "+" : ""}${(v * 100).toFixed(1)}%`; }
function cls(v) { return v == null || v === 0 ? "zero" : v > 0 ? "pos" : "neg"; }
function price(v) { if (v == null) return "—"; if (v >= 1000) return Math.round(v).toLocaleString(); return v < 0.001 ? Number(v).toExponential(2) : Number(v).toPrecision(4); }
function ago(iso) { const t = new Date(iso).getTime(); if (!t) return ""; const s = (Date.now() - t) / 1000;
  if (s < 60) return `${Math.floor(s)}s ago`; if (s < 3600) return `${Math.floor(s / 60)}m ago`; if (s < 86400) return `${Math.floor(s / 3600)}h ago`; return `${Math.floor(s / 86400)}d ago`; }
function setVal(id, text, c) { const el = $(id); el.textContent = text; if (c !== undefined) el.className = `${el.className.replace(/\b(pos|neg|zero)\b/g, "").trim()} ${c}`; }
async function fetchJson(path) { const r = await fetch(`${API_BASE_URL}${path}`); if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`); return r.json(); }
const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } }, set: (k, v) => { try { localStorage.setItem(k, v); } catch {} }, del: k => { try { localStorage.removeItem(k); } catch {} } };
const empty = t => `<div class="empty">${t}</div>`;
window.LEDGER = { mood: null, posts: [], market: null, journal: [], overview: null };

// ── Views (Dashboard / Market Thoughts / Owner) ─────────────────────
function showView(v) {
  document.querySelectorAll(".view").forEach(x => x.classList.toggle("hidden", x.id !== `view-${v}`));
  document.querySelectorAll(".nav-btn").forEach(b => b.classList.toggle("active", b.dataset.view === v));
  if (location.hash !== `#${v}`) history.replaceState(null, "", v === "dash" ? location.pathname : `#${v}`);
  if (v === "market") loadMarket(); if (v === "portfolio") loadPortfolio(); if (v === "predictions") loadPredictions(); if (v === "owner") ownerOpen();
  window.dispatchEvent(new Event("ledger:view"));
}
$("nav").addEventListener("click", e => { const b = e.target.closest(".nav-btn"); if (b) showView(b.dataset.view); });

// ── Collapsible panels ──────────────────────────────────────────────
function initFold(root) {
  root.querySelectorAll(".collapsible:not([data-fi])").forEach(c => {
    c.dataset.fi = "1";
    const k = `ledgerFold:${c.dataset.key}`, btn = c.querySelector(".fold");
    const set = f => { c.classList.toggle("folded", f); btn.textContent = f ? "+" : "–"; btn.setAttribute("aria-label", f ? "Expand" : "Minimize"); };
    const sv = store.get(k); set(sv == null ? c.dataset.fold === "1" : sv === "1");
    c.querySelector(".card-head").addEventListener("click", () => { const f = !c.classList.contains("folded"); set(f); store.set(k, f ? "1" : "0"); });
  });
}
initFold(document);

// ── Equity chart: small square + crosshair tooltip ──────────────────
let eqPts = [];
function renderEquity(points) {
  const el = $("equity-chart"); eqPts = (points || []).map(p => ({ t: new Date(p.t), v: p.v }));
  if (eqPts.length < 2) { el.innerHTML = `<div class="empty sm">Curve appears after the first closed trades.</div>`; return; }
  const W = 300, H = 160, P = 6, vals = eqPts.map(p => p.v), min = Math.min(0, ...vals), max = Math.max(0, ...vals), span = (max - min) || 1;
  const x = i => P + (i / (vals.length - 1)) * (W - 2 * P), y = v => H - P - ((v - min) / span) * (H - 2 * P);
  const line = vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const col = vals[vals.length - 1] >= 0 ? "#22d39b" : "#ff5c7a";
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
    <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="${col}" stop-opacity=".35"/><stop offset="1" stop-color="${col}" stop-opacity="0"/></linearGradient></defs>
    <line x1="0" x2="${W}" y1="${y(0)}" y2="${y(0)}" stroke="#2a3342" stroke-dasharray="3 5"/>
    <path d="${line}L${x(vals.length - 1)},${H}L${x(0)},${H}Z" fill="url(#g)"/>
    <path d="${line}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke"/>
    <line id="eq-x" y1="0" y2="${H}" stroke="#9aa7ff" stroke-width="1" vector-effect="non-scaling-stroke" opacity="0"/>
    <circle id="eq-dot" r="3.5" fill="#fff" stroke="${col}" stroke-width="2" opacity="0" vector-effect="non-scaling-stroke"/></svg>`;
  el._geo = { x, y, W };
}
(function eqHover() {
  const el = $("equity-chart"), tip = $("eq-tip");
  const move = ev => { if (eqPts.length < 2 || !el._geo) return; const r = el.getBoundingClientRect(), cx = (ev.touches ? ev.touches[0].clientX : ev.clientX) - r.left;
    const i = Math.max(0, Math.min(eqPts.length - 1, Math.round((cx / r.width) * (eqPts.length - 1)))), p = eqPts[i], g = el._geo;
    const X = g.x(i), Y = g.y(p.v); const l = el.querySelector("#eq-x"), d = el.querySelector("#eq-dot");
    l.setAttribute("x1", X); l.setAttribute("x2", X); l.setAttribute("opacity", ".6"); d.setAttribute("cx", X); d.setAttribute("cy", Y); d.setAttribute("opacity", "1");
    tip.innerHTML = `<b class="${cls(p.v)}">${usdc(p.v)}</b><span>${p.t.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}</span>`;
    tip.style.opacity = 1; tip.style.left = `${Math.min(r.width - 110, Math.max(0, (X / g.W) * r.width - 55))}px`; };
  const out = () => { tip.style.opacity = 0; el.querySelectorAll("#eq-x,#eq-dot").forEach(n => n.setAttribute("opacity", "0")); };
  el.addEventListener("mousemove", move); el.addEventListener("touchmove", move, { passive: true }); el.addEventListener("touchstart", move, { passive: true });
  el.addEventListener("mouseleave", out); el.addEventListener("touchend", () => setTimeout(out, 1500));
})();

// ── Renderers ───────────────────────────────────────────────────────
function renderState(state) {
  const ps = state.open_positions || [];
  $("stat-open-count").textContent = ps.length; const co = $("chip-open"); if (co) co.textContent = `${ps.length} open`;
  $("positions-body").innerHTML = ps.length ? ps.map(p => `
    <div class="pcard">
      <div class="top"><span class="tk">${esc(p.ticker)}${p.moonbag ? '<span class="tag moon">🌙 MOONBAG</span>' : ""}</span>
        <span class="pp mono ${cls(p.pnl_pct)}">${pct(p.pnl_pct)}</span></div>
      <div class="rows mono">
        <span>Entry</span><span>$${price(p.avg_entry)}</span>
        <span>Now</span><span>$${price(p.current_price)}</span>
        <span>Size</span><span>${usdc(p.size_sol, false)}</span>
        <span>PnL</span><span class="${cls(p.pnl_current_sol)}">${usdc(p.pnl_current_sol)}</span>
        <span>Opened</span><span>${p.opened_at ? ago(p.opened_at) : "—"}</span>
      </div>
    </div>`).join("") : empty("No open positions — waiting for a signal.");
}
function renderRealState(r) {
  const b = $("real-armed-badge"); if (b) b.textContent = r.armed ? "REAL MONEY · ARMED" : "REAL · UNARMED"; if (b) b.className = `chip ${r.armed ? "real" : "warn"}`;
  setVal("stat-real-balance", usd(r.balance_usdc));
  setVal("stat-real-pnl", usd(r.realized_pnl_usdc, true), cls(r.realized_pnl_usdc)); setVal("stat-exposure", usd(r.exposure_usdc));
}
function renderOverview(o) {
  LEDGER.overview = o;
  setVal("pnl-today", usdc(o.pnl_sol.today), cls(o.pnl_sol.today)); setVal("pnl-7d", usdc(o.pnl_sol.d7), cls(o.pnl_sol.d7)); setVal("pnl-all", usdc(o.pnl_sol.all), cls(o.pnl_sol.all));
  const tot = o.wins + o.losses; $("winloss").textContent = tot ? `${o.wins} / ${o.losses} · ${Math.round(o.wins / tot * 100)}%` : "—";
  renderEquity(o.equity);
  $("closed-body").innerHTML = o.closed_trades.length ? o.closed_trades.slice(0, 12).map(t => `
    <div class="row"><div class="l"><div class="t">${esc(t.symbol)} <span class="muted">${t.action === "partial_close" ? "partial" : ""}</span></div>
      <div class="m">${esc(t.reason || "exit")} · ${ago(t.at)}</div></div><div class="mono ${cls(t.pnl_sol)}">${usdc(t.pnl_sol)}</div></div>`).join("") : empty("No closed trades yet.");
}

// ── Mirko's thoughts (persona feed) ────────────────────────────────
const KIND = { musing: ["💭", "thought"], mood: ["🫀", "mood"], recap: ["📊", "recap"], entry: ["🟢", "entry"], exit_win: ["💰", "exit"],
  exit_loss: ["🩸", "exit"], refusal: ["🚩", "refused"], thesis_own: ["🧭", "thesis"], thesis_kol: ["👀", "watching"] };
function renderMind(f) {
  const pill = $("mood-pill"), body = $("mind-feed");
  if (!f) { pill.textContent = "offline"; body.innerHTML = empty("Mirko's thoughts will appear here."); return; }
  LEDGER.mood = f.mood; LEDGER.posts = f.posts || [];
  pill.textContent = `${f.mood.emoji || ""} ${f.mood.label || ""}`.trim();
  const ps = f.posts || [];
  const list = ps.slice(0, 30).reverse();   // chat-like: oldest at top, newest at the bottom
  const newest = list.length ? list[list.length - 1].ts : 0, atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
  body.innerHTML = list.length ? list.map((p, i) => { const [ic, lb] = KIND[p.kind] || ["💬", p.kind];
    return `<div class="thought ${i === list.length - 1 ? "latest" : ""} k-${esc(p.kind)}"><div class="th-meta"><span class="th-tag">${ic} ${esc(p.topic ? p.topic.replace(/_/g, " ") : lb)}</span><span class="th-time">${ago(new Date(p.ts * 1000))}</span></div><div class="th-text">${esc(p.text)}</div></div>`; }).join("")
    : (f.beliefs || []).length ? f.beliefs.map(b => `<div class="thought"><div class="th-meta"><span>🧭 belief</span></div><div class="th-text">${esc(b)}</div></div>`).join("")
    : empty("Mirko is quiet for now — first thoughts land within a few hours.");
  if (newest !== LEDGER._lastPost || atBottom) { LEDGER._lastPost = newest; requestAnimationFrame(() => { body.scrollTop = body.scrollHeight; }); }
}

function setConn(ok) { $("conn-dot").className = `dot ${ok ? "ok" : "err"}`; $("conn-status").textContent = ok ? "Live" : "Unreachable";
}

async function pollOnce() {
  const res = await Promise.allSettled([
    fetchJson("/api/state").then(renderState),
    fetchJson("/api/real_state").then(renderRealState),
    fetchJson("/api/journal?limit=60").then(j => { LEDGER.journal = j; }),
    fetchJson("/api/overview").then(renderOverview),
    fetchJson("/api/persona/feed?limit=40").then(renderMind).catch(() => renderMind(null)),
    fetchJson("/api/market_thoughts").then(m => { if (m.ready) LEDGER.market = m; }),
  ]);
  setConn(res.slice(0, 2).some(r => r.status === "fulfilled"));
  if (!LEDGER.councilTs || Date.now() - LEDGER.councilTs > 600_000) { LEDGER.councilTs = Date.now(); fetchJson("/api/council").then(x => { LEDGER.council = x; }).catch(() => {}); }
  if (!LEDGER.socialTs || Date.now() - LEDGER.socialTs > 600_000) { LEDGER.socialTs = Date.now(); fetchJson("/api/social").then(x => { LEDGER.social = x; }).catch(() => {}); }
  window.dispatchEvent(new Event("ledger:data"));
}

// ── Market Thoughts tab ─────────────────────────────────────────────
const fp = v => v == null ? "—" : v >= 1000 ? `$${Math.round(v).toLocaleString()}` : v >= 10 ? `$${v.toFixed(2)}` : `$${v.toFixed(4)}`;
const pc = (v, d = 1) => v == null ? "—" : `${v > 0 ? "+" : ""}${Number(v).toFixed(d)}%`;
const big = v => v == null ? "—" : v >= 1e9 ? `$${(v / 1e9).toFixed(2)}B` : v >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v / 1e3)}K`;
async function loadMarket() {
  let m; try { m = await fetchJson("/api/market_thoughts"); } catch { return; }
  if (!m.ready) { $("mt-regime").textContent = m.message || "Preparing…"; return; }
  m.assets = (m.assets || []).map(a => ({ support: [], resistance: [], notes: [], ...a }));
  m.regime = m.regime || { label: "Unknown", key: "chop", score: 0 };
  LEDGER.market = m;
  $("mt-regime").textContent = m.regime.label; $("mt-summary").textContent = m.summary;
  $("mt-stance").textContent = m.stance; $("mt-time").textContent = `updated ${ago(new Date((m.ts || 0) * 1000))}`;
  $("mt-scale").textContent = m.size_scale < 1 ? `memecoin size ×${m.size_scale}` : "full size allowed";
  $("mt-stance").className = `chip ${m.regime.key === "risk_off" ? "warn" : m.regime.key === "risk_on" ? "real" : ""}`;
  if (m.fng) { $("fng-v").textContent = m.fng.v; $("fng-l").textContent = `${m.fng.cls} · yesterday ${m.fng.prev}`; drawFng(m.fng.v); }
  const riskEmo = r => r == null ? "" : r <= 3 ? "🟢" : r <= 6 ? "🟡" : r <= 8 ? "🟠" : "🔴";
  const riskOf = i => i.risk ?? Math.max(0, Math.min(10, ({ mid: 4, low: 7, micro: 8 }[i.tier] ?? 2) + (i.venue === "Perp" ? 1 : 0) + ((i.rr || 0) < 1.5 ? 1 : 0) + (i.spec ? 1 : 0)));
  const idea = i => (i = { ...i, risk: riskOf(i) }, `<article class="ic ${i.side === "Long" ? "long" : "short"}">
      <header><span class="ic-tk">${esc(i.name)}</span><span class="ic-side">${i.side.toUpperCase()}</span><span class="ic-venue">${esc(i.venue)}${i.chain ? " · " + esc(i.chain) : ""}</span>
        <span class="ic-risk" title="Risk score 0-10">${riskEmo(i.risk)} ${i.risk ?? "–"}<small>/10</small></span></header>
      <dl class="ic-kv mono"><dt>Entry</dt><dd>${fp(i.entry_lo)} – ${fp(i.entry_hi)}</dd><dt>Stop</dt><dd class="neg">${fp(i.stop)}</dd>
        <dt>Target</dt><dd class="pos">${fp(i.t1)}${i.t2 ? ` → ${fp(i.t2)}` : ""}</dd><dt>R:R</dt><dd>${i.rr ?? "—"}</dd>${i.mcap ? `<dt>Mcap</dt><dd>${big(i.mcap)}</dd>` : ""}</dl>
      <p class="ic-why">${esc(i.why)}${i.spec ? " · speculative, small size" : ""}</p></article>`);
  const col = (xs, msg) => xs.length ? xs.map(idea).join("") : `<div class="empty sm">${msg}</div>`;
  $("mt-maj").innerHTML = col(m.trade_ideas || [], "No clean setup on the majors.");
  $("mt-mid").innerHTML = col(m.mid_caps || [], "No mid cap is accumulating cleanly.");
  $("mt-low").innerHTML = col(m.low_caps || [], "No low cap passes the filters.");
  $("mt-micro").innerHTML = col(m.micro_caps || [], "No high-conviction micro cap today.");
  const T = m.trenches;
  $("tr-mood").textContent = T ? T.mood : "—";
  const heat = !T ? "cold" : /fire|tailwind/i.test(T.mood) ? "hot" : /selective|choppy/i.test(T.mood) ? "warm" : "cold";
  $("trench-box").className = `trench-in heat-${heat}`;
  const li = xs => xs && xs.length ? `<ul>${xs.map(x => typeof x === "string" ? `<li><b>${esc(x)}</b></li>` : `<li><b>${esc(x.sym)}</b> <span class="cn">${esc(x.chain || "")}</span><br>${esc(x.why)}</li>`).join("")}</ul>` : `<div class="muted">Nothing convincing.</div>`;
  $("mt-trench").innerHTML = T ? `<p class="tr-take">${esc(T.take)}</p><div class="tr-grid">
      <div class="tr-col"><h4>🚀 Could pump</h4>${li(T.could_pump)}</div><div class="tr-col"><h4>🩸 Could dump</h4>${li(T.could_dump)}</div>
      <div class="tr-col"><h4>🧺 Accumulate</h4>${li(T.accumulate)}</div><div class="tr-col"><h4>🏛 Long-term</h4>${li(T.long_term)}</div></div>
` : empty("Trenches read appears after the next market refresh.");
  const b = m.next_boom;
  $("boom-w").textContent = b && b.boom_window ? `expected window · ${b.boom_window}${b.since ? ` · featured since ${b.since}` : ""}` : "";
  const prev = (b && b.previous) || [];
  $("mt-boom-prev").innerHTML = prev.length ? prev.map(x => `<div class="row col"><div class="t"><b>${esc(x.emoji || "🚀")} ${esc(x.theme)}</b> <span class="muted sm">${esc(x.shown_from || "?")} → ${esc(x.shown_to || "")}</span></div>
      <div class="muted sm">${md(x.thesis || "")}</div><div class="muted sm mono">window ${esc(x.boom_window || "—")} · ${[...(x.crypto || []).map(c => "$" + String(c).replace(/^\$/, "")), ...(x.stocks || [])].map(esc).join(" · ")}</div></div>`).join("")
    : empty("The archive starts when today's theme rotates out. Every past boom stays here.");
  $("mt-boom").innerHTML = b ? `<h3>${esc(b.emoji || "🚀")} ${esc(b.theme)}</h3><p class="boom-t">${md(b.thesis || "")}</p>
      <div class="grid2 inner"><div><div class="label pad">Why now</div><ul>${(b.why_now || []).slice(0, 4).map(w => `<li>${md(w)}</li>`).join("")}</ul></div>
      <div><div class="label pad">Crypto</div><div class="trend">${(b.crypto || []).map(c => `<span class="tchip"><b>$${esc(String(c).replace(/^\$/, ""))}</b></span>`).join("") || '<span class="muted">no clean crypto proxy, equities lead this one</span>'}</div>
      <div class="label pad" style="margin-top:10px">Stocks</div><div class="trend">${(b.stocks || []).map(s => `<span class="tchip"><b>${esc(s)}</b></span>`).join("") || '<span class="muted">—</span>'}</div>
      ${b.invalidation ? `<div class="label pad" style="margin-top:12px">Invalidation</div><div class="muted">${md(b.invalidation)}</div>` : ""}</div></div>` : empty("No theme in rotation.");
  try {
  $("mt-assets").innerHTML = m.assets.map(a => `<div class="card asset t-${a.tone}">
    <div class="a-head"><div><div class="a-name">${a.name}</div><div class="a-px mono">${fp(a.price)}</div></div>
      <div class="a-right"><span class="bias b-${a.tone}">${esc(a.bias)}</span><div class="mono muted">24h <span class="${cls(a.chg24)}">${pc(a.chg24)}</span> · 7d <span class="${cls(a.chg7)}">${pc(a.chg7)}</span></div></div></div>
    ${levelBar(a)}
    <p class="a-struct">${esc(a.structure)}</p>
    <div class="cases"><div><b class="pos">Bull</b> ${esc(a.bull_case)}</div><div><b class="neg">Bear</b> ${esc(a.bear_case)}</div></div>
    ${a.notes.length ? `<div class="a-notes">⚙️ ${esc(a.notes.join(" · "))}</div>` : ""}
    <div class="plan">🎯 ${esc(a.plan)}</div></div>`).join("");
  const hl = m.hyperliquid;
  $("hl-sub").textContent = hl ? `${hl.accounts} profitable accounts > $1M` : "unavailable";
  $("mt-hl").innerHTML = hl ? Object.entries(hl.coins).map(([c, h]) => { const ls = h.long_share == null ? 0.5 : h.long_share;
    return `<div class="row col"><div class="hl-top"><b>${c}</b><span class="mono muted">${h.longs}L / ${h.shorts}S · net ${big(h.long_ntl - h.short_ntl).replace("$-", "-$")}</span></div>
      <div class="lsbar"><i style="width:${Math.round(ls * 100)}%"></i></div>
      <div class="mono muted sm">${Math.round(ls * 100)}% long by notional · avg long ${fp(h.long_entry)} · avg short ${fp(h.short_entry)}</div></div>`; }).join("") : empty("Hyperliquid data unavailable this round.");
  if ($("mt-deriv")) $("mt-deriv").innerHTML = ""
    + (m.coinbase_premium != null ? `<div class="row"><div class="l"><div class="t">Coinbase premium</div><div class="m mono ${cls(m.coinbase_premium)}">${pc(m.coinbase_premium, 3)}</div></div></div>` : "")
    + (m.global ? `<div class="row"><div class="l"><div class="t">Total market</div><div class="m mono">${big(m.global.mcap)} · ${pc(m.global.mcap_chg)} · BTC dom ${m.global.btc_dom.toFixed(1)}%</div></div></div>` : "");
  $("mt-dex").innerHTML = (m.dex_flows || []).length ? m.dex_flows.map(d => `<div class="row"><div class="l"><div class="t">${esc(d.pair)} <span class="cn">${esc(d.chain)}</span></div>
      <div class="lsbar thin"><i style="width:${Math.round(d.buy_share * 100)}%"></i></div><div class="m mono">vol ${big(d.vol24)} · liq ${big(d.liq)}</div></div>
      <div class="mono ${cls(d.chg24)}">${pc(d.chg24, 0)}</div></div>`).join("") : empty("No DEX flow data this round.");
  $("mt-trend").innerHTML = (m.trending || []).map(t => `<div class="tchip"><b>${esc(t.symbol)}</b><span class="mono ${cls(t.chg24)}">${pc(t.chg24, 0)}</span></div>`).join("") || empty("—");
  } catch (e) { console.warn("market lower", e); }
  try { const I = m.insights || {}; const it = I.items || [];
    $("mt-insights").innerHTML = (I.beliefs || []).map(b => `<div class="row"><div class="l"><div class="t">💡 ${esc(b)}</div></div></div>`).join("")
      + it.slice(0, 12).map(x => `<div class="row"><div class="l"><div class="m"><span class="cn">${esc(x.cat)}</span> ${esc(x.title)}</div></div></div>`).join("") || empty("Today's reading list is being collected.");
  } catch (e) { console.warn("insights", e); }
}
const md = t => esc(t).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*/g, "");
function levelBar(a) {
  const lv = [...a.support.map(v => ["s", v]), ...a.resistance.map(v => ["r", v]), ["p", a.price]];
  const vs = lv.map(x => x[1]), lo = Math.min(...vs) * 0.995, hi = Math.max(...vs) * 1.005, X = v => ((v - lo) / (hi - lo)) * 100;
  return `<div class="lvl">${lv.map(([k, v]) => `<span class="lv lv-${k}" style="left:${X(v)}%"><i></i><em class="mono">${k === "p" ? "now" : Math.abs(X(v) - X(a.price)) < 9 ? "" : fp(v)}</em></span>`).join("")}</div>`;
}
function drawFng(v) {
  const c = $("fng-gauge"), x = c.getContext("2d"), W = c.width, H = c.height, cx = W / 2, cy = H - 12, r = 92;
  x.clearRect(0, 0, W, H); x.lineWidth = 14; x.lineCap = "round";
  const g = x.createLinearGradient(cx - r, 0, cx + r, 0); g.addColorStop(0, "#ff5c7a"); g.addColorStop(.5, "#f5b84b"); g.addColorStop(1, "#22d39b");
  x.strokeStyle = "#1a2230"; x.beginPath(); x.arc(cx, cy, r, Math.PI, 0); x.stroke();
  x.strokeStyle = g; x.beginPath(); x.arc(cx, cy, r, Math.PI, Math.PI + Math.PI * v / 100); x.stroke();
  const a = Math.PI + Math.PI * v / 100; x.fillStyle = "#fff"; x.beginPath(); x.arc(cx + r * Math.cos(a), cy + r * Math.sin(a), 6, 0, 7); x.fill();
}

$("boom-tabs").addEventListener("click", e => { const t = e.target.closest(".tab"); if (!t) return;
  document.querySelectorAll("#boom-tabs .tab").forEach(x => x.classList.toggle("active", x === t));
  $("mt-boom").classList.toggle("hidden", t.dataset.b !== "cur"); $("mt-boom-prev").classList.toggle("hidden", t.dataset.b === "cur"); $("boom-w").classList.toggle("hidden", t.dataset.b !== "cur"); });

// ── Portfolio tab (paper, simulated) ────────────────────────────────
const SLV = { spot: ["Crypto spot", "#7c8cff"], perps: ["Crypto perps", "#22d3e6"], stocks: ["Penny stocks", "#f5b84b"], poly: ["Polymarket predictions", "#ff6ec7"] };
const eur = v => v == null ? "—" : `€${Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
async function loadPortfolio() {
  let P; try { P = await fetchJson("/api/portfolio"); } catch { return; }
  $("pf-total").innerHTML = `${eur(P.total_eur)} <span class="${cls(P.pnl_pct)}" style="font-size:18px">${pc(P.pnl_pct, 2)}</span>`;
  $("pf-sub").textContent = `Started with ${eur(P.start_eur || 3000)} on ${new Date(P.created * 1000).toLocaleDateString()} · simulated, no real money`;
  const ORDER = ["spot", "perps", "stocks", "poly"];
  const sl = Object.entries(P.sleeves).sort((a, b) => ORDER.indexOf(a[0]) - ORDER.indexOf(b[0]));
  const c = $("pf-donut"), x = c.getContext("2d"), tot = sl.reduce((a, [, v]) => a + v.value_eur, 0) || 1; x.clearRect(0, 0, 180, 180);
  let a0 = -Math.PI / 2; sl.forEach(([k, v]) => { const a1 = a0 + 6.283 * v.value_eur / tot; x.strokeStyle = SLV[k][1]; x.lineWidth = 22; x.beginPath(); x.arc(90, 90, 66, a0 + .02, a1 - .02); x.stroke(); a0 = a1; });
  x.fillStyle = "#e6edf6"; x.font = "600 13px Inter"; x.textAlign = "center"; x.fillText("allocation", 90, 95);
  const H = P.history || [];
  if (H.length > 1) { const vs = H.map(h => h.v), mn = Math.min(...vs), mx = Math.max(...vs), sp = (mx - mn) || 1, Wd = 600, Hh = 150;
    const d = vs.map((v, i) => `${i ? "L" : "M"}${(i / (vs.length - 1) * Wd).toFixed(1)},${(Hh - 6 - (v - mn) / sp * (Hh - 12)).toFixed(1)}`).join("");
    const col = vs[vs.length - 1] >= 3000 ? "#22d39b" : "#ff5c7a";
    $("pf-chart").innerHTML = `<svg viewBox="0 0 ${Wd} ${Hh}" preserveAspectRatio="none"><path d="${d}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>`; }
  else $("pf-chart").innerHTML = `<div class="empty sm">The chart starts with the next 15-minute snapshot.</div>`;
  const book = b => b ? `<span class="tier">${esc(b)}</span>` : "";
  $("pf-sleeves").innerHTML = sl.map(([k, v]) => `<div class="card sleeve collapsible" data-key="pf-${k}" data-fold="0">
    <div class="card-head"><div><div class="label"><span class="sw" style="background:${SLV[k][1]}"></span>${SLV[k][0]}</div>
      <div class="big mono">${eur(v.value_eur)} <span class="${cls(v.pnl_pct)}" style="font-size:14px">${pc(v.pnl_pct, 2)}</span></div>
      <div class="muted">cash ${eur(v.cash_eur)} · ${v.positions.length} positions${v.last_decision ? ` · decided ${ago(new Date(v.last_decision * 1000))}` : ""}</div></div>
      <button class="fold" aria-label="Expand">+</button></div>
    <div class="fold-body"><div class="pf-list">${v.positions.length ? v.positions.map(p => `<article class="pp-card">
      <header><div class="pp-name"><b>${esc(p.sym)}</b>${book(p.book)}${p.side !== "Spot" ? `<span class="tier">${esc(p.side)} ${p.lev}x</span>` : ""}</div>
        <div class="pp-val mono" id="lv-${k}-${esc(p.sym)}"><span class="${cls(p.pnl_pct)}">${pc(p.pnl_pct, 1)}</span> · ${eur(p.value_eur)}</div></header>
      <dl class="kv mono"><dt>Entry</dt><dd>$${price(p.entry_usd)}</dd><dt>Now</dt><dd id="lp-${k}-${esc(p.sym)}">$${price(p.last_usd)}</dd>
        ${p.stop_usd ? `<dt>Stop</dt><dd class="neg">$${price(p.stop_usd)}</dd>` : ""}${p.target_usd ? `<dt>Target</dt><dd class="pos">$${price(p.target_usd)}</dd>` : ""}</dl>
      <p class="pp-why">${esc(p.why)}</p></article>`).join("") : empty("Flat. Waiting for a setup.")}</div>
      ${v.note ? `<div class="muted sm" style="margin-top:8px">${esc(v.note)}</div>` : ""}</div></div>`).join("");
  initFold($("pf-sleeves"));
  pfLive();
  $("pf-trades").innerHTML = (P.trades || []).length ? P.trades.map(t => `<div class="row"><div class="l"><div class="t">${esc(t.action)} ${esc(t.sym)} <span class="cn">${SLV[t.sleeve][0]}</span></div>
      <div class="m">${esc(t.why)}</div></div><div class="mono" style="text-align:right">${eur(t.eur)}${t.pnl_pct != null ? `<br><span class="${cls(t.pnl_pct)}">${pc(t.pnl_pct, 1)}</span>` : ""}<br><span class="jt">${ago(new Date(t.ts * 1000))}</span></div></div>`).join("") : empty("No trades yet.");
  const C = P.commentary || {};
  $("pf-comment").innerHTML = `<p>${esc(C.intro || "")}</p><ul>${Object.entries(C.sleeves || {}).filter(([, n]) => n).map(([k, n]) => `<li><b>${SLV[k][0]}:</b> ${esc(n)}</li>`).join("")}</ul>
    ${(C.positions || []).length ? `<div class="label pad" style="margin-top:12px">Why I hold each position</div><ul>${C.positions.map(p => `<li><b>${esc(p.sym)}</b> — ${esc(p.why)}</li>`).join("")}</ul>` : ""}`;
}
async function pfLive() {
  let L; try { L = await fetchJson("/api/portfolio/live"); } catch { return; }
  Object.entries(L.positions || {}).forEach(([k, ps]) => Object.entries(ps).forEach(([sym, p]) => {
    const e = document.getElementById(`lv-${k}-${sym}`), q = document.getElementById(`lp-${k}-${sym}`);
    if (e) e.innerHTML = `<span class="${cls(p.pnl_pct)}">${pc(p.pnl_pct, 1)}</span> · ${eur(p.value_eur)} <span class="live-dot" title="live"></span>`;
    if (q && p.last_usd != null) q.textContent = k === "poly" ? `${Math.round(p.last_usd * 100)}¢` : `$${price(p.last_usd)}`;
  }));
}
const pfOpen = () => !document.hidden && !$("view-portfolio").classList.contains("hidden");
setInterval(() => { if (pfOpen()) pfLive(); }, 20_000);
setInterval(() => { if (pfOpen()) loadPortfolio(); }, 300_000);

// ── Predictions (paper) ─────────────────────────────────────────────
const lis = iso => new Date(iso).toLocaleString("en-GB", { timeZone: "Europe/Lisbon", weekday: "short", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
const SPORT = { soccer: "⚽", nba: "🏀", ufc: "🥊" };
const pctp = p => `${Math.round(p * 100)}%`;
const res = r => r === "won" ? '<span class="res won">WON</span>' : r === "lost" ? '<span class="res lost">LOST</span>' : r === "void" ? '<span class="res void">VOID</span>' : "";
function betCard(b, settled) {
  const m = b.main;
  return `<article class="bet">
    <header><span class="bet-lg">${SPORT[b.sport] || "🎯"} ${esc(b.league)}</span><span class="bet-ko mono">${settled ? esc(b.final || "") : lis(b.kickoff) + " Lisbon"}</span></header>
    <h3>${esc(b.title)}</h3>
    <div class="bet-main"><div><div class="hud-label">Main pick · ${esc(m.market)}</div><div class="bet-pick">${esc(m.pick)} ${res(m.result)}</div></div>
      <div class="bet-prob"><b class="mono">${pctp(m.p)}</b><span class="mono muted">@ ${m.odds} · €${m.stake}</span></div></div>
    <div class="conf"><i style="width:${Math.round(m.p * 100)}%"></i></div>
    <ul class="bet-side">${(b.side || []).map(x => `<li><span>${esc(x.market)} ${res(x.result)}</span><span class="mono muted">${pctp(x.p)} · €${x.stake}</span></li>`).join("")}</ul>
    <p class="bet-why">💭 ${esc(m.why)}${settled && b.pnl != null ? ` · <b class="${cls(b.pnl)}">${b.pnl > 0 ? "+" : ""}€${b.pnl.toFixed(2)}</b>` : ""}</p></article>`;
}
async function loadPredictions() {
  let P; try { P = await fetchJson("/api/predictions"); } catch { return; }
  const S = P.sports || {}, pm = P.polymarket || { positions: [] };
  const tot = (S.value || 0) + (pm.value_eur || 0), start = (S.start || 0) + 1000;
  $("pr-total").innerHTML = `${eur(tot)} <span class="${cls(tot - start)}" style="font-size:18px">${pc((tot / start - 1) * 100, 2)}</span>`;
  $("pr-sub").textContent = `Paper bankroll €${start.toLocaleString()} (sports €${S.start || 1000} · Polymarket €1,000). Nothing real is placed.`;
  $("pr-stats").innerHTML = [["Open", (S.open || []).length + (pm.positions || []).length], ["Settled", S.n_settled ?? 0],
    ["Hit rate", S.hit_rate == null ? "—" : pctp(S.hit_rate)], ["Main picks", S.main_hit_rate == null ? "—" : pctp(S.main_hit_rate)], ["PnL", `€${(S.pnl || 0).toFixed(2)}`]]
    .map(([k, v]) => `<div class="pr-stat"><span class="hud-label">${k}</span><b class="mono">${v}</b></div>`).join("");
  $("pr-sports").innerHTML = (S.open || []).length ? S.open.map(b => betCard(b, false)).join("") : empty("No big fixtures in the next 72h. Mirko only bets the important ones.");
  $("pr-done").innerHTML = (S.settled || []).length ? S.settled.map(b => betCard(b, true)).join("") : empty("Nothing settled yet — results land ~2h after the final whistle.");
  $("pr-poly").innerHTML = (pm.positions || []).length ? pm.positions.map(p => `<article class="bet poly">
    <header><span class="bet-lg">🔮 Polymarket</span><span class="bet-ko mono">${p.end ? "resolves " + new Date(p.end).toLocaleDateString("en-GB", { timeZone: "Europe/Lisbon" }) : ""}</span></header>
    <h3>${esc(p.title || p.sym)}</h3>
    <div class="bet-main"><div><div class="hud-label">Mirko says</div><div class="bet-pick">${esc(p.side)}</div></div>
      <div class="bet-prob"><b class="mono">${Math.round((p.mirko_p || 0) * 100)}%</b><span class="mono muted">market ${Math.round(p.entry_usd * 100)}¢ → ${Math.round((p.last_usd || 0) * 100)}¢</span></div></div>
    <div class="conf"><i style="width:${Math.round((p.mirko_p || 0) * 100)}%"></i></div>
    <p class="bet-why">💭 ${esc(p.why)} · stake €${(p.value_eur || 0).toFixed(2)} now <span class="${cls(p.pnl_pct)}">${pc(p.pnl_pct, 1)}</span></p></article>`).join("")
    : empty("Mirko picks up to 4 live Polymarket questions once a day.");
}
$("pr-tabs").addEventListener("click", e => { const t = e.target.closest(".tab"); if (!t) return;
  document.querySelectorAll("#pr-tabs .tab").forEach(x => x.classList.toggle("active", x === t));
  ["sports", "poly", "done"].forEach(k => $(`pr-${k}`).classList.toggle("hidden", k !== t.dataset.p)); });
setInterval(() => { if (!document.hidden && !$("view-predictions").classList.contains("hidden")) loadPredictions(); }, 120_000);

// ── Owner view (same admin token as the switch) ─────────────────────
const tok = () => store.get("ledgerAdminToken");
async function ownerFetch(path, opts = {}) {
  const r = await fetch(`${API_BASE_URL}${path}`, { ...opts, headers: { "Content-Type": "application/json", Authorization: `Bearer ${tok() || ""}`, ...(opts.headers || {}) } });
  if (r.status === 401) { store.del("ledgerAdminToken"); ownerLocked(true); throw new Error("unauthorized"); }
  const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`); return j;
}
function ownerLocked(l) { $("owner-locked").classList.toggle("hidden", !l); $("owner-body").classList.toggle("hidden", l); }
async function ownerOpen() {
  if (!tok()) { ownerLocked(true); return; }
  try { await ownerFetch("/api/owner/check"); ownerLocked(false); loadOwner(); } catch {}
}
$("unlock-btn").addEventListener("click", async () => { const t = prompt("Admin token (from Railway boot log line [SWITCH] ... token=...):"); if (!t) return;
  store.set("ledgerAdminToken", t.trim()); try { await ownerFetch("/api/owner/check"); ownerLocked(false); loadOwner(); ownerControl(); } catch { alert("Wrong admin token."); } });
$("lock-btn").addEventListener("click", () => { store.del("ledgerAdminToken"); ownerLocked(true); });
async function loadOwner() {
  try { const o = await ownerFetch("/api/owner/overview"); const tr = [...o.traders].sort((a, b) => (b.active - a.active) || (b.trades - a.trades));
    $("traders-sum").textContent = `${tr.filter(t => t.active).length} active · ${tr.filter(t => !t.active).length} paused`;
    $("traders-body").innerHTML = tr.map(t => `<div class="tr ${t.active ? "" : "off"}"><div class="h"><span>${esc(t.handle.replace(/^fomo:/i, ""))}</span><span class="st ${t.active ? "on" : "off"}">${t.active ? "ACTIVE" : "PAUSED"}</span></div>
      <div class="chains">${t.chains.map(c => `<span class="cn">${esc(c)}</span>`).join("")}</div>
      <div class="muted">${t.trades ? `${t.trades} trades · hit ${Math.round(t.hit_rate * 100)}% · <span class="${cls(t.pnl_sol)}">${sol(t.pnl_sol, 3)}</span>` : "No trades yet"}</div>
      <div class="bar"><i style="width:${t.hit_rate == null ? 0 : Math.round(t.hit_rate * 100)}%"></i></div></div>`).join("") || empty("No traders configured."); } catch (e) { console.error(e); }
  loadLab();
}
const ST = { proposed: ["Proposed", ""], failed_checks: ["Failed checks", "neg"], paper: ["Paper trading", "acc"], paper_failed: ["Failed in paper", "neg"],
  awaiting_approval: ["Awaiting your approval", "warn"], approved: ["Live · approved", "pos"], rejected: ["Rejected", "mut"] };
const m2 = m => m && m.n ? `${pc(m.mean * 100, 2)}/trade · win ${Math.round(m.win_rate * 100)}% · DD ${Math.round(m.max_dd * 100)}% · n=${m.n}` : "—";
async function loadLab() {
  let L; try { L = await ownerFetch("/api/owner/lab"); } catch { return; }
  $("lab-sub").textContent = `ideas by ${L.llm} · paper needs ${L.paper_trades_required} live signals`;
  $("lab-body").innerHTML = L.strategies.length ? L.strategies.map(s => { const [lb, c] = ST[s.status] || [s.status, ""], R = s.results || {}, pp = R.paper;
    return `<div class="lab-card"><div class="lab-h"><div><b>${esc(s.name)}</b> <span class="cn">${esc(s.kind.replace("_", " "))}</span>${s.trader ? ` <span class="cn">${esc(s.trader)}</span>` : ""}<div class="muted sm">by ${esc(s.proposer)} · ${ago(new Date(s.created * 1000))}</div></div><span class="pill ${c}">${lb}</span></div>
      <div class="mono sm params">${Object.entries(s.params).map(([k, v]) => `${esc(k)} = <b>${v}</b>`).join(" · ")}</div>
      ${s.rationale ? `<p class="muted sm">${esc(s.rationale)}</p>` : ""}
      ${R.backtest ? `<div class="lab-grid mono sm"><span>Backtest · current</span><b>${m2(R.backtest.current)}</b><span>Backtest · candidate</span><b>${m2(R.backtest.candidate)}</b>
        <span>Stress · candidate</span><b>${m2(R.stress && R.stress.candidate)}</b>${pp ? `<span>Paper</span><b>${pp.n}/${L.paper_trades_required} ${pp.verdict ? "· " + esc(pp.verdict) : ""}</b>` : ""}</div>` : ""}
      ${R.checks ? `<div class="checks">${R.checks.map(k => `<span class="ck ${k.ok ? "ok" : "no"}" title="${esc(k.detail)}">${k.ok ? "✓" : "✕"} ${esc(k.name)}</span>`).join("")}</div>` : ""}
      ${["approved", "rejected"].includes(s.status) ? "" : `<div class="lab-act"><button class="btn" data-a="approve" data-id="${s.id}" ${s.status === "awaiting_approval" ? "" : "disabled title='Must pass backtest, risk, stress and paper first'"}>Approve</button><button class="btn ghost" data-a="reject" data-id="${s.id}">Reject</button></div>`}</div>`; }).join("")
    : empty("No proposals yet. The lab proposes once a day, or ask for ideas now.");
}
$("lab-body").addEventListener("click", async e => { const b = e.target.closest("button[data-a]"); if (!b || b.disabled) return;
  if (!confirm(b.dataset.a === "approve" ? "Approve and apply this strategy to the LIVE config?" : "Reject this strategy?")) return;
  try { await ownerFetch(`/api/owner/lab/${b.dataset.id}/${b.dataset.a}`, { method: "POST" }); } catch (err) { alert(err.message); } loadLab(); });
$("lab-propose").addEventListener("click", async () => { try { await ownerFetch("/api/owner/lab/propose", { method: "POST" }); $("lab-sub").textContent = "running backtests…"; setTimeout(loadLab, 8000); } catch (e) { alert(e.message); } });

// ── ON/OFF kill switch ──────────────────────────────────────────────
let switchEnabled = null;
function renderSwitch(st) {
  switchEnabled = !!st.enabled;
  const s = $("switch-state"); s.textContent = switchEnabled ? "ON" : "OFF"; s.className = `switch-state ${switchEnabled ? "on" : "off"}`;
  $("status-pulse").className = `pulse ${switchEnabled ? "on" : "off"}`;
  if ($("switch-note")) $("switch-note").textContent = switchEnabled ? "Mirko is watching the market and can open new positions." : "Paused — no new buys. Open positions are still managed (exits keep running).";
  const b = $("switch-btn"); b.disabled = false; b.className = `switch ${switchEnabled ? "on" : ""}`;
  $("switch-caption").textContent = switchEnabled ? "Tap to pause" : "Tap to resume";
}
async function ownerControl() {
  const t = tok(); if (!t) { $("hud-control").classList.add("hidden"); return; }
  try { await ownerFetch("/api/owner/check"); $("hud-control").classList.remove("hidden");  }
  catch { $("hud-control").classList.add("hidden"); }
}
async function pollSwitch() { try { renderSwitch(await fetchJson("/api/bot_switch")); } catch (e) { console.error(e); } }
$("switch-btn").addEventListener("click", async () => {
  let token = tok();
  if (!token) { token = prompt("Admin token (from Railway boot log line [SWITCH] ... token=...):"); if (!token) return; }
  const target = !switchEnabled;
  if (!confirm(target ? "Turn the bot ON (allow new buys)?" : "Turn the bot OFF (no new buys)?")) return;
  const b = $("switch-btn"); b.disabled = true;
  try {
    const r = await fetch(`${API_BASE_URL}/api/bot_switch`, { method: "POST", headers: { "Content-Type": "application/json", "Authorization": `Bearer ${token.trim()}` }, body: JSON.stringify({ enabled: target }) });
    if (r.status === 401) { store.del("ledgerAdminToken"); alert("Wrong admin token."); }
    else if (!r.ok) alert(`Failed: HTTP ${r.status}`);
    else { store.set("ledgerAdminToken", token.trim()); renderSwitch(await r.json()); ownerControl(); }
  } finally { b.disabled = false; pollSwitch(); }
});

pollOnce(); setInterval(() => { if (!document.hidden) pollOnce(); }, POLL_INTERVAL_MS);
ownerControl(); pollSwitch(); setInterval(() => { if (!document.hidden) pollSwitch(); }, POLL_INTERVAL_MS);
setInterval(() => { if (!document.hidden && !$("view-market").classList.contains("hidden")) loadMarket(); }, 120_000);
{ const h = (location.hash || "").slice(1); showView(["dash", "market", "portfolio", "predictions", "owner"].includes(h) ? h : "dash"); }
