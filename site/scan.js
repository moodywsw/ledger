// Ledger's mind — live scan. Vanilla canvas, driven by real data in window.LEDGER.
(() => {
  const cv = document.getElementById("scan"); if (!cv) return;
  const ctx = cv.getContext("2d"), reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const mobile = matchMedia("(max-width: 700px)").matches, DPR = Math.min(devicePixelRatio || 1, mobile ? 1.5 : 2);
  const DOMAINS = [
    { id: "market", label: "market", col: [124, 140, 255] }, { id: "smart", label: "smart money", col: [34, 211, 230] },
    { id: "kols", label: "KOLs", col: [255, 110, 199] }, { id: "risk", label: "risk", col: [255, 92, 122] },
    { id: "memory", label: "memory", col: [245, 184, 75] }, { id: "news", label: "news", col: [160, 120, 255] }];
  let W = 0, H = 0, stars = [], gal = [], spider = { x: 0, y: 0, tx: 0, ty: 0, target: 0, dwell: 0, legs: [] }, tags = [], logLines = [], events = 0, visible = true, last = 0;
  const rnd = (a, b) => a + Math.random() * (b - a);

  function layout() {
    const r = cv.getBoundingClientRect(); W = r.width; H = r.height; cv.width = W * DPR; cv.height = H * DPR; ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    const pos = mobile ? [[.22, .2], [.75, .18], [.2, .55], [.8, .52], [.3, .85], [.72, .86]] : [[.12, .3], [.36, .18], [.62, .22], [.88, .32], [.25, .78], [.72, .78]];
    const n = mobile ? 70 : 150;
    gal = DOMAINS.map((d, i) => ({ ...d, x: pos[i][0] * W, y: pos[i][1] * H, r: Math.min(W, H) * (mobile ? .16 : .17), heat: .3, info: "",
      dots: Array.from({ length: n }, () => { const a = rnd(0, 6.283), rr = Math.pow(Math.random(), .6); return { a, rr, s: rnd(.7, 2.2), tw: rnd(0, 6.283), sp: rnd(-.0006, .0006) }; }) }));
    stars = Array.from({ length: mobile ? 60 : 140 }, () => ({ x: rnd(0, W), y: rnd(0, H), s: rnd(.2, 1), tw: rnd(0, 6) }));
    spider.x = spider.tx = W / 2; spider.y = spider.ty = H / 2;
    spider.legs = Array.from({ length: 16 }, (_, i) => ({ a: i / 16 * 6.283, len: rnd(16, 30), ph: rnd(0, 6) }));
  }

  // ── real-data tags per domain ──
  function facts(id) {
    const L = window.LEDGER || {}, m = L.market, out = [];
    if (id === "market" && m) { m.assets.forEach(a => out.push(`${a.name} · ${a.bias.split(" ·")[0].toLowerCase()}`)); out.push(`regime · ${m.regime.label.toLowerCase()}`); if (m.fng) out.push(`F&G · ${m.fng.v}`); }
    if (id === "smart" && m) { const hl = m.hyperliquid; if (hl) Object.entries(hl.coins).forEach(([c, h]) => out.push(`whales · ${c} ${Math.round((h.long_share || 0) * 100)}% long`));
      (m.dex_flows || []).slice(0, 3).forEach(d => out.push(`${d.pair} · ${d.buy_share > .55 ? "whale buy" : "flow"} ${Math.round(d.buy_share * 100)}%`)); }
    if (id === "kols") { (m && m.trending || []).slice(0, 4).forEach(t => out.push(`trending · ${t.symbol}`)); out.push("wallets · watching"); }
    if (id === "risk") { (L.journal || []).filter(e => e.kind === "refused").slice(0, 4).forEach(e => out.push(`${e.token_ticker || "token"} · ${/rug|honeypot|bundle|scam/i.test(e.text) ? "rug · flagged" : "skipped"}`));
      if (m) out.push(m.size_scale < 1 ? `size ×${m.size_scale}` : "size · full"); }
    if (id === "memory") { const md = L.mood; if (md) out.push(`mood · ${md.label}`); (L.posts || []).filter(p => p.kind === "exit_win" || p.kind === "exit_loss").slice(0, 3).forEach(p => out.push(p.kind === "exit_win" ? "lesson · win" : "lesson · loss")); }
    if (id === "news") { (L.posts || []).filter(p => p.kind === "musing").slice(0, 3).forEach(p => out.push(`${(p.topic || "thought").replace("_", " ")} · ${p.text.split(" ").slice(0, 3).join(" ")}…`)); }
    return out.length ? out : [`${id} · scanning`];
  }
  function heat(g) { const L = window.LEDGER || {}, m = L.market;
    if (g.id === "market" && m) return Math.min(1, Math.abs(m.regime.score) / 4 + .3);
    if (g.id === "risk") return m && m.regime.key === "risk_off" ? .9 : .4;
    if (g.id === "memory" && L.mood) return .3 + (L.mood.confidence || .5) * .6;
    return .45; }

  function pushLog(t) { logLines.unshift(t); logLines.length = Math.min(logLines.length, 7); events++;
    const el = document.getElementById("scan-log"); if (el) el.innerHTML = logLines.map((l, i) => `<div style="opacity:${1 - i * .12}">${l.replace(/</g, "&lt;")}</div>`).join(""); }

  function nextTarget() {
    spider.target = (spider.target + 1 + Math.floor(Math.random() * (gal.length - 1))) % gal.length;
    const g = gal[spider.target]; spider.tx = g.x + rnd(-g.r * .3, g.r * .3); spider.ty = g.y + rnd(-g.r * .3, g.r * .3); spider.dwell = 0;
  }
  function arrive() {
    const g = gal[spider.target], fs = facts(g.id); g.heat = heat(g);
    const pick = fs.sort(() => Math.random() - .5).slice(0, 2);
    pick.forEach((t, i) => tags.push({ t, x: spider.x + rnd(-80, 60), y: spider.y + rnd(-60, 40) + i * 22, life: 0, col: g.col }));
    tags = tags.slice(-6); pushLog(`read  ${g.label.padEnd(11)} ${pick[0] || ""}`);
  }

  function draw(t) {
    ctx.clearRect(0, 0, W, H);
    for (const s of stars) { ctx.fillStyle = `rgba(200,210,255,${.15 + .25 * Math.sin(t / 900 + s.tw) ** 2})`; ctx.fillRect(s.x, s.y, s.s, s.s); }
    for (const g of gal) {
      const [r, gg, b] = g.col, glow = ctx.createRadialGradient(g.x, g.y, 0, g.x, g.y, g.r * 1.2);
      glow.addColorStop(0, `rgba(${r},${gg},${b},${.16 + g.heat * .14})`); glow.addColorStop(1, "rgba(0,0,0,0)"); ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(g.x, g.y, g.r * 1.2, 0, 6.283); ctx.fill();
      for (const d of g.dots) { d.a += d.sp * 16; const x = g.x + Math.cos(d.a) * d.rr * g.r, y = g.y + Math.sin(d.a) * d.rr * g.r * .8;
        ctx.fillStyle = `rgba(${r},${gg},${b},${.35 + .65 * Math.abs(Math.sin(t / 1200 + d.tw))})`; ctx.fillRect(x, y, d.s, d.s); }
      ctx.font = "600 12px JetBrains Mono, monospace"; ctx.fillStyle = `rgba(${r},${gg},${b},.95)`; ctx.fillText(g.label, g.x - g.r * .5, g.y - g.r * .85);
      ctx.font = "10px JetBrains Mono, monospace"; ctx.fillStyle = "rgba(160,170,190,.7)"; ctx.fillText(g.info || `${Math.round(g.heat * 100)}% active`, g.x - g.r * .5, g.y - g.r * .85 + 14);
    }
    // spider
    const dx = spider.tx - spider.x, dy = spider.ty - spider.y, dist = Math.hypot(dx, dy);
    if (dist > 2) { spider.x += dx * .02; spider.y += dy * .02; } else if (spider.dwell++ === 0) arrive(); else if (spider.dwell > 150) nextTarget();
    const g = gal[spider.target]; const [r, gg, b] = g.col;
    ctx.strokeStyle = `rgba(${r},${gg},${b},.35)`; ctx.lineWidth = 1; ctx.setLineDash([3, 4]); ctx.beginPath(); ctx.moveTo(spider.x, spider.y); ctx.lineTo(spider.tx, spider.ty); ctx.stroke(); ctx.setLineDash([]);
    for (const l of spider.legs) { const k = l.len + Math.sin(t / 180 + l.ph) * 5, ex = spider.x + Math.cos(l.a) * k, ey = spider.y + Math.sin(l.a) * k;
      ctx.strokeStyle = "rgba(120,240,220,.55)"; ctx.beginPath(); ctx.moveTo(spider.x, spider.y); ctx.lineTo(ex, ey); ctx.stroke(); ctx.fillStyle = "#7ff0dc"; ctx.fillRect(ex - 1, ey - 1, 2, 2); }
    const sg = ctx.createRadialGradient(spider.x, spider.y, 0, spider.x, spider.y, 22); sg.addColorStop(0, "rgba(255,110,199,.9)"); sg.addColorStop(1, "rgba(255,110,199,0)");
    ctx.fillStyle = sg; ctx.beginPath(); ctx.arc(spider.x, spider.y, 22, 0, 6.283); ctx.fill(); ctx.fillStyle = "#ff6ec7"; ctx.fillRect(spider.x - 4, spider.y - 4, 8, 8);
    // tags
    ctx.font = "11px JetBrains Mono, monospace";
    for (const tg of tags) { tg.life++; const a = Math.min(1, tg.life / 20) * (tg.life > 380 ? Math.max(0, 1 - (tg.life - 380) / 60) : 1); if (a <= 0) continue;
      const w = ctx.measureText(tg.t).width + 14; ctx.globalAlpha = a; ctx.strokeStyle = `rgba(${tg.col.join(",")},.6)`; ctx.fillStyle = "rgba(10,14,20,.85)";
      ctx.beginPath(); ctx.roundRect ? ctx.roundRect(tg.x, tg.y, w, 20, 4) : ctx.rect(tg.x, tg.y, w, 20); ctx.fill(); ctx.stroke();
      ctx.fillStyle = "#e6edf6"; ctx.fillText(tg.t, tg.x + 7, tg.y + 14); ctx.globalAlpha = 1; }
    tags = tags.filter(tg => tg.life < 440);
  }

  // ── mini panels ──
  const radar = document.getElementById("radar"), gauge = document.getElementById("gauge");
  function minis() {
    const L = window.LEDGER || {}, m = L.market;
    if (radar) { const c = radar.getContext("2d"), w = radar.width = radar.clientWidth * DPR, h = radar.height = radar.clientHeight * DPR, cx = w / 2, cy = h / 2, R = Math.min(w, h) * .38;
      c.clearRect(0, 0, w, h); c.strokeStyle = "rgba(125,138,156,.25)";
      for (let k = 1; k <= 3; k++) { c.beginPath(); gal.forEach((g, i) => { const a = i / gal.length * 6.283 - 1.57; c[i ? "lineTo" : "moveTo"](cx + Math.cos(a) * R * k / 3, cy + Math.sin(a) * R * k / 3); }); c.closePath(); c.stroke(); }
      c.beginPath(); gal.forEach((g, i) => { const a = i / gal.length * 6.283 - 1.57, v = R * (.25 + g.heat * .75); c[i ? "lineTo" : "moveTo"](cx + Math.cos(a) * v, cy + Math.sin(a) * v); }); c.closePath();
      c.fillStyle = "rgba(34,211,230,.18)"; c.strokeStyle = "#22d3e6"; c.lineWidth = 1.5 * DPR; c.fill(); c.stroke(); }
    const heatEl = document.getElementById("heat");
    if (heatEl) { const cells = []; const vals = m ? [...m.assets.map(a => a.score / 6), ...(m.dex_flows || []).map(d => (d.buy_share - .5) * 4), ...(m.trending || []).map(t => (t.chg24 || 0) / 30)] : [];
      while (cells.length < 40) cells.push(vals.length ? vals[cells.length % vals.length] * (0.6 + 0.4 * Math.random()) : (Math.random() - .5) * .3);
      heatEl.innerHTML = cells.map(v => `<i style="background:${v >= 0 ? `rgba(34,211,155,${.15 + Math.min(1, v) * .8})` : `rgba(255,92,122,${.15 + Math.min(1, -v) * .8})`}"></i>`).join("");
      document.getElementById("heat-n").textContent = m ? `${vals.length} signals` : "—"; }
    if (gauge) { const conf = L.mood ? (L.mood.confidence ?? .5) : .5, c = gauge.getContext("2d"), w = gauge.width = gauge.clientWidth * DPR, h = gauge.height = gauge.clientHeight * DPR, cx = w / 2, cy = h * .82, R = Math.min(w / 2, h) * .7;
      c.clearRect(0, 0, w, h); c.lineWidth = 8 * DPR; c.lineCap = "round"; c.strokeStyle = "#1a2230"; c.beginPath(); c.arc(cx, cy, R, Math.PI, 0); c.stroke();
      c.strokeStyle = conf > .6 ? "#22d39b" : conf < .4 ? "#ff5c7a" : "#f5b84b"; c.beginPath(); c.arc(cx, cy, R, Math.PI, Math.PI + Math.PI * conf); c.stroke();
      document.getElementById("gauge-v").textContent = `${Math.round(conf * 100)}`; }
    const rate = document.getElementById("scan-rate"); if (rate) rate.textContent = `${(events / 10).toFixed(1)} ev/s`; events = 0;
    const sub = document.getElementById("scan-sub"); if (sub && m) sub.textContent = `regime · ${m.regime.label.toLowerCase()} · ${L.mood ? "mood · " + L.mood.label : ""}`;
    gal.forEach(g => { g.heat = heat(g); });
  }

  function loop(t) {
    if (!visible) return;
    if (!mobile || t - last > 33) { draw(t); last = t; }  // ~30fps cap on phones
    requestAnimationFrame(loop);
  }
  function start() { if (reduce) { draw(0); return; } requestAnimationFrame(loop); }
  const onVis = () => { const v = !document.hidden && !document.getElementById("view-dash").classList.contains("hidden") && inView; if (v && !visible) { visible = true; start(); } else visible = v; };
  let inView = true;
  new IntersectionObserver(es => { inView = es[0].isIntersecting; onVis(); }).observe(cv);
  document.addEventListener("visibilitychange", onVis); window.addEventListener("ledger:view", onVis);
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { layout(); if (reduce) draw(0); }, 200); });
  layout(); nextTarget(); pushLog("boot  mind online"); start(); minis(); setInterval(() => { if (!document.hidden) minis(); }, 10_000);
  if (reduce) setInterval(() => { if (!document.hidden) { spider.x = spider.tx; spider.y = spider.ty; arrive(); nextTarget(); spider.x = spider.tx; spider.y = spider.ty; draw(0); } }, 6000);
})();
