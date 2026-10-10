(() => {
  const cv = document.getElementById("bub"); if (!cv) return;
  const ctx = cv.getContext("2d"), tfBox = document.getElementById("bub-tf");
  const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let items = [], B = [], tf = "d1", W = 0, H = 260, dpr = 1, raf = 0, loaded = 0;
  function size() { dpr = Math.min(2, devicePixelRatio || 1); W = cv.clientWidth; H = W < 500 ? 240 : 260; cv.width = W * dpr; cv.height = H * dpr; cv.style.height = H + "px"; ctx.setTransform(dpr, 0, 0, dpr, 0, 0); build(); }
  function build() {
    if (!items.length || !W) return;
    const v = items.map(i => Math.abs(i[tf] ?? 0)), mx = Math.max(1, ...v);
    const area = W * H * 0.55, raw = v.map(x => 0.25 + Math.sqrt(x / mx)), sum = raw.reduce((a, r) => a + r * r, 0), k = Math.sqrt(area / (Math.PI * sum));
    const old = Object.fromEntries(B.map(b => [b.id, b]));
    B = items.map((it, n) => { const o = old[it.id], r = Math.max(9, raw[n] * k);
      return { id: it.id, s: it.s, c: it[tf] ?? 0, t: it.t, r, x: o ? o.x : r + Math.random() * (W - 2 * r), y: o ? o.y : r + Math.random() * (H - 2 * r), vx: o ? o.vx : (Math.random() - .5) * .3, vy: o ? o.vy : (Math.random() - .5) * .3 }; });
  }
  function step() {
    for (const b of B) { b.x += b.vx; b.y += b.vy; b.vx *= .995; b.vy *= .995; b.vx += (Math.random() - .5) * .02; b.vy += (Math.random() - .5) * .02;
      if (b.x < b.r) { b.x = b.r; b.vx = Math.abs(b.vx); } if (b.x > W - b.r) { b.x = W - b.r; b.vx = -Math.abs(b.vx); }
      if (b.y < b.r) { b.y = b.r; b.vy = Math.abs(b.vy); } if (b.y > H - b.r) { b.y = H - b.r; b.vy = -Math.abs(b.vy); } }
    for (let i = 0; i < B.length; i++) for (let j = i + 1; j < B.length; j++) { const a = B[i], b = B[j], dx = b.x - a.x, dy = b.y - a.y, d = Math.hypot(dx, dy) || 1, o = a.r + b.r - d;
      if (o > 0) { const ux = dx / d, uy = dy / d, f = o * .5; a.x -= ux * f; a.y -= uy * f; b.x += ux * f; b.y += uy * f; a.vx -= ux * .02; a.vy -= uy * .02; b.vx += ux * .02; b.vy += uy * .02; } }
  }
  function draw() {
    ctx.clearRect(0, 0, W, H);
    for (const b of B) {
      const up = b.c >= 0, a = Math.min(1, .35 + Math.abs(b.c) / 12), col = up ? `34,211,155` : `255,92,122`;
      const g = ctx.createRadialGradient(b.x, b.y, b.r * .2, b.x, b.y, b.r);
      g.addColorStop(0, `rgba(${col},.04)`); g.addColorStop(.75, `rgba(${col},${.18 * a})`); g.addColorStop(1, `rgba(${col},${.75 * a})`);
      ctx.beginPath(); ctx.arc(b.x, b.y, b.r, 0, 6.2832); ctx.fillStyle = g; ctx.fill();
      ctx.lineWidth = b.t ? 2 : 1; ctx.strokeStyle = b.t ? "rgba(255,214,102,.9)" : `rgba(${col},${a})`; ctx.stroke();
      if (b.r > 13) { ctx.fillStyle = "#fff"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.font = `600 ${Math.min(15, b.r * .42)}px Inter,system-ui,sans-serif`; ctx.fillText(b.s, b.x, b.y - (b.r > 20 ? b.r * .17 : 0));
        if (b.r > 20) { ctx.font = `${Math.min(12, b.r * .3)}px ui-monospace,monospace`; ctx.fillStyle = "rgba(255,255,255,.85)"; ctx.fillText(`${up ? "+" : ""}${b.c.toFixed(1)}%`, b.x, b.y + b.r * .3); } }
    }
  }
  function loop() { step(); draw(); raf = document.hidden || reduce ? 0 : requestAnimationFrame(loop); }
  function start() { if (!raf && !reduce) raf = requestAnimationFrame(loop); else if (reduce) { for (let i = 0; i < 120; i++) step(); draw(); } }
  async function load() { try { const r = await fetch("/api/bubbles"); const d = await r.json(); if (d.items && d.items.length) { items = d.items; loaded = Date.now(); size(); start(); } } catch {} }
  tfBox.addEventListener("click", e => { const b = e.target.closest("button"); if (!b) return; tf = b.dataset.tf; tfBox.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b)); build(); start(); });
  cv.addEventListener("click", e => { const r = cv.getBoundingClientRect(), x = e.clientX - r.left, y = e.clientY - r.top;
    const b = B.find(b => Math.hypot(b.x - x, b.y - y) < b.r); if (b) window.open(`https://www.coingecko.com/en/coins/${encodeURIComponent(b.id)}`, "_blank", "noopener"); });
  cv.addEventListener("mousemove", e => { const r = cv.getBoundingClientRect(); cv.style.cursor = B.some(b => Math.hypot(b.x - e.clientX + r.left, b.y - e.clientY + r.top) < b.r) ? "pointer" : "default"; });
  new ResizeObserver(() => { if (cv.clientWidth && cv.clientWidth !== W) size(); }).observe(cv);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) start(); });
  new IntersectionObserver(es => { if (es[0].isIntersecting && (!loaded || Date.now() - loaded > 6e5)) load(); }).observe(cv);
})();
