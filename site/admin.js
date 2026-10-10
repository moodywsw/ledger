const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
let T = (() => { try { return localStorage.getItem("ledgerAdminToken") || ""; } catch { return ""; } })();
async function api(body) {
  const r = await fetch("/api/owner/ask_access", { method: body ? "POST" : "GET", headers: { "Content-Type": "application/json", Authorization: `Bearer ${T}` }, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) throw new Error("bad token"); return r.json();
}
async function load() {
  try {
    const d = await api(); $("adm-tok").classList.add("hidden"); $("adm-body").classList.remove("hidden"); $("adm-st").textContent = "unlocked";
    try { localStorage.setItem("ledgerAdminToken", T); } catch {}
    $("adm-feed").checked = !!d.public_feed;
    $("adm-list").innerHTML = d.codes.slice().reverse().map(c => `<tr><td>${esc(c.label)}</td><td>${c.daily}</td><td>${c.uses}</td><td>${c.last ? new Date(c.last * 1000).toLocaleString() : "—"}</td><td>${c.revoked ? "revoked" : "active"}</td><td>${c.revoked ? "" : `<button class="btn ghost" data-rv="${esc(c.id)}">Revoke</button>`}</td></tr>`).join("") || `<tr><td colspan="6" class="muted">No codes yet.</td></tr>`;
  } catch { $("adm-st").textContent = T ? "wrong token" : "locked"; }
}
$("adm-tok").addEventListener("submit", e => { e.preventDefault(); T = $("adm-t").value.trim(); load(); });
$("adm-new").addEventListener("submit", async e => { e.preventDefault();
  const r = await api({ action: "create", label: $("adm-label").value, daily: +$("adm-daily").value });
  $("adm-code").classList.remove("hidden"); $("adm-code").innerHTML = `Code for <b>${esc(r.label)}</b> (shown once, copy it now): <code class="mono">${esc(r.code)}</code>`; $("adm-label").value = ""; load(); });
$("adm-list").addEventListener("click", async e => { const b = e.target.closest("[data-rv]"); if (b && confirm("Revoke this code?")) { await api({ action: "revoke", id: b.dataset.rv }); load(); } });
$("adm-feed").addEventListener("change", async e => { await api({ action: "feed", on: e.target.checked }); });
if (T) load();
