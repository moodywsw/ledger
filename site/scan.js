// Mirko's mind — live scan. Vanilla canvas, driven by real data in window.LEDGER.
(() => {
  const cv = document.getElementById("scan"); if (!cv) return;
  const ctx = cv.getContext("2d"), reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const mobile = matchMedia("(max-width: 700px)").matches, DPR = Math.min(devicePixelRatio || 1, mobile ? 1.5 : 2);
  const DOMAINS = [
    { id: "market", label: "market", col: [124, 140, 255] }, { id: "smart", label: "smart money", col: [34, 211, 230] },
    { id: "kols", label: "KOLs", col: [255, 110, 199] }, { id: "risk", label: "risk", col: [255, 92, 122] },
    { id: "memory", label: "memory", col: [245, 184, 75] }, { id: "news", label: "news", col: [160, 120, 255] },
    { id: "social", label: "social media", col: [80, 230, 140] }, { id: "politics", label: "politics", col: [255, 150, 90] },
    { id: "agents", label: "agents", col: [190, 240, 255] }];
  let W = 0, H = 0, stars = [], gal = [], spider = { x: 0, y: 0, tx: 0, ty: 0, target: 0, dwell: 0, legs: [] }, tags = [], logLines = [], events = 0, visible = true, last = 0;
  const rnd = (a, b) => a + Math.random() * (b - a);

  let far = null, mid = null, px = 0, py = 0, mx = 0, my = 0, drift = 0;
  const off = (w, h) => { const c = document.createElement("canvas"); c.width = w * DPR; c.height = h * DPR; const x = c.getContext("2d"); x.setTransform(DPR, 0, 0, DPR, 0, 0); return [c, x]; };
  const PAD = 60;
  function nebula(x, w, h) {
    const cols = [[110, 60, 200], [30, 140, 190], [200, 60, 140], [60, 90, 200], [180, 120, 60]];
    x.globalCompositeOperation = "lighter";
    for (let i = 0; i < (mobile ? 10 : 18); i++) { const c = cols[i % cols.length], cx = rnd(0, w), cy = rnd(0, h), r = rnd(80, 260);
      for (let k = 0; k < 4; k++) { const ox = cx + rnd(-r, r) * .5, oy = cy + rnd(-r, r) * .3, rr = r * rnd(.4, 1), g = x.createRadialGradient(ox, oy, 0, ox, oy, rr);
        g.addColorStop(0, `rgba(${c},${rnd(.025, .06)})`); g.addColorStop(1, "rgba(0,0,0,0)"); x.fillStyle = g; x.beginPath(); x.ellipse(ox, oy, rr, rr * rnd(.4, .9), rnd(0, 3), 0, 6.283); x.fill(); } }
    // dark dust lanes
    x.globalCompositeOperation = "source-over";
    for (let i = 0; i < 6; i++) { const cx = rnd(0, w), cy = rnd(0, h), g = x.createRadialGradient(cx, cy, 0, cx, cy, rnd(60, 160)); g.addColorStop(0, "rgba(3,4,8,.35)"); g.addColorStop(1, "rgba(3,4,8,0)"); x.fillStyle = g; x.fillRect(cx - 200, cy - 200, 400, 400); }
  }
  function starfield(x, w, h, n, max) {
    for (let i = 0; i < n; i++) { const t = Math.random(), s = Math.pow(Math.random(), 3) * max + .3, hue = t < .15 ? "255,210,170" : t < .3 ? "170,200,255" : "235,238,255";
      x.fillStyle = `rgba(${hue},${rnd(.25, .9)})`; x.fillRect(rnd(0, w), rnd(0, h), s, s);
      if (s > max * .7) { const X = rnd(0, w), Y = rnd(0, h), g = x.createRadialGradient(X, Y, 0, X, Y, s * 4); g.addColorStop(0, `rgba(${hue},.5)`); g.addColorStop(1, "rgba(0,0,0,0)"); x.fillStyle = g; x.fillRect(X - s * 4, Y - s * 4, s * 8, s * 8); } }
  }
  function bgGalaxy(x, cx, cy, size, kind) {
    const rot = rnd(0, 6.283), tilt = rnd(.25, 1), warm = Math.random() < .5, core = warm ? "255,225,180" : "200,215,255", arm = warm ? "190,170,255" : "140,190,255";
    const g = x.createRadialGradient(cx, cy, 0, cx, cy, size * (kind === "e" ? 1 : .45));
    g.addColorStop(0, `rgba(${core},.55)`); g.addColorStop(.3, `rgba(${core},.14)`); g.addColorStop(1, "rgba(0,0,0,0)");
    x.save(); x.translate(cx, cy); x.rotate(rot); x.scale(1, kind === "e" ? rnd(.5, .9) : tilt); x.translate(-cx, -cy); x.fillStyle = g; x.beginPath(); x.arc(cx, cy, size, 0, 6.283); x.fill();
    if (kind === "s") { const arms = Math.random() < .6 ? 2 : 3, n = Math.round(size * 9);
      for (let i = 0; i < n; i++) { const a = i % arms * 6.283 / arms, t = Math.random() * 2.6, r = size * .12 * Math.exp(.6 * t) * .55, th = a + t * 1.6 + rnd(-.35, .35);
        x.fillStyle = Math.random() < .12 ? "rgba(255,150,200,.55)" : `rgba(${arm},${rnd(.15, .55)})`; x.fillRect(cx + Math.cos(th) * r + rnd(-1.5, 1.5), cy + Math.sin(th) * r + rnd(-1.5, 1.5), rnd(.5, 1.4), rnd(.5, 1.4)); } }
    x.restore();
  }
  let ready = false;
  function layout() {
    const r = cv.getBoundingClientRect(); if (r.width < 20 || r.height < 20) { ready = false; return; } W = r.width; H = r.height; cv.width = W * DPR; cv.height = H * DPR; ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    const fw = W + PAD * 2, fh = H + PAD * 2; let fx, mx2;
    [far, fx] = off(fw, fh); fx.fillStyle = "#03040a"; fx.fillRect(0, 0, fw, fh); nebula(fx, fw, fh); starfield(fx, fw, fh, Math.min(2600, fw * fh / (mobile ? 420 : 260)), 1.4);
    [mid, mx2] = off(fw, fh); starfield(mx2, fw, fh, mobile ? 120 : 260, 2.2);
    const ng = mobile ? 26 : 60;
    for (let i = 0; i < ng; i++) { const d = Math.pow(Math.random(), 2); bgGalaxy(mx2, rnd(0, fw), rnd(0, fh), 6 + d * (mobile ? 26 : 38), Math.random() < .62 ? "s" : "e"); }
    const pos = mobile ? [[.2, .18], [.72, .18], [.25, .38], [.8, .36], [.2, .63], [.75, .6], [.3, .87], [.75, .87], [.5, .5]]
      : [[.14, .2], [.42, .14], [.72, .16], [.86, .46], [.16, .56], [.5, .5], [.3, .84], [.72, .82], [.88, .14]];
    const n = mobile ? 90 : 220;
    gal = DOMAINS.map((d, i) => ({ ...d, x: pos[i][0] * W, y: pos[i][1] * H, r: Math.min(W, H) * (mobile ? .12 : .13), heat: .3, info: "", rot: rnd(0, 6), tilt: rnd(.55, .85),
      dots: Array.from({ length: n }, (_, k) => { const arm = k % 2 * Math.PI, t = Math.random() * 2.4, rr = Math.min(1, .08 * Math.exp(.9 * t)), core = Math.random() < .3;
        return core ? { a: rnd(0, 6.283), rr: Math.pow(Math.random(), 2) * .35, s: rnd(.6, 1.8), tw: rnd(0, 6.283), sp: rnd(.0004, .0009) }
          : { a: arm + t * 1.9 + rnd(-.3, .3), rr, s: rnd(.6, 2), tw: rnd(0, 6.283), sp: .0005 / (rr + .3) }; }) }));
    stars = Array.from({ length: mobile ? 25 : 60 }, () => ({ x: rnd(0, W), y: rnd(0, H), s: rnd(1, 2.2), tw: rnd(0, 6) }));
    spider.x = spider.tx = W / 2; spider.y = spider.ty = H / 2;
    spider.legs = Array.from({ length: 16 }, (_, i) => ({ a: i / 16 * 6.283, len: rnd(16, 30), ph: rnd(0, 6) }));
    ready = true; spider.target = Math.min(spider.target, gal.length - 1); nextTarget();
  }
  cv.addEventListener("pointermove", e => { const r = cv.getBoundingClientRect(); mx = ((e.clientX - r.left) / r.width - .5) * 2; my = ((e.clientY - r.top) / r.height - .5) * 2; });
  cv.addEventListener("pointerleave", () => { mx = my = 0; });

  // ── real-data tags per domain ──
  function facts(id) {
    const L = window.LEDGER || {}, m = L.market && L.market.regime && Array.isArray(L.market.assets) ? L.market : null, out = [];
    if (id === "market" && m) { m.assets.forEach(a => out.push(`${a.name} · ${a.bias.split(" ·")[0].toLowerCase()}`)); out.push(`regime · ${m.regime.label.toLowerCase()}`); if (m.fng) out.push(`F&G · ${m.fng.v}`); }
    if (id === "smart" && m) { const hl = m.hyperliquid; if (hl && hl.coins) Object.entries(hl.coins).forEach(([c, h]) => out.push(`whales · ${c} ${Math.round((h.long_share || 0) * 100)}% long`));
      (m.dex_flows || []).slice(0, 3).forEach(d => out.push(`${d.pair} · ${d.buy_share > .55 ? "whale buy" : "flow"} ${Math.round(d.buy_share * 100)}%`)); }
    if (id === "kols") { (m && m.trending || []).slice(0, 4).forEach(t => out.push(`trending · ${t.symbol}`)); out.push("wallets · watching"); }
    if (id === "risk") { (L.journal || []).filter(e => e.kind === "refused").slice(0, 4).forEach(e => out.push(`${e.token_ticker || "token"} · ${/rug|honeypot|bundle|scam/i.test(e.text) ? "rug · flagged" : "skipped"}`));
      if (m) out.push(m.size_scale < 1 ? `size ×${m.size_scale}` : "size · full"); }
    if (id === "memory") { const md = L.mood; if (md) out.push(`mood · ${md.label}`); (L.posts || []).filter(p => p.kind === "exit_win" || p.kind === "exit_loss").slice(0, 3).forEach(p => out.push(p.kind === "exit_win" ? "lesson · win" : "lesson · loss")); }
    if (id === "news") { (L.posts || []).filter(p => p.kind === "musing" && p.text).slice(0, 3).forEach(p => out.push(`${(p.topic || "thought").replace("_", " ")} · ${p.text.split(" ").slice(0, 3).join(" ")}…`)); }
    const S = L.social;
    if (id === "social" && S) { (S.hot_tickers || []).slice(0, 3).forEach(t => out.push(`reddit · $${t}`)); (S.x || []).slice(0, 3).forEach(x => out.push(`X · @${x.user}`));
      if (S.tone) out.push(`crowd · ${S.tone.crypto > .1 ? "greedy" : S.tone.crypto < -.1 ? "fearful" : "mixed"}`); }
    const C = L.council;
    if (id === "agents" && C && C.lines) C.lines.filter(l => l.agent !== "Mirko").forEach(l => out.push(`${l.agent} · ${l.text.split(" ").slice(0, 4).join(" ")}…`));
    if (id === "politics" && S) { ((S.reddit || {}).politics || []).slice(0, 3).forEach(h => out.push(`politics · ${h.split(" ").slice(0, 3).join(" ")}…`)); }
    return out.length ? out : [`${id} · scanning`];
  }
  function heat(g) { const L = window.LEDGER || {}, m = L.market && L.market.regime ? L.market : null;
    if (g.id === "market" && m) return Math.min(1, Math.abs(m.regime.score) / 4 + .3);
    if (g.id === "risk") return m && m.regime.key === "risk_off" ? .9 : .4;
    if (g.id === "memory" && L.mood) return .3 + (L.mood.confidence || .5) * .6;
    return .45; }

  const evts = [];
  function pushLog(t) { logLines.unshift(t); logLines.length = Math.min(logLines.length, 7); evts.push(Date.now());
    const el = document.getElementById("scan-log"); if (el) el.innerHTML = logLines.map((l, i) => `<div style="opacity:${1 - i * .12}">${l.replace(/</g, "&lt;")}</div>`).join(""); }

  function nextTarget() {
    spider.target = (spider.target + 1 + Math.floor(Math.random() * (gal.length - 1))) % gal.length;
    const g = gal[spider.target]; spider.tx = g.x + rnd(-g.r * .3, g.r * .3); spider.ty = g.y + rnd(-g.r * .3, g.r * .3); spider.dwell = 0;
  }
  function arrive() {
    const g = gal[spider.target], fs = facts(g.id); g.heat = heat(g);
    const pick = fs.sort(() => Math.random() - .5).slice(0, 2);
    pick.forEach((t, i) => tags.push({ t, x: spider.x + rnd(-80, 60), y: spider.y + rnd(-60, 40) + i * 22, life: 0, col: g.col }));
    tags = tags.slice(-6);
    const w = document.getElementById("wolf"), dr = document.getElementById("dream");  // his breathing glow follows the scan
    if (w) w.style.setProperty("--dream", (0.25 + g.heat * 0.75).toFixed(2));
    const SRC = { market: "binance+coingecko", smart: "hyperliquid+dex", kols: "coingecko trending", risk: "own journal", memory: "own memory",
      news: "rss feeds", social: "reddit+x", politics: "r/politics+worldnews", agents: "agent council" };
    pushLog(`${g.label.padEnd(12)} ← ${SRC[g.id] || "scan"} · ${pick[0] || ""}`);
  }

  function draw(t) {
    drift += reduce ? 0 : .02; px += (mx - px) * .04; py += (my - py) * .04;
    const fxo = -PAD + Math.sin(drift / 40) * 8 - px * 10, fyo = -PAD + Math.cos(drift / 55) * 6 - py * 8;
    ctx.drawImage(far, fxo, fyo, W + PAD * 2, H + PAD * 2);
    ctx.drawImage(mid, -PAD + Math.sin(drift / 40) * 18 - px * 26, -PAD + Math.cos(drift / 55) * 14 - py * 20, W + PAD * 2, H + PAD * 2);
    for (const s of stars) { const a = .2 + .8 * Math.sin(t / 700 + s.tw) ** 2; ctx.fillStyle = `rgba(230,236,255,${a})`; ctx.fillRect(s.x - px * 30, s.y - py * 24, s.s, s.s); }
    ctx.globalCompositeOperation = "lighter";
    for (const g of gal) {
      const [r, gg, b] = g.col, glow = ctx.createRadialGradient(g.x, g.y, 0, g.x, g.y, g.r * 1.2);
      glow.addColorStop(0, `rgba(${r},${gg},${b},${.18 + g.heat * .16})`); glow.addColorStop(.25, `rgba(${r},${gg},${b},.06)`); glow.addColorStop(1, "rgba(0,0,0,0)"); ctx.fillStyle = glow; ctx.beginPath(); ctx.arc(g.x, g.y, g.r * 1.2, 0, 6.283); ctx.fill();
      const cr = Math.cos(g.rot), sr = Math.sin(g.rot);
      for (const d of g.dots) { d.a += d.sp * 16; const ux = Math.cos(d.a) * d.rr * g.r, uy = Math.sin(d.a) * d.rr * g.r * g.tilt, x = g.x + ux * cr - uy * sr, y = g.y + ux * sr + uy * cr;
        ctx.fillStyle = `rgba(${r},${gg},${b},${.3 + .7 * Math.abs(Math.sin(t / 1200 + d.tw))})`; ctx.fillRect(x, y, d.s, d.s); }
    }
    ctx.globalCompositeOperation = "source-over";
    for (const g of gal) { const [r, gg, b] = g.col;
      ctx.font = "600 12px JetBrains Mono, monospace"; ctx.fillStyle = `rgba(${r},${gg},${b},.95)`; ctx.fillText(g.label, g.x - g.r * .5, g.y - g.r * .85);
      ctx.font = "10px JetBrains Mono, monospace"; ctx.fillStyle = "rgba(160,170,190,.75)"; ctx.fillText(g.info || `${Math.round(g.heat * 100)}% active`, g.x - g.r * .5, g.y - g.r * .85 + 14); }
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
  let convIdx = 0;
  function convList() {
    const L = window.LEDGER || {}, m = L.market && L.market.regime ? L.market : null, md = L.mood || {}, out = [];
    if (m) {
      const reg = m.regime;
      let c = .5 + Math.max(-1, Math.min(1, Math.abs(reg.score) / 4)) * .25 + ((md.confidence ?? .5) - .5) * .5 - (md.tilt || 0) * .2;
      out.push({ k: "MARKET", v: Math.round(Math.max(.05, Math.min(.98, c)) * 100), tone: reg.key === "risk_on" ? "bull" : reg.key === "risk_off" ? "bear" : "neutral",
        d: `${reg.label} · ${reg.key === "risk_off" ? "defensive size" : reg.key === "risk_on" ? "full size" : "normal size"}` });
      const order = ["BTC", "ETH", "SOL"];
      (m.assets || []).sort((a, b) => order.indexOf(a.name) - order.indexOf(b.name)).forEach(a => out.push({ k: a.name, v: Math.round(Math.max(5, Math.min(95, 50 + a.score * 8))), tone: a.tone,
        d: a.tone === "bull" ? "trending up, buying dips" : a.tone === "bear" ? "weak, selling rips" : "range, waiting for a breakout" }));
      (m.stocks_conv || []).forEach(x => out.push({ k: x.name, v: x.conv, tone: x.tone, d: `${x.note} · 1M ${x.chg1m > 0 ? "+" : ""}${x.chg1m}%` }));
    }
    return out;
  }
  function drawConv() {
    const items = convList(); if (!items.length) return;
    const it = items[convIdx % items.length], conv = it.v / 100;
    const col = it.v >= 65 ? "#22d39b" : it.v >= 45 ? "#f5b84b" : "#ff5c7a";
    const c = gauge.getContext("2d"), w = gauge.width = gauge.clientWidth * DPR, h = gauge.height = gauge.clientHeight * DPR, cx = w / 2, cy = h * .9, R = Math.min(w / 2, h) * .78;
    c.clearRect(0, 0, w, h); c.lineWidth = 7 * DPR; c.lineCap = "round"; c.strokeStyle = "#1a2230"; c.beginPath(); c.arc(cx, cy, R, Math.PI, 0); c.stroke();
    c.strokeStyle = col; c.shadowColor = col; c.shadowBlur = 8 * DPR; c.beginPath(); c.arc(cx, cy, R, Math.PI, Math.PI + Math.PI * conv); c.stroke();
    const v = document.getElementById("gauge-v"); v.textContent = it.v; v.style.color = col;
    const a = document.getElementById("conv-asset"); if (a) { a.textContent = it.k; a.style.color = col; }
    const d = document.getElementById("conv-d"); if (d) d.textContent = it.d;
  }
  setInterval(() => { if (!document.hidden && gauge) { convIdx++; try { drawConv(); } catch (e) {} } }, 5000);
  function minis() {
    if (!ready) return;
    const L = window.LEDGER || {}, m = L.market && L.market.regime && Array.isArray(L.market.assets) ? L.market : null;
    if (radar && radar.clientWidth > 10) { const c = radar.getContext("2d"), w = radar.width = radar.clientWidth * DPR, h = radar.height = radar.clientHeight * DPR, cx = w / 2, cy = h / 2, R = Math.min(w, h) * .38;
      c.clearRect(0, 0, w, h); c.strokeStyle = "rgba(125,138,156,.25)";
      for (let k = 1; k <= 3; k++) { c.beginPath(); gal.forEach((g, i) => { const a = i / gal.length * 6.283 - 1.57; c[i ? "lineTo" : "moveTo"](cx + Math.cos(a) * R * k / 3, cy + Math.sin(a) * R * k / 3); }); c.closePath(); c.stroke(); }
      c.beginPath(); gal.forEach((g, i) => { const a = i / gal.length * 6.283 - 1.57, v = R * (.25 + g.heat * .75); c[i ? "lineTo" : "moveTo"](cx + Math.cos(a) * v, cy + Math.sin(a) * v); }); c.closePath();
      c.fillStyle = "rgba(34,211,230,.18)"; c.strokeStyle = "#22d3e6"; c.lineWidth = 1.5 * DPR; c.fill(); c.stroke(); }
    const heatEl = document.getElementById("heat");
    if (heatEl) { const cells = []; const vals = m ? [...m.assets.map(a => a.score / 6), ...(m.dex_flows || []).map(d => (d.buy_share - .5) * 4), ...(m.trending || []).map(t => (t.chg24 || 0) / 30)] : [];
      while (cells.length < 40) cells.push(vals.length ? vals[cells.length % vals.length] * (0.6 + 0.4 * Math.random()) : (Math.random() - .5) * .3);
      heatEl.innerHTML = cells.map(v => `<i style="background:${v >= 0 ? `rgba(34,211,155,${.15 + Math.min(1, v) * .8})` : `rgba(255,92,122,${.15 + Math.min(1, -v) * .8})`}"></i>`).join("");
      document.getElementById("heat-n").textContent = m ? `${vals.length} signals` : "—"; }
    if (gauge) drawConv();
    const rate = document.getElementById("scan-rate"); while (evts.length && evts[0] < Date.now() - 60000) evts.shift(); if (rate) rate.textContent = `${evts.length} reads/min`;
    gal.forEach(g => { g.heat = heat(g); });
  }

  function loop(t) {
    if (!visible) { running = false; return; }
    if (!ready) layout();
    if (ready && (!mobile || t - last > 33)) { try { draw(t); } catch (e) { errs++; if (errs % 50 === 1) console.warn("brain draw", e); tags = []; } last = t; }  // never let one bad frame kill the loop
    requestAnimationFrame(loop);
  }
  let running = false, errs = 0;
  function start() { if (reduce) { if (!ready) layout(); if (ready) try { draw(0); } catch (e) {} return; } if (!running) { running = true; requestAnimationFrame(loop); } }
  const onVis = () => { const v = !document.hidden && !document.getElementById("view-dash").classList.contains("hidden") && inView; visible = v; if (v) { if (!ready || Math.abs(cv.getBoundingClientRect().width - W) > 2) layout(); start(); } };
  let inView = true;
  new IntersectionObserver(es => { inView = es[0].isIntersecting; onVis(); }).observe(cv);
  document.addEventListener("visibilitychange", onVis); window.addEventListener("ledger:view", onVis);
  let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(() => { layout(); if (reduce && ready) draw(0); }, 200); });
  const safeMinis = () => { try { minis(); } catch (e) { console.warn("brain minis", e); } };
  layout(); pushLog("boot  mind online"); start(); safeMinis(); setInterval(() => { if (!document.hidden) safeMinis(); }, 10_000); window.addEventListener("ledger:data", safeMinis);
  if (reduce) setInterval(() => { if (!document.hidden && ready) { spider.x = spider.tx; spider.y = spider.ty; arrive(); nextTarget(); spider.x = spider.tx; spider.y = spider.ty; draw(0); } }, 6000);
})();
