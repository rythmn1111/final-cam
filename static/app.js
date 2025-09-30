const btn = document.getElementById("captureBtn");
const statusEl = document.getElementById("status");
const img = document.getElementById("preview");
const gridLocal = document.getElementById("gridLocal");
const countLocal = document.getElementById("countLocal");

async function capture() {
  btn.disabled = true;
  btn.textContent = "Capturing…";
  statusEl.textContent = "Taking picture and converting…";
  try {
    const r = await fetch("/capture", { method: "POST" });
    const data = await r.json();
    if (!data.ok) throw new Error(data.error || "Unknown error");
    img.src = data.url;
    statusEl.textContent = "Done.";
    await refreshGallery();
  } catch (e) {
    console.error(e);
    statusEl.textContent = e.message || "Failed to capture. Check server logs.";
  } finally {
    btn.disabled = false;
    btn.textContent = "Capture BnW";
  }
}
btn.addEventListener("click", capture);

// SSE: captured -> refresh
try {
  const es = new EventSource("/events");
  es.onmessage = async (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "captured") {
        img.src = "/latest.webp?ts=" + msg.ts;
        await refreshGallery();
      }
    } catch {}
  };
} catch {}

function fmtBytes(n){
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024*1024) return (n/1024).toFixed(1) + " KB";
  return (n/1024/1024).toFixed(2) + " MB";
}

function renderLocal(items){
  gridLocal.innerHTML = "";
  countLocal.textContent = `(${items.length})`;
  for (const it of items){
    const dt = new Date(it.mtimeMs);
    const card = document.createElement("div");
    card.className = "card";
    card.innerHTML = `
      <a href="${it.url}" target="_blank" rel="noopener">
        <img class="thumb" loading="lazy" src="${it.url}" alt="${it.name}" />
      </a>
      <div class="meta">${dt.toLocaleString()} · ${fmtBytes(it.size)}</div>
    `;
    gridLocal.appendChild(card);
  }
}

async function refreshGallery(){
  try{
    const r = await fetch("/gallery.json");
    const data = await r.json();
    if (!data.ok) throw new Error("Gallery failed");
    renderLocal(data.local || []);
  }catch(e){
    console.error(e);
    statusEl.textContent = "Failed to load gallery.";
  }
}

(async function init(){
  img.src = "/latest.webp?ts=" + Date.now();
  await refreshGallery();
})();
