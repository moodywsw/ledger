// Market bubbles: canvas physics at display refresh rate. Bubbles are pre-rendered sprites (no per-frame gradients).
(() => {
  const cv = document.getElementById("bub"); if (!cv) return;
  const ctx = cv.getContext("2d", { alpha: true }), tfBox = document.getElementById("bub-tf");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let items = [], B = [], tf = "d1", W = 0, H = 260, dpr = 1, raf = 0, loaded = 0, last = 0, hover = null, bg = null, t0 = performance.now();
  function sprite(r, up, strength, trend, sym, chg) {
    const s = Math.ceil(r * 2 + 6), c = document.createElement("canvas"); c.width = s * dpr; c.height = s * dpr;
    const x = c.getContext("2d"); x.scale(dpr, dpr); const m = s / 2, col = up ? "34,211,155" : "255,92,122", a = Math.min(1, .4 + strength);
    const g = x.createRadialGradient(m - r * .3, m - r * .35, r * .1, m, m, r);
    g.addColorStop(0, `rgba(${col},${.10 * a})`); g.addColorStop(.7, `rgba(${col},${.22 * a})`); g.addColorStop(1, `rgba(${col},${.85 * a})`);
    x.beginPath(); x.arc(m, m, r, 0, 6.2832); x.fillStyle = g; x.fill();
    x.lineWidth = trend ? 2 : 1.2; x.strokeStyle = trend ? "rgba(255,214,102,.95)" : `rgba(${col},${.9 * a})`; x.stroke();
    x.beginPath(); x.ellipse(m - r * .35, m - r * .45, r * .28, r * .14, -.6, 0, 6.2832); x.fillStyle = "rgba(255,255,255,.16)"; x.fill();
    if (r > 12) { x.fillStyle = "#fff"; x.textAlign = "center"; x.textBaseline = "middle";
      x.font = `700 ${Math.min(16, r * .44)}px Inter,system-ui,sans-serif`; x.fillText(sym, m, m - (r > 19 ? r * .16 : 0));
      if (r > 19) { x.font = `600 ${Math.min(12, r * .3)}px ui-monospace,monospace`; x.fillStyle = "rgba(255,255,255,.88)"; x.fillText(`${up ? "+" : ""}${chg.toFixed(1)}%`, m, m + r * .32); } }
    return c;
  }
  function background() {
    const c = document.createElement("canvas"); c.width = W * dpr; c.height = H * dpr; const x = c.getContext("2d"); x.scale(dpr, dpr);
    x.strokeStyle = "rgba(122,162,255,.06)"; x.lineWidth = 1;
    for (let i = 0; i < W; i += 28) { x.beginPath(); x.moveTo(i + .5, 0); x.lineTo(i + .5, H); x.stroke(); }
    for (let j = 0; j < H; j += 28) { x.beginPath(); x.moveTo(0, j + .5); x.lineTo(W, j + .5); x.stroke(); }
    return c;
  }
  function size() { dpr = Math.min(2, devicePixelRatio || 1); W = cv.clientWidth; H = W < 500 ? 250 : 280; cv.width = W * dpr; cv.height = H * dpr; cv.style.height = H + "px"; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); bg = background(); build(); }
  function build() {
    if (!items.length || !W) return;
    const v = items.map(i => Math.abs(i[tf] ?? 0)), mx = Math.max(1, ...v);
    const raw = v.map(x => .28 + Math.sqrt(x / mx)), sum = raw.reduce((a, r) => a + r * r, 0), k = Math.sqrt(W * H * .5 / (Math.PI * sum));
    const old = Object.fromEntries(B.map(b => [b.id, b]));
    B = items.map((it, n) => { const o = old[it.id], r = Math.max(9, Math.min(H * .28, raw[n] * k)), c = it[tf] ?? 0;
      return { id: it.id, s: it.s, c, r, img: sprite(r, c >= 0, Math.abs(c) / 10, it.t, it.s, c),
        x: o ? Math.min(W - r, Math.max(r, o.x)) : r + Math.random() * (W - 2 * r), y: o ? Math.min(H - r, Math.max(r, o.y)) : r + Math.random() * (H - 2 * r),
        vx: o ? o.vx : (Math.random() - .5) * 40, vy: o ? o.vy : (Math.random() - .5) * 40 }; });
  }
  function step(dt) {
    const cx = W / 2, cy = H / 2;
    for (const b of B) {
      b.vx += ((cx - b.x) * .15 + (Math.random() - .5) * 60) * dt; b.vy += ((cy - b.y) * .15 + (Math.random() - .5) * 60) * dt;
      const sp = Math.hypot(b.vx, b.vy), max = 70; if (sp > max) { b.vx *= max / sp; b.vy *= max / sp; }
      b.x += b.vx * dt; b.y += b.vy * dt;
      if (b.x < b.r) { b.x = b.r; b.vx = Math.abs(b.vx) * .8; } else if (b.x > W - b.r) { b.x = W - b.r; b.vx = -Math.abs(b.vx) * .8; }
      if (b.y < b.r) { b.y = b.r; b.vy = Math.abs(b.vy) * .8; } else if (b.y > H - b.r) { b.y = H - b.r; b.vy = -Math.abs(b.vy) * .8; }
    }
    for (let it = 0; it < 2; it++) for (let i = 0; i < B.length; i++) { const a = B[i];
      for (let j = i + 1; j < B.length; j++) { const b = B[j], dx = b.x - a.x, dy = b.y - a.y, rr = a.r + b.r; if (Math.abs(dx) > rr || Math.abs(dy) > rr) continue;
        const d = Math.hypot(dx, dy) || .01, o = rr - d; if (o <= 0) continue;
        const ux = dx / d, uy = dy / d, ma = b.r * b.r / (a.r * a.r + b.r * b.r), f = o * .5;
        a.x -= ux * f * ma * 2; a.y -= uy * f * ma * 2; b.x += ux * f * (1 - ma) * 2; b.y += uy * f * (1 - ma) * 2;
        const rv = (b.vx - a.vx) * ux + (b.vy - a.vy) * uy; if (rv < 0) { const imp = rv * .9; a.vx += ux * imp * ma; a.vy += uy * imp * ma; b.vx -= ux * imp * (1 - ma); b.vy -= uy * imp * (1 - ma); } } }
  }
  function draw(t) {
    ctx.clearRect(0, 0, W, H);
    const k = (t - t0) / 1000, gx = W * (.5 + .35 * Math.sin(k * .25)), gy = H * (.5 + .3 * Math.cos(k * .31));
    const g = ctx.createRadialGradient(gx, gy, 0, gx, gy, Math.max(W, H) * .7); g.addColorStop(0, "rgba(122,92,255,.10)"); g.addColorStop(.5, "rgba(34,211,230,.04)"); g.addColorStop(1, "rgba(0,0,0,0)");
    ctx.fillStyle = g; ctx.fillRect(0, 0, W, H); if (bg) ctx.drawImage(bg, 0, 0, W, H);
    for (const b of B) { const s = b.img.width / dpr, z = hover === b ? 1.08 : 1; ctx.drawImage(b.img, b.x - s * z / 2, b.y - s * z / 2, s * z, s * z); }
  }
  function loop(t) { const dt = Math.min(.05, (t - (last || t)) / 1000) || .016; last = t; step(dt); draw(t); raf = document.hidden ? 0 : requestAnimationFrame(loop); }
  function start() { if (reduce) { for (let i = 0; i < 200; i++) step(.016); draw(performance.now()); return; } if (!raf) { last = 0; raf = requestAnimationFrame(loop); } }
  async function load() { try { const d = await (await fetch("/api/bubbles")).json(); if (d.items && d.items.length) { items = d.items; loaded = Date.now(); size(); start(); } } catch {} }
  const at = e => { const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top; return B.find(b => (b.x - x) ** 2 + (b.y - y) ** 2 < b.r * b.r); };
  tfBox.addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; tf = b.dataset.tf; tfBox.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); build(); start(); });
  cv.addEventListener("click", e => { const b = at(e); if (b) window.open(`https://www.coingecko.com/en/coins/${encodeURIComponent(b.id)}`, "_blank", "noopener"); });
  cv.addEventListener("mousemove", e => { hover = at(e) || null; cv.style.cursor = hover ? "pointer" : "default"; });
  cv.addEventListener("mouseleave", () => { hover = null; });
  new ResizeObserver(() => { if (cv.clientWidth && Math.abs(cv.clientWidth - W) > 1) size(); }).observe(cv);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) start(); });
  new IntersectionObserver(es => { if (es[0].isIntersecting) { if (!loaded || Date.now() - loaded > 6e5) load(); else start(); } else if (raf) { cancelAnimationFrame(raf); raf = 0; } }).observe(cv);
})();
