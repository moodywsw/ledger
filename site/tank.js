// Mirko's healing tank: bubbles rising forever inside the glass (canvas, clipped), light CPU.
(() => {
  const cv = document.getElementById("bubbles"); if (!cv) return;
  const ctx = cv.getContext("2d"), reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
  const DPR = Math.min(devicePixelRatio || 1, 2);
  let W = 0, H = 0, bs = [], run = true, last = 0;
  const spawn = (y) => ({ x: Math.random() * W, y: y ?? H + Math.random() * 20, r: .8 + Math.random() ** 2 * 4.2,
    v: 8 + Math.random() * 22, wob: Math.random() * 6.28, amp: .3 + Math.random() * 1.2 });
  function size() { const r = cv.getBoundingClientRect(); W = r.width; H = r.height; cv.width = W * DPR; cv.height = H * DPR; ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    bs = Array.from({ length: Math.max(W > 10 ? 14 : 0, Math.round(Math.min(46, W * H / 900))) }, () => spawn(Math.random() * H)); }
  function draw(dt) {
    ctx.clearRect(0, 0, W, H);
    const dream = parseFloat(getComputedStyle(cv.parentElement).getPropertyValue("--dream")) || .35;
    for (const b of bs) {
      b.y -= b.v * dt * (0.8 + dream * 0.6); b.wob += dt * 2; const x = b.x + Math.sin(b.wob) * b.amp * 3;
      if (b.y < -6) Object.assign(b, spawn());
      ctx.beginPath(); ctx.arc(x, b.y, b.r, 0, 6.283);
      ctx.strokeStyle = `rgba(200,255,250,${.35 + b.r / 12})`; ctx.lineWidth = .8; ctx.stroke();
      ctx.fillStyle = "rgba(160,255,240,.08)"; ctx.fill();
      ctx.fillStyle = "rgba(255,255,255,.7)"; ctx.fillRect(x - b.r * .35, b.y - b.r * .45, Math.max(.6, b.r * .3), Math.max(.6, b.r * .3));
    }
  }
  function loop(t) { if (!run) return; const dt = Math.min(.1, (t - (last || t)) / 1000); if (t - last > 33) { draw(dt || .033); last = t; } requestAnimationFrame(loop); }
  const vis = () => { const v = !document.hidden && cv.offsetParent !== null; if (v && !run) { run = true; last = 0; requestAnimationFrame(loop); } else run = v; };
  document.addEventListener("visibilitychange", vis); window.addEventListener("ledger:view", vis);
  // size from the real box (it may be 0 at load while fonts/images/grid settle) — ResizeObserver fixes "frozen bubbles"
  const ro = new ResizeObserver(() => { const r = cv.getBoundingClientRect(); if (Math.abs(r.width - W) > 1 || Math.abs(r.height - H) > 1) { size(); if (reduce) draw(0); } });
  ro.observe(cv);
  size(); if (reduce) draw(0); else requestAnimationFrame(loop);
})();
