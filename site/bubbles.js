// Market bubbles v3: canvas physics (wander drift + elastic collisions), drag & throw, hover glow, click opens coin.
(() => {
  const cv = document.getElementById("bub"); if (!cv) return;
  const ctx = cv.getContext("2d"), tfBox = document.getElementById("bub-tf"), srcBox = document.getElementById("bub-src");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const TF = { cg: [["h1", "1h"], ["d1", "24h"], ["w1", "7d"]], dex: [["h1", "1h"], ["w1", "6h"], ["d1", "24h"]] };
  let src = "cg", tf = "d1", data = {}, B = [], W = 0, H = 280, dpr = 1, raf = 0, last = 0, hover = null, drag = null, visible = false, T0 = performance.now();
  const stars = Array.from({ length: 70 }, () => ({ x: Math.random(), y: Math.random(), r: Math.random() * 1.2 + .2, p: Math.random() * 6.28 }));
  function sprite(r, up, k, trend, sym, chg) {
    const s = Math.ceil(r * 2 + 8), c = document.createElement("canvas"); c.width = c.height = s * dpr;
    const x = c.getContext("2d"); x.scale(dpr, dpr); const m = s / 2, col = up ? "34,211,155" : "255,92,122", a = Math.min(1, .45 + k);
    const g = x.createRadialGradient(m - r * .3, m - r * .35, r * .05, m, m, r);
    g.addColorStop(0, `rgba(${col},${.08 * a})`); g.addColorStop(.72, `rgba(${col},${.20 * a})`); g.addColorStop(1, `rgba(${col},${.9 * a})`);
    x.beginPath(); x.arc(m, m, r, 0, 6.2832); x.fillStyle = g; x.fill();
    x.lineWidth = trend ? 2.2 : 1.2; x.strokeStyle = trend ? "rgba(255,214,102,.95)" : `rgba(${col},.95)`; x.stroke();
    x.beginPath(); x.ellipse(m - r * .35, m - r * .45, r * .3, r * .14, -.6, 0, 6.2832); x.fillStyle = "rgba(255,255,255,.18)"; x.fill();
    if (r > 11) { x.fillStyle = "#fff"; x.textAlign = "center"; x.textBaseline = "middle";
      x.font = `700 ${Math.min(17, r * .42)}px Inter,system-ui,sans-serif`; x.fillText(sym, m, m - (r > 18 ? r * .17 : 0));
      if (r > 18) { x.font = `600 ${Math.min(12, r * .3)}px ui-monospace,monospace`; x.fillStyle = "rgba(255,255,255,.9)"; x.fillText(`${up ? "+" : ""}${(chg || 0).toFixed(1)}%`, m, m + r * .33); } }
    return c;
  }
  function size() { dpr = Math.min(2, devicePixelRatio || 1); W = cv.clientWidth; H = W < 500 ? 300 : 320; cv.width = W * dpr; cv.height = H * dpr; cv.style.height = H + "px"; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); build(); }
  function build() {
    const items = (data[src] || {}).items || []; if (!items.length || !W) return;
    const v = items.map(i => Math.abs(i[tf] ?? 0)), mx = Math.max(1, ...v);
    const raw = v.map(x => .35 + Math.sqrt(x / mx)), sum = raw.reduce((a, r) => a + r * r, 0), k = Math.sqrt(W * H * .38 / (Math.PI * sum));
    const old = Object.fromEntries(B.map(b => [b.id, b]));
    B = items.map((it, n) => { const o = old[it.id], r = Math.max(9, Math.min(H * .22, raw[n] * k)), c = it[tf] ?? 0;
      return { id: it.id, s: it.s, c, r, m: r * r, img: sprite(r, c >= 0, Math.abs(c) / 12, it.t, it.s, c), a: Math.random() * 6.28,
        x: o ? o.x : r + Math.random() * (W - 2 * r), y: o ? o.y : r + Math.random() * (H - 2 * r), vx: o ? o.vx : (Math.random() - .5) * 60, vy: o ? o.vy : (Math.random() - .5) * 60 }; });
  }
  function step(dt) {
    for (const b of B) {
      if (b === drag) continue;
      b.a += (Math.random() - .5) * 2.4 * dt;               // wander heading: continuous organic drift
      b.vx += Math.cos(b.a) * 34 * dt; b.vy += Math.sin(b.a) * 34 * dt;
      const sp = Math.hypot(b.vx, b.vy), cap = 90;
      if (sp > cap) { b.vx *= .96; b.vy *= .96; } else if (sp < 14) { b.vx += Math.cos(b.a) * 20 * dt; b.vy += Math.sin(b.a) * 20 * dt; }
      b.x += b.vx * dt; b.y += b.vy * dt;
      if (b.x < b.r) { b.x = b.r; b.vx = Math.abs(b.vx) * .9; b.a = 0; } else if (b.x > W - b.r) { b.x = W - b.r; b.vx = -Math.abs(b.vx) * .9; b.a = Math.PI; }
      if (b.y < b.r) { b.y = b.r; b.vy = Math.abs(b.vy) * .9; b.a = Math.PI / 2; } else if (b.y > H - b.r) { b.y = H - b.r; b.vy = -Math.abs(b.vy) * .9; b.a = -Math.PI / 2; }
    }
    for (let i = 0; i < B.length; i++) { const a = B[i];
      for (let j = i + 1; j < B.length; j++) { const b = B[j], dx = b.x - a.x, dy = b.y - a.y, rr = a.r + b.r;
        if (dx > rr || dx < -rr || dy > rr || dy < -rr) continue;
        const d2 = dx * dx + dy * dy; if (d2 >= rr * rr) continue;
        const d = Math.sqrt(d2) || .01, ux = dx / d, uy = dy / d, o = rr - d;
        const ia = a === drag ? 0 : 1 / a.m, ib = b === drag ? 0 : 1 / b.m, it = ia + ib || 1;
        a.x -= ux * o * ia / it; a.y -= uy * o * ia / it; b.x += ux * o * ib / it; b.y += uy * o * ib / it;
        const rv = (b.vx - a.vx) * ux + (b.vy - a.vy) * uy;
        if (rv < 0) { const j2 = -1.8 * rv / it; a.vx -= ux * j2 * ia; a.vy -= uy * j2 * ia; b.vx += ux * j2 * ib; b.vy += uy * j2 * ib; } } }
  }
  function bg(t) {
    const k = (t - T0) / 1000;
    const g1x = W * (.3 + .2 * Math.sin(k * .21)), g1y = H * (.4 + .25 * Math.cos(k * .17)), g2x = W * (.72 + .18 * Math.cos(k * .13)), g2y = H * (.6 + .25 * Math.sin(k * .19));
    let g = ctx.createRadialGradient(g1x, g1y, 0, g1x, g1y, W * .45); g.addColorStop(0, "rgba(122,92,255,.16)"); g.addColorStop(1, "rgba(122,92,255,0)"); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    g = ctx.createRadialGradient(g2x, g2y, 0, g2x, g2y, W * .4); g.addColorStop(0, "rgba(34,211,230,.11)"); g.addColorStop(1, "rgba(34,211,230,0)"); ctx.fillStyle = g; ctx.fillRect(0, 0, W, H);
    ctx.strokeStyle = "rgba(122,162,255,.07)"; ctx.lineWidth = 1; const off = (k * 8) % 32; ctx.beginPath();
    for (let x = -off; x < W; x += 32) { ctx.moveTo(x + .5, 0); ctx.lineTo(x + .5, H); }
    for (let y = -off; y < H; y += 32) { ctx.moveTo(0, y + .5); ctx.lineTo(W, y + .5); } ctx.stroke();
    ctx.fillStyle = "#cfe3ff"; for (const s of stars) { ctx.globalAlpha = .25 + .35 * Math.sin(k * 1.3 + s.p) ** 2; ctx.fillRect(s.x * W, s.y * H, s.r, s.r); } ctx.globalAlpha = 1;
  }
  function draw(t) {
    ctx.clearRect(0, 0, W, H); bg(t);
    for (const b of B) { if (b === hover || b === drag) continue; const s = b.img.width / dpr; ctx.drawImage(b.img, b.x - s / 2, b.y - s / 2, s, s); }
    const h = drag || hover; if (h) { const s = h.img.width / dpr * 1.1; ctx.save(); ctx.shadowColor = h.c >= 0 ? "rgba(34,211,155,.9)" : "rgba(255,92,122,.9)"; ctx.shadowBlur = 24; ctx.drawImage(h.img, h.x - s / 2, h.y - s / 2, s, s); ctx.restore(); }
  }
  function loop(t) { const dt = Math.min(.033, (t - (last || t)) / 1000) || .016; last = t; step(dt); draw(t); raf = visible && !document.hidden ? requestAnimationFrame(loop) : 0; }
  function start() { if (reduce) { for (let i = 0; i < 120; i++) step(.016); draw(performance.now()); return; } if (!raf) { last = 0; raf = requestAnimationFrame(loop); } }
  async function load(s) { try { const d = await (await fetch("/api/bubbles" + (s === "dex" ? "?src=dex" : ""))).json(); if (d.items && d.items.length) { data[s] = { ...d, at: Date.now() }; if (s === src) { B = []; size(); start(); } } } catch {} }
  const pt = e => { const r = cv.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
  const at = p => { for (let i = B.length - 1; i >= 0; i--) { const b = B[i]; if ((b.x - p.x) ** 2 + (b.y - p.y) ** 2 < b.r * b.r) return b; } return null; };
  let down = null, hist = [];
  cv.addEventListener("pointerdown", e => { const p = pt(e), b = at(p); down = { p, t: performance.now(), b }; if (b) { drag = b; b.ox = p.x - b.x; b.oy = p.y - b.y; hist = [{ ...p, t: performance.now() }]; cv.setPointerCapture(e.pointerId); e.preventDefault(); } });
  cv.addEventListener("pointermove", e => { const p = pt(e);
    if (drag) { drag.x = Math.max(drag.r, Math.min(W - drag.r, p.x - drag.ox)); drag.y = Math.max(drag.r, Math.min(H - drag.r, p.y - drag.oy)); hist.push({ ...p, t: performance.now() }); if (hist.length > 6) hist.shift(); }
    else { hover = at(p); cv.style.cursor = hover ? "grab" : "default"; } });
  cv.addEventListener("pointerup", e => { const p = pt(e);
    if (drag) { const h0 = hist[0], h1 = hist[hist.length - 1], dt = Math.max(16, h1.t - h0.t) / 1000;
      drag.vx = Math.max(-900, Math.min(900, (h1.x - h0.x) / dt)); drag.vy = Math.max(-900, Math.min(900, (h1.y - h0.y) / dt)); }
    if (down && down.b && Math.hypot(p.x - down.p.x, p.y - down.p.y) < 5 && performance.now() - down.t < 350) {
      const id = down.b.id; window.open(/^https:\/\//.test(id) ? id : `https://www.coingecko.com/en/coins/${encodeURIComponent(id)}`, "_blank", "noopener"); }
    drag = null; down = null; start(); });
  cv.addEventListener("pointerleave", () => { if (!drag) hover = null; });
  function tfButtons() { tfBox.innerHTML = TF[src].map(([k, l]) => `<button data-tf="${k}" class="${k === tf ? "on" : ""}">${l}</button>`).join(""); }
  tfBox.addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; tf = b.dataset.tf; tfButtons(); build(); start(); });
  srcBox && srcBox.addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; src = b.dataset.src; srcBox.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); tf = "d1"; tfButtons(); B = [];
    if (data[src] && Date.now() - data[src].at < 3e5) { build(); start(); } else load(src); });
  new ResizeObserver(() => { if (cv.clientWidth && Math.abs(cv.clientWidth - W) > 1) size(); }).observe(cv);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && visible) start(); });
  new IntersectionObserver(es => { visible = es[0].isIntersecting; if (visible) { if (!data[src] || Date.now() - data[src].at > 6e5) load(src); else start(); } }).observe(cv);
  tfButtons();
  window.__bub = () => B.map(b => ({ s: b.s, x: Math.round(b.x), y: Math.round(b.y), r: Math.round(b.r) }));   // QA hook
})();
