// Mirko TA: one annotated chart (Lightweight Charts + overlay canvas for zones/entry/TP/SL) and an organised read.
(() => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fp = x => x == null || !isFinite(x) ? "–" : x >= 100 ? "$" + x.toLocaleString("en-US", { maximumFractionDigits: 2 }) : x >= 0.01 ? "$" + x.toFixed(4) : "$" + x.toPrecision(3);
  const big = x => x == null ? "–" : x >= 1e9 ? `$${(x / 1e9).toFixed(2)}B` : x >= 1e6 ? `$${(x / 1e6).toFixed(2)}M` : x >= 1e3 ? `$${(x / 1e3).toFixed(0)}k` : `$${Math.round(x)}`;
  const LW = () => window.LightweightCharts;
  let chart, cs, D = null, inited = false, busy = false, plan = null;
  const nums = s => (String(s || "").replace(/,/g, "").match(/\d*\.?\d+(e-?\d+)?/gi) || []).map(Number).filter(x => x > 0);
  function parsePlan(d) {
    const a = (d.read || {}).action || {}, px = d.ta.price, near = x => x > px * .2 && x < px * 5;
    if (!/long|short/i.test(a.type || "")) return null;
    const en = nums(a.entry).filter(near), tp = nums(a.tp).filter(near), sl = nums(a.sl).filter(near);
    if (!en.length || !sl.length) return null;
    return { side: /long/i.test(a.type) ? "long" : "short", lo: Math.min(...en), hi: Math.max(...en), tp: tp.slice(0, 3), sl: sl[0] };
  }
  function build() {
    const el = $("ta-chart"); el.innerHTML = "";
    const w = el.clientWidth, h = w < 600 ? 360 : 520;
    chart = LW().createChart(el, { width: w, height: h, layout: { background: { color: "transparent" }, textColor: "#8fa0b8", fontFamily: "ui-monospace, monospace", attributionLogo: false },
      grid: { vertLines: { color: "rgba(255,255,255,.035)" }, horzLines: { color: "rgba(255,255,255,.035)" } },
      rightPriceScale: { borderColor: "rgba(255,255,255,.08)", scaleMargins: { top: .08, bottom: .18 } }, timeScale: { borderColor: "rgba(255,255,255,.08)", timeVisible: true, rightOffset: 22 },
      crosshair: { mode: 0 }, localization: { priceFormatter: p => p >= 100 ? p.toFixed(2) : p >= 0.01 ? p.toFixed(4) : p.toPrecision(3) } });
    chart.timeScale().subscribeVisibleLogicalRangeChange(() => requestAnimationFrame(overlay));
  }
  const line = (data, color, width = 1, style = 0) => { const s = chart.addLineSeries({ color, lineWidth: width, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }); s.setData(data); return s; };
  function draw(d) {
    build(); D = d; plan = parsePlan(d);
    cs = chart.addCandlestickSeries({ upColor: "#22d39b", downColor: "#ff5c7a", borderVisible: false, wickUpColor: "#22d39b", wickDownColor: "#ff5c7a" });
    cs.setData(d.bars.map(b => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));
    const vs = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: .86, bottom: 0 } });
    vs.setData(d.bars.map(b => ({ time: b.time, value: b.value, color: b.close >= b.open ? "rgba(34,211,155,.28)" : "rgba(255,92,122,.28)" })));
    line(d.series.ema20, "rgba(122,162,255,.8)"); line(d.series.ema50, "rgba(255,209,102,.8)");
    for (const tl of [d.ta.tl_high, d.ta.tl_low]) if (tl && tl.from.t < tl.to.t) line([{ time: tl.from.t, value: tl.from.v }, { time: tl.to.t, value: tl.to.v }], "rgba(255,255,255,.6)", 1);
    (d.ta.support || []).slice(0, 2).forEach((p, i) => cs.createPriceLine({ price: p, color: "rgba(34,211,155,.9)", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: `S${i + 1}` }));
    (d.ta.resistance || []).slice(0, 2).forEach((p, i) => cs.createPriceLine({ price: p, color: "rgba(255,92,122,.9)", lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: `R${i + 1}` }));
    const vp = (d.intel || {}).vp; if (vp) cs.createPriceLine({ price: vp.poc, color: "rgba(255,209,102,.7)", lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: "POC" });
    if (plan) {
      cs.createPriceLine({ price: (plan.lo + plan.hi) / 2, color: "#7aa2ff", lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: plan.side === "long" ? "ENTRY ▲" : "ENTRY ▼" });
      plan.tp.forEach((p, i) => cs.createPriceLine({ price: p, color: "#22d39b", lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: `TP${i + 1}` }));
      cs.createPriceLine({ price: plan.sl, color: "#ff5c7a", lineWidth: 2, lineStyle: 0, axisLabelVisible: true, title: "SL" });
    }
    const P = d.ta.paths, col = { bull: "#22d39b", base: "rgba(143,160,184,.8)", bear: "#ff5c7a" };
    for (const k of ["bull", "base", "bear"]) if (P && P[k] && P[k].length) { const s = line(P[k].map(x => ({ time: x.t, value: x.v })), col[k], 2, 2);
      s.setMarkers([{ time: P[k][P[k].length - 1].t, position: k === "bear" ? "belowBar" : "aboveBar", color: col[k], shape: k === "bear" ? "arrowDown" : k === "bull" ? "arrowUp" : "circle", text: k }]); }
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, d.bars.length - 130), to: d.bars.length + 24 });
    setTimeout(overlay, 60);
  }
  // Shaded zones drawn under the chart's own coordinate system.
  function overlay() {
    const cv = $("ta-ov"), el = $("ta-chart"); if (!cv || !cs || !D) return;
    const dpr = Math.min(2, devicePixelRatio || 1), W = el.clientWidth, H = el.clientHeight; cv.width = W * dpr; cv.height = H * dpr; cv.style.width = W + "px"; cv.style.height = H + "px";
    const x = cv.getContext("2d"); x.setTransform(dpr, 0, 0, dpr, 0, 0); x.clearRect(0, 0, W, H);
    const pw = W - (chart.priceScale("right").width() || 60), y = p => cs.priceToCoordinate(p);
    const band = (lo, hi, fill, label, lc) => { const a = y(hi), b = y(lo); if (a == null || b == null) return; const top = Math.min(a, b), h = Math.max(3, Math.abs(b - a));
      x.fillStyle = fill; x.fillRect(0, top, pw, h); if (label) { x.font = "600 10px ui-monospace,monospace"; x.fillStyle = lc; x.fillText(label, 8, top - 4 < 10 ? top + 12 : top - 4); } };
    const t = D.ta, px = t.price, z = .006;
    (t.support || []).slice(0, 2).forEach((p, i) => band(p * (1 - z), p * (1 + z), "rgba(34,211,155,.10)", i ? "" : "SUPPORT ZONE", "rgba(34,211,155,.9)"));
    (t.resistance || []).slice(0, 2).forEach((p, i) => band(p * (1 - z), p * (1 + z), "rgba(255,92,122,.10)", i ? "" : "RESISTANCE ZONE", "rgba(255,92,122,.9)"));
    const vp = (D.intel || {}).vp; if (vp) band(vp.val, vp.vah, "rgba(255,209,102,.035)", "", "");
    if (plan) {
      band(plan.lo, Math.max(plan.hi, plan.lo * 1.0015), "rgba(122,162,255,.22)", `ENTRY ${fp(plan.lo)}${plan.hi > plan.lo ? "–" + fp(plan.hi) : ""}`, "#a9c1ff");
      const e = (plan.lo + plan.hi) / 2, t1 = plan.tp[0];
      if (t1) band(Math.min(e, t1), Math.max(e, t1), "rgba(34,211,155,.06)", "", "");
      band(Math.min(e, plan.sl), Math.max(e, plan.sl), "rgba(255,92,122,.06)", "", "");
    }
  }
  function header(d) {
    const m = d.meta, t = d.ta, r = d.read || {}, c = r.conviction, emo = c >= 8 ? "🟢" : c >= 6 ? "🟡" : c >= 4 ? "🟠" : "🔴";
    const logo = m.logo || (window.LOGOS || {})[String(m.symbol || "").toUpperCase()];
    $("ta-meta").innerHTML = `<div class="th-id">${logo ? `<img src="${esc(logo)}" alt="" class="coin-logo lg" referrerpolicy="no-referrer">` : `<span class="coin-logo lg ltr">${esc((m.symbol || "?")[0])}</span>`}
        <div><b class="tm-sym">${esc(m.symbol || d.q)}</b><div class="muted sm mono">${esc(m.name && m.name !== m.symbol ? m.name + " · " : "")}${esc(m.onchain ? "CEX + " + m.onchain : m.chain)} · ${esc(d.tf)}</div></div></div>
      <div class="th-px"><b class="mono">${fp(t.price)}</b><span class="mono ${t.chg >= 0 ? "pos" : "neg"}">${t.chg >= 0 ? "+" : ""}${t.chg.toFixed(1)}%</span><small class="muted">window</small></div>
      <div class="th-stats mono">${t.rsi ? `<span>RSI <b>${t.rsi.toFixed(0)}</b></span>` : ""}${m.mcap ? `<span>MC <b>${big(m.mcap)}</b></span>` : ""}${m.liq ? `<span>Liq <b>${big(m.liq)}</b></span>` : ""}</div>
      <div class="th-conv" title="Mirko's conviction"><span>${emo}</span><b class="mono">${c}</b><small>/10</small></div>`;
  }
  const card = (cls, title, body) => `<article class="card tr-card ${cls}"><h3>${title}</h3>${body}</article>`;
  const P = x => x ? `<p>${esc(x)}</p>` : "";
  function read(d) {
    const r = d.read, a = r.action || {}, I = d.intel || {}, pr = r.prediction || {}, sc = r.scenarios || {};
    const cls = /long/i.test(a.type) ? "long" : /short/i.test(a.type) ? "short" : "flat";
    const pl = plan ? `<div class="tr-plan mono">Entry <b>${fp(plan.lo)}${plan.hi > plan.lo ? "–" + fp(plan.hi) : ""}</b> <span class="ar">→</span> TP <b class="tp">${plan.tp.map(fp).join(" / ") || "–"}</b> <span class="ar">·</span> SL <b class="sl">${fp(plan.sl)}</b>${a.rr ? ` <span class="ar">·</span> R:R <b>${esc(String(a.rr).slice(0, 24))}</b>` : ""}</div>`
      : `<div class="tr-plan mono">${a.entry ? `Entry <b>${esc(a.entry)}</b> ` : ""}${a.tp ? `→ TP <b class="tp">${esc(a.tp)}</b> ` : ""}${a.sl ? `· SL <b class="sl">${esc(a.sl)}</b>` : ""}</div>`;
    const up = +pr.up || 0, sw = +pr.sideways || 0, dn = +pr.down || 0, tot = up + sw + dn;
    const ho = I.holders, wh = I.whales, hl = I.hl, ht = I.hl_top, so = I.social || {}, vp = I.vp;
    const kv = (k, v) => v ? `<li><span>${k}</span><b>${v}</b></li>` : "";
    const smart = [ho && ho.count ? kv("Holders", `${ho.count.toLocaleString()}${ho.growth ? ` <i class="${ho.growth.delta >= 0 ? "pos" : "neg"}">${ho.growth.delta >= 0 ? "+" : ""}${ho.growth.delta} / ${ho.growth.hours}h</i>` : ""}`) : "",
      ho && ho.top10_pct != null ? kv("Top 10 hold", `${ho.top10_pct.toFixed(1)}%`) : "",
      wh ? kv(`Whales >${big(wh.min_usd)} 24h`, `<i class="pos">${big(wh.buys_usd)} buy</i> · <i class="neg">${big(wh.sells_usd)} sell</i>`) : "",
      hl ? kv("HL funding / OI", `${hl.funding_8h_pct.toFixed(4)}% · ${big(hl.oi_usd)}`) : "",
      ht ? kv("HL top traders", `<i class="pos">${ht.longs}L</i> / <i class="neg">${ht.shorts}S</i>`) : "",
      vp ? kv("POC · value area", `${fp(vp.poc)} · ${fp(vp.val)}–${fp(vp.vah)}`) : ""].join("");
    const quotes = (so.quotes || []).slice(0, 3).map(q => `<li class="${q.tone}">${q.url ? `<a href="${esc(q.url)}" target="_blank" rel="noopener">${esc(q.text)}</a>` : esc(q.text)}<small>${esc(q.src)}</small></li>`).join("");
    const news = (so.news || []).slice(0, 3).map(n => `<li>${/^https:\/\//.test(n.url) ? `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>` : esc(n.title)}</li>`).join("");
    $("ta-read").innerHTML = `
      ${card("tr-verdict " + cls, "Verdict · what Mirko does now", `<p class="tr-sum">${esc(r.summary || "")}</p><div class="tr-act"><span class="act-t ${cls}">${esc(a.type || "No trade")}</span>${pl}</div>`)}
      ${card("tr-pred", "Next move" + (pr.horizon ? ` · ${esc(pr.horizon)}` : ""), tot ? `<div class="pbars">${[["▲ Up", up, "u"], ["◆ Sideways", sw, "s"], ["▼ Down", dn, "d"]].map(([l, v, c]) => `<div class="pb ${c}"><span>${l}</span><div><i style="width:${v / tot * 100}%"></i></div><b class="mono">${Math.round(v / tot * 100)}%</b></div>`).join("")}</div>${P(pr.next_move)}` : P("No probability split this time."))}
      ${card("tr-struct", "Structure &amp; levels", P(r.structure) + P(r.levels) + P(r.indicators) + (sc.bull || sc.bear ? `<ul class="tr-sc"><li class="pos">▲ ${esc(sc.bull || "")}</li><li>◆ ${esc(sc.base || "")}</li><li class="neg">▼ ${esc(sc.bear || "")}</li></ul>` : ""))}
      ${card("tr-smart", "Smart money &amp; holders", P(r.smart_money) + (smart ? `<ul class="tr-kv">${smart}</ul>` : "") + P(r.liquidity))}
      ${card("tr-sent", "Sentiment &amp; news", P(r.sentiment) + `<div class="mono sm muted">Reddit 7d · ${so.mentions_7d || 0} posts · <span class="pos">${so.bull || 0} bullish</span> · <span class="neg">${so.bear || 0} bearish</span></div>` + (quotes ? `<ul class="tr-q">${quotes}</ul>` : "") + (news ? `<ul class="tr-n">${news}</ul>` : ""))}
      ${card("tr-thesis", "Thesis", r.thesis ? `<p class="tr-th">${esc(r.thesis)}</p>` : `<p class="muted">No thesis worth writing — Mirko only writes one when the setup earns it.</p>`)}
      <p class="muted sm tr-foot">Not financial advice · AI read on free public data · ${d.via === "rules" ? "rule-based fallback" : "reasoned read"}</p>`;
    $("ta-read").classList.remove("hidden");
  }
  async function go(q) {
    if (busy || !q) return; busy = true; $("ta-q").value = q;
    $("ta-meta").innerHTML = `<div class="ta-loading"><span class="spin"></span>Reading ${esc(q)}: candles, holders, whale flows, chatter… (≈20–40s)</div>`; $("ta-read").classList.add("hidden");
    try { const r = await fetch(`/api/ta?q=${encodeURIComponent(q)}`), d = await r.json();
      if (d.error) $("ta-meta").innerHTML = `<div class="empty">${esc(d.error)}</div>`; else { header(d); draw(d); read(d); }
    } catch { $("ta-meta").innerHTML = `<div class="empty">Connection hiccup — try again.</div>`; }
    busy = false;
  }
  async function init() {
    if (inited) return; inited = true;
    try { const p = await (await fetch("/api/ta/picks")).json(); $("ta-picks").innerHTML = p.picks.map(s => `<button class="src-chip" type="button">${esc(s)}</button>`).join(""); } catch {}
    go("BTC");
  }
  $("ta-form").addEventListener("submit", e => { e.preventDefault(); go($("ta-q").value.trim()); });
  $("ta-picks").addEventListener("click", e => { const b = e.target.closest(".src-chip"); if (b) go(b.textContent); });
  new ResizeObserver(() => { const w = $("ta-chart").clientWidth; if (chart && w) { chart.applyOptions({ width: w, height: w < 600 ? 360 : 520 }); requestAnimationFrame(overlay); } }).observe($("ta-chart"));
  const chk = () => { if (!$("view-ta").classList.contains("hidden")) init(); };
  window.addEventListener("ledger:view", chk); chk();
})();
