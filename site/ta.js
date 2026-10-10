(() => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fp = x => x == null ? "–" : x >= 100 ? "$" + x.toLocaleString("en-US", { maximumFractionDigits: 2 }) : x >= 0.01 ? "$" + x.toFixed(4) : "$" + x.toPrecision(3);
  let chart, rsiChart, inited = false, busy = false;
  const LW = () => window.LightweightCharts;
  const opts = h => ({ height: h, layout: { background: { color: "transparent" }, textColor: "#8fa0b8", fontFamily: "ui-monospace, monospace" },
    grid: { vertLines: { color: "rgba(255,255,255,.04)" }, horzLines: { color: "rgba(255,255,255,.04)" } },
    rightPriceScale: { borderColor: "rgba(255,255,255,.08)" }, timeScale: { borderColor: "rgba(255,255,255,.08)", timeVisible: true }, crosshair: { mode: 0 } });
  function build() {
    $("ta-chart").innerHTML = ""; $("ta-rsi").innerHTML = "";
    const w = $("ta-chart").clientWidth, mob = w < 600;
    chart = LW().createChart($("ta-chart"), { ...opts(mob ? 320 : 440), width: w, localization: { priceFormatter: p => p >= 100 ? p.toFixed(2) : p >= 0.01 ? p.toFixed(4) : p.toPrecision(3) } });
    rsiChart = LW().createChart($("ta-rsi"), { ...opts(110), width: w });
    chart.timeScale().subscribeVisibleLogicalRangeChange(r => r && rsiChart.timeScale().setVisibleLogicalRange(r));
  }
  function line(c, data, color, width = 1, style = 0) { const s = c.addLineSeries({ color, lineWidth: width, lineStyle: style, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false }); s.setData(data); return s; }
  function draw(d) {
    build();
    const cs = chart.addCandlestickSeries({ upColor: "#22d39b", downColor: "#ff5c7a", borderVisible: false, wickUpColor: "#22d39b", wickDownColor: "#ff5c7a" });
    cs.setData(d.bars.map(b => ({ time: b.time, open: b.open, high: b.high, low: b.low, close: b.close })));
    const vs = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: .82, bottom: 0 } });
    vs.setData(d.bars.map(b => ({ time: b.time, value: b.value, color: b.close >= b.open ? "rgba(34,211,155,.35)" : "rgba(255,92,122,.35)" })));
    line(chart, d.series.ema20, "#7aa2ff"); line(chart, d.series.ema50, "#ffd166"); if (d.series.ema200.length) line(chart, d.series.ema200, "#c084fc", 2);
    (d.ta.support || []).forEach((p, i) => cs.createPriceLine({ price: p, color: "rgba(34,211,155,.75)", lineWidth: i ? 1 : 2, lineStyle: 2, axisLabelVisible: true, title: `S${i + 1}` }));
    (d.ta.resistance || []).forEach((p, i) => cs.createPriceLine({ price: p, color: "rgba(255,92,122,.75)", lineWidth: i ? 1 : 2, lineStyle: 2, axisLabelVisible: true, title: `R${i + 1}` }));
    for (const tl of [d.ta.tl_high, d.ta.tl_low]) if (tl && tl.from.t < tl.to.t) line(chart, [{ time: tl.from.t, value: tl.from.v }, { time: tl.to.t, value: tl.to.v }], "rgba(255,255,255,.55)", 1, 0);
    const P = d.ta.paths, col = { bull: "#22d39b", base: "#8fa0b8", bear: "#ff5c7a" };
    for (const k of ["bull", "base", "bear"]) { const s = line(chart, P[k].map(x => ({ time: x.t, value: x.v })), col[k], 2, 2);
      s.setMarkers([{ time: P[k][P[k].length - 1].t, position: k === "bear" ? "belowBar" : "aboveBar", color: col[k], shape: k === "bear" ? "arrowDown" : k === "bull" ? "arrowUp" : "circle", text: k }]); }
    const rs = line(rsiChart, d.series.rsi, "#c084fc", 1.5);
    rs.createPriceLine({ price: 70, color: "rgba(255,92,122,.5)", lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
    rs.createPriceLine({ price: 30, color: "rgba(34,211,155,.5)", lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, d.bars.length - 140), to: d.bars.length + 20 });
  }
  function meta(d) {
    const m = d.meta, t = d.ta;
    $("ta-meta").innerHTML = `<div class="tm-l">${m.logo ? `<img src="${esc(m.logo)}" alt="" class="coin-logo lg" referrerpolicy="no-referrer">` : `<span class="coin-logo lg ltr">${esc((m.symbol || "?")[0])}</span>`}
      <div><b>${esc(m.symbol || d.q)}</b> <span class="muted">${esc(m.name || "")}</span><div class="muted sm mono">${esc(m.chain)} · ${esc(m.source)} · ${esc(d.tf)}</div></div></div>
      <div class="tm-kv mono"><span>Price <b>${fp(t.price)}</b></span><span>~7d <b class="${t.chg >= 0 ? "pos" : "neg"}">${t.chg >= 0 ? "+" : ""}${t.chg.toFixed(1)}%</b></span><span>RSI <b>${t.rsi ? t.rsi.toFixed(0) : "–"}</b></span>
      ${m.mcap ? `<span>Mcap <b>$${(m.mcap / 1e6).toFixed(2)}M</b></span>` : ""}${m.liq ? `<span>Liq <b>$${(m.liq / 1e3).toFixed(0)}k</b></span>` : ""}</div>`;
  }
  function read(d) {
    const r = d.read, a = r.action || {}, c = r.conviction, emo = c >= 8 ? "🟢" : c >= 6 ? "🟡" : c >= 4 ? "🟠" : "🔴";
    const cls = /long/i.test(a.type) ? "long" : /short/i.test(a.type) ? "short" : "flat";
    const sc = r.scenarios || {};
    $("ta-read").innerHTML = `<div class="tr-h"><div><div class="label">Mirko's read · ${d.via === "rules" ? "rule-based" : "reasoned"}</div><h2>${esc(r.summary || "")}</h2></div>
        <div class="ta-conv"><span>${emo}</span><b class="mono">${c}</b><small>/10</small></div></div>
      <div class="ta-act ${cls}"><span class="act-t">${esc(a.type || "No trade")}</span>${a.entry ? `<span>Entry <b class="mono">${esc(a.entry)}</b></span>` : ""}${a.tp ? `<span>TP <b class="mono pos">${esc(a.tp)}</b></span>` : ""}${a.sl ? `<span>SL <b class="mono neg">${esc(a.sl)}</b></span>` : ""}${a.rr ? `<span>R:R <b class="mono">${esc(a.rr)}</b></span>` : ""}</div>
      <div class="dk-grid">
        <div class="dk-box"><h3>Structure</h3><p>${esc(r.structure || "")}</p></div>
        <div class="dk-box"><h3>Levels</h3><p>${esc(r.levels || "")}</p></div>
        <div class="dk-box"><h3>Indicators</h3><p>${esc(r.indicators || "")}</p></div>
        ${sc.bull || sc.bear ? `<div class="dk-box"><h3>Scenarios</h3><ul class="ta-sc"><li class="pos">▲ ${esc(sc.bull || "")}</li><li>● ${esc(sc.base || "")}</li><li class="neg">▼ ${esc(sc.bear || "")}</li></ul></div>` : ""}
      </div><p class="muted sm">Not financial advice. Paper analysis by an AI agent.</p>`;
    $("ta-read").classList.remove("hidden");
  }
  async function go(q) {
    if (busy || !q) return; busy = true; $("ta-q").value = q;
    $("ta-meta").innerHTML = `<div class="muted">Pulling candles and thinking about ${esc(q)}…</div>`; $("ta-read").classList.add("hidden");
    try {
      const r = await fetch(`/api/ta?q=${encodeURIComponent(q)}`); const d = await r.json();
      if (d.error) { $("ta-meta").innerHTML = `<div class="empty">${esc(d.error)}</div>`; }
      else { meta(d); draw(d); read(d); }
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
  new ResizeObserver(() => { const w = $("ta-chart").clientWidth; if (chart && w) { chart.applyOptions({ width: w }); rsiChart.applyOptions({ width: w }); } }).observe($("ta-chart"));
  const chk = () => { if (!$("view-ta").classList.contains("hidden")) init(); };
  window.addEventListener("ledger:view", chk); chk();
})();
