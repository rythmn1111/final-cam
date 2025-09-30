<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Pi BnW Cam</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:ital,wght@0,200;0,400;0,700;1,200;1,400;1,700&display=swap" rel="stylesheet">
  <style>
    :root { --gap: 12px; color-scheme: light; }
    body{font-family:'Atkinson Hyperlegible',system-ui,Segoe UI,Arial,sans-serif;font-weight:200;max-width:980px;margin:24px auto;padding:0 16px;background:#ECE7E1;color:#333}
    h1{margin:0 0 12px;color:#eb5d40}
    .logo{height:120px;width:auto;display:block;margin:0 auto 40px}
    .toolbar{display:flex;flex-wrap:wrap;align-items:center;justify-content:center;gap:20px;margin:40px 0 40px 0}
    button{padding:16px 24px;border:0;border-radius:0;font-weight:200;font-family:'Atkinson Hyperlegible',system-ui,Segoe UI,Arial,sans-serif;cursor:pointer;font-size:16px;display:flex;align-items:center;gap:8px}
    .icon{width:16px;height:16px;display:inline-block}
    .camera-icon{background:linear-gradient(45deg, transparent 30%, currentColor 30%, currentColor 70%, transparent 70%), linear-gradient(-45deg, transparent 30%, currentColor 30%, currentColor 70%, transparent 70%);border-radius:2px}
    #captureBtn{background:#eb5d40;color:#fff}
    #status{margin:4px 0 16px;opacity:.8;min-height:1.2em;color:#eb5d40}
    .hero{display:grid;grid-template-columns:1fr;gap:16px;align-items:start}
    .hero img{width:100%;height:auto;border-radius:12px;display:block}
    .muted{opacity:.7;font-size:14px;color:#eb5d40}
    .section{margin-top:24px}
    .section h2{margin:0 0 8px;color:#eb5d40}
    .grid{display:grid;grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));gap: var(--gap);}
    .card{position:relative;overflow:visible;border-radius:12px;box-shadow:0 1px 4px rgba(235,93,64,.2);background:#ECE7E1;border:2px solid #eb5d40;transition:transform .15s ease, box-shadow .15s ease;display:flex;flex-direction:column}
    .card:hover{transform:translateY(-2px); box-shadow:0 6px 18px rgba(235,93,64,.3)}
    .thumb{width:100%;height:160px;object-fit:cover;display:block;filter:grayscale(100%) contrast(110%);border-radius:8px 8px 0 0}
    .meta{background:#eb5d40;color:#fff;font-size:12px;padding:8px;border-radius:0 0 8px 8px;margin-top:auto}
    @media (max-width:720px){ .hero{grid-template-columns:1fr} }
  </style>
</head>
<body>
  <img src="/logo.png" alt="EvenAfter Cam" class="logo">

  <div class="toolbar">
    <button id="captureBtn"><span class="icon camera-icon"></span>Capture BnW</button>
    <span id="status" class="muted"></span>
  </div>

  <div class="hero">
    <div>
      <img id="preview" alt="Last capture will appear here" />
      <div class="muted">Latest image (auto-updates)</div>
    </div>
  </div>

  <div class="section">
    <h2>Local captures <span class="muted" id="countLocal"></span></h2>
    <div id="gridLocal" class="grid"></div>
  </div>

  <script>
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
  </script>
</body>
</html>
