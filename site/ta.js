(() => {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fp = x => x == null ? "–" : x >= 100 ? "$" + x.toLocaleString("en-US", { maximumFractionDigits: 2 }) : x >= 0.01 ? "$" + x.toFixed(4) : "$" + x.toPrecision(3);
  let chart, rsiChart, inited = false, busy = false;
  const LW = () => window.LightweightCharts;
  const opts = h => ({ height: h, layout: { background: { color: "transparent" }, textColor: "#8fa0b8", fontFamily: "ui-monospace, monospace", attributionLogo: false },
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
    const vp = (d.intel || {}).vp; if (vp) cs.createPriceLine({ price: vp.poc, color: "rgba(255,209,102,.8)", lineWidth: 1, lineStyle: 1, axisLabelVisible: true, title: "POC" });
    for (const tl of [d.ta.tl_high, d.ta.tl_low]) if (tl && tl.from.t < tl.to.t) line(chart, [{ time: tl.from.t, value: tl.from.v }, { time: tl.to.t, value: tl.to.v }], "rgba(255,255,255,.55)", 1, 0);
    const P = d.ta.paths, col = { bull: "#22d39b", base: "#8fa0b8", bear: "#ff5c7a" };
    for (const k of ["bull", "base", "bear"]) { const s = line(chart, P[k].map(x => ({ time: x.t, value: x.v })), col[k], 2, 2);
      s.setMarkers([{ time: P[k][P[k].length - 1].t, position: k === "bear" ? "belowBar" : "aboveBar", color: col[k], shape: k === "bear" ? "arrowDown" : k === "bull" ? "arrowUp" : "circle", text: k }]); }
    const rs = line(rsiChart, d.series.rsi, "#c084fc", 1.5);
    rs.createPriceLine({ price: 70, color: "rgba(255,92,122,.5)", lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
    rs.createPriceLine({ price: 30, color: "rgba(34,211,155,.5)", lineStyle: 2, lineWidth: 1, axisLabelVisible: false });
    chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, d.bars.length - 140), to: d.bars.length + 20 });
  }
  const big = x => x == null ? "–" : x >= 1e9 ? `$${(x / 1e9).toFixed(2)}B` : x >= 1e6 ? `$${(x / 1e6).toFixed(2)}M` : x >= 1e3 ? `$${(x / 1e3).toFixed(0)}k` : `$${Math.round(x)}`;
  function meta(d) {
    const m = d.meta, t = d.ta, logo = m.logo || (window.LOGOS || {})[String(m.symbol || "").toUpperCase()];
    $("ta-meta").innerHTML = `<div class="tm-l">${logo ? `<img src="${esc(logo)}" alt="" class="coin-logo lg" referrerpolicy="no-referrer">` : `<span class="coin-logo lg ltr">${esc((m.symbol || "?")[0])}</span>`}
      <div><b class="tm-sym">${esc(m.symbol || d.q)}</b> <span class="muted">${esc(m.name && m.name !== m.symbol ? m.name : "")}</span><div class="muted sm mono">${esc(m.onchain ? `CEX + ${m.onchain}` : m.chain)} · ${esc(m.source)} · ${esc(d.tf)}${m.address ? ` · <span title="${esc(m.address)}">${esc(m.address.slice(0, 4))}…${esc(m.address.slice(-4))}</span>` : ""}</div></div></div>
      <div class="tm-kv mono"><span>Price <b>${fp(t.price)}</b></span><span>Window <b class="${t.chg >= 0 ? "pos" : "neg"}">${t.chg >= 0 ? "+" : ""}${t.chg.toFixed(1)}%</b></span><span>RSI <b>${t.rsi ? t.rsi.toFixed(0) : "–"}</b></span>
      ${m.mcap ? `<span>Mcap <b>${big(m.mcap)}</b></span>` : ""}${m.liq ? `<span>Liq <b>${big(m.liq)}</b></span>` : ""}${m.url ? `<a class="tm-link" href="${esc(m.url)}" target="_blank" rel="noopener">DexScreener ↗</a>` : ""}</div>`;
    const tv = $("ta-tv");
    if (m.tv) {
      const src = `https://s.tradingview.com/widgetembed/?symbol=${encodeURIComponent(m.tv)}&interval=240&hidesidetoolbar=0&symboledit=1&saveimage=0&toolbarbg=0b1018&theme=dark&style=1&timezone=Europe%2FLisbon&locale=en&studies=${encodeURIComponent('["MAExp@tv-basicstudies","RSI@tv-basicstudies"]')}`;
      if (tv.dataset.sym !== m.tv) { tv.innerHTML = `<div class="ta-own-h"><span class="hud-label">Live chart · TradingView · ${esc(m.tv)}</span></div><iframe title="TradingView chart" src="${src}" loading="lazy" referrerpolicy="no-referrer" sandbox="allow-scripts allow-same-origin allow-popups"></iframe>`; tv.dataset.sym = m.tv; }
      tv.classList.remove("hidden");
    } else { tv.classList.add("hidden"); tv.innerHTML = ""; tv.dataset.sym = ""; }
  }
  const box = (t, body, cls = "") => body ? `<div class="dk-box ${cls}"><h3>${t}</h3>${body}</div>` : "";
  const P = x => x ? `<p>${esc(x)}</p>` : "";
  function read(d) {
    const r = d.read, a = r.action || {}, c = r.conviction, emo = c >= 8 ? "🟢" : c >= 6 ? "🟡" : c >= 4 ? "🟠" : "🔴";
    const cls = /long/i.test(a.type) ? "long" : /short/i.test(a.type) ? "short" : "flat", pr = r.prediction || {}, I = d.intel || {};
    const up = +pr.up || 0, sw = +pr.sideways || 0, dn = +pr.down || 0, tot = up + sw + dn || 1;
    $("ta-verdict").innerHTML = `<div class="card verdict"><div class="tr-h"><div><div class="label">Mirko's verdict · ${d.via === "rules" ? "rule-based" : "reasoned"}</div><h2>${esc(r.summary || "")}</h2></div>
        <div class="ta-conv" title="Conviction"><span>${emo}</span><b class="mono">${c}</b><small>/10</small></div></div>
      <div class="ta-act ${cls}"><span class="act-t">${esc(a.type || "No trade")}</span>${a.entry ? `<span>Entry <b class="mono">${esc(a.entry)}</b></span>` : ""}${a.tp ? `<span>TP <b class="mono tp">${esc(a.tp)}</b></span>` : ""}${a.sl ? `<span>SL <b class="mono sl">${esc(a.sl)}</b></span>` : ""}${a.rr ? `<span>R:R <b class="mono">${esc(a.rr)}</b></span>` : ""}</div>
      ${tot > 1 ? `<div class="pred"><div class="pred-h"><span class="hud-label">Next move${pr.horizon ? " · " + esc(pr.horizon) : ""}</span><span class="sm">${esc(pr.next_move || "")}</span></div>
        <div class="pred-bar"><i class="u" style="width:${up / tot * 100}%">▲ ${Math.round(up / tot * 100)}%</i><i class="s" style="width:${sw / tot * 100}%">◆ ${Math.round(sw / tot * 100)}%</i><i class="d" style="width:${dn / tot * 100}%">▼ ${Math.round(dn / tot * 100)}%</i></div></div>` : ""}
      ${r.thesis ? `<div class="ta-thesis"><span class="hud-label">Thesis</span><p>${esc(r.thesis)}</p></div>` : ""}</div>`;
    const ho = I.holders, wh = I.whales, hl = I.hl, ht = I.hl_top, so = I.social || {}, vp = I.vp;
    const smData = [ho && ho.count ? `<li>Holders <b>${ho.count.toLocaleString()}</b>${ho.growth ? ` <span class="${ho.growth.delta >= 0 ? "pos" : "neg"}">${ho.growth.delta >= 0 ? "+" : ""}${ho.growth.delta} in ${ho.growth.hours}h</span>` : ""}</li>` : "",
      ho && ho.top10_pct != null ? `<li>Top 10 hold <b>${ho.top10_pct.toFixed(1)}%</b>${ho.top11_20_pct != null ? ` · next 10 <b>${ho.top11_20_pct.toFixed(1)}%</b>` : ""}</li>` : "",
      wh ? `<li>Whales >${big(wh.min_usd)} · 24h: buys <b class="pos">${big(wh.buys_usd)}</b> (${wh.n_buys}) vs sells <b class="neg">${big(wh.sells_usd)}</b> (${wh.n_sells})</li>` : "",
      hl ? `<li>Hyperliquid perp: funding <b>${hl.funding_8h_pct.toFixed(4)}%</b>/8h · OI <b>${big(hl.oi_usd)}</b></li>` : "",
      ht ? `<li>HL top traders: <b class="pos">${ht.longs} long</b> / <b class="neg">${ht.shorts} short</b></li>` : ""].join("");
    const vpHtml = vp ? `<div class="vp">${vp.bins.slice().reverse().map(b => `<i style="width:${Math.max(2, b.v * 100)}%" class="${b.p >= vp.val && b.p <= vp.vah ? "va" : ""}${Math.abs(b.p - vp.poc) < 1e-12 ? " poc" : ""}"></i>`).join("")}</div><p class="mono sm">POC ${fp(vp.poc)} · value area ${fp(vp.val)}–${fp(vp.vah)}</p>` : "";
    const quotes = (so.quotes || []).map(q => `<li class="q ${q.tone}"><span>${q.tone === "bull" ? "🟢" : q.tone === "bear" ? "🔴" : "⚪"}</span>${q.url ? `<a href="${esc(q.url)}" target="_blank" rel="noopener">${esc(q.text)}</a>` : esc(q.text)}<small>${esc(q.src)}</small></li>`).join("");
    const news = (so.news || []).slice(0, 4).map(n => `<li>${/^https:\/\//.test(n.url) ? `<a href="${esc(n.url)}" target="_blank" rel="noopener">${esc(n.title)}</a>` : esc(n.title)}</li>`).join("");
    const sc = r.scenarios || {};
    $("ta-secs").innerHTML = `<div class="dk-grid ta-grid">
      ${box("📐 Market structure", P(r.structure))}${box("🧱 Key levels", P(r.levels))}${box("📊 Indicators", P(r.indicators))}
      ${box("📶 Volume profile", P(r.volume_profile) + vpHtml)}
      ${box("🐋 Smart money", P(r.smart_money) + (smData ? `<ul class="kvl">${smData}</ul>` : ""))}
      ${box("💧 Liquidity", P(r.liquidity))}
      ${box("🗣 Social sentiment", P(r.sentiment) + `<p class="mono sm">Reddit 7d: ${so.mentions_7d || 0} posts · ${so.bull || 0} bullish · ${so.bear || 0} bearish</p>` + (quotes ? `<ul class="quotes">${quotes}</ul>` : ""))}
      ${box("📰 News", news ? `<ul class="news">${news}</ul>` : "<p class='muted'>No fresh headlines.</p>")}
      ${sc.bull || sc.bear ? box("🧭 Scenarios", `<ul class="ta-sc"><li class="pos">▲ ${esc(sc.bull || "")}</li><li>◆ ${esc(sc.base || "")}</li><li class="neg">▼ ${esc(sc.bear || "")}</li></ul>`) : ""}
    </div><p class="muted sm">Not financial advice. Analysis by an AI agent on free public data.</p>`;
    $("ta-verdict").classList.remove("hidden"); $("ta-secs").classList.remove("hidden");
  }
  async function go(q) {
    if (busy || !q) return; busy = true; $("ta-q").value = q;
    $("ta-meta").innerHTML = `<div class="ta-loading"><span class="spin"></span>Pulling candles, holders, whale flows and chatter on ${esc(q)} — Mirko is thinking (≈20–40s)…</div>`; $("ta-verdict").classList.add("hidden"); $("ta-secs").classList.add("hidden");
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
