// Minimal canvas annotation overlay (P2-06, DD §7.2/C21): load an existing
// substep photo, draw red arrows/circles over it, save the composited PNG
// as a NEW media entry appended to the substep's list. The source image is
// never modified or removed -- DD: never destroy source. No external libs;
// reuses uploadMedia()/setSubstepMedia() from library.js.

function openAnnotator(substepId, imageUrl, existingMedia) {
  const existing = typeof existingMedia === "string" ? JSON.parse(existingMedia) : existingMedia;

  const overlay = document.createElement("div");
  overlay.style.cssText =
    "position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:1000;" +
    "display:flex;align-items:center;justify-content:center;";
  overlay.innerHTML = `
    <div style="background:#fff;padding:1rem;border-radius:8px;max-width:95vw;max-height:95vh;overflow:auto;">
      <div style="margin-bottom:.5rem;">
        <button type="button" data-tool="arrow" class="btn btn-sm">Arrow</button>
        <button type="button" data-tool="circle" class="btn btn-sm">Circle</button>
        <button type="button" data-action="save" class="btn btn-sm">Save annotation</button>
        <button type="button" data-action="close" class="btn btn-sm">Close</button>
      </div>
      <canvas style="max-width:100%;border:1px solid #ccc;"></canvas>
    </div>`;
  document.body.appendChild(overlay);

  const canvas = overlay.querySelector("canvas");
  const ctx = canvas.getContext("2d");
  const img = new Image();
  img.onload = () => {
    canvas.width = img.width;
    canvas.height = img.height;
    ctx.drawImage(img, 0, 0);
  };
  img.src = imageUrl;

  let tool = "arrow";
  let drawing = false;
  let startX = 0;
  let startY = 0;
  let snapshot = null;

  overlay.querySelectorAll("[data-tool]").forEach((btn) => {
    btn.addEventListener("click", () => {
      tool = btn.dataset.tool;
    });
  });

  function canvasPoint(e) {
    const rect = canvas.getBoundingClientRect();
    return {
      x: (e.clientX - rect.left) * (canvas.width / rect.width),
      y: (e.clientY - rect.top) * (canvas.height / rect.height),
    };
  }

  canvas.addEventListener("mousedown", (e) => {
    const p = canvasPoint(e);
    startX = p.x;
    startY = p.y;
    drawing = true;
    snapshot = ctx.getImageData(0, 0, canvas.width, canvas.height);
  });

  canvas.addEventListener("mousemove", (e) => {
    if (!drawing) return;
    const { x, y } = canvasPoint(e);
    ctx.putImageData(snapshot, 0, 0);
    ctx.strokeStyle = "red";
    ctx.fillStyle = "red";
    ctx.lineWidth = 3;
    if (tool === "circle") {
      const r = Math.hypot(x - startX, y - startY);
      ctx.beginPath();
      ctx.arc(startX, startY, r, 0, Math.PI * 2);
      ctx.stroke();
    } else {
      drawArrow(ctx, startX, startY, x, y);
    }
  });

  canvas.addEventListener("mouseup", () => {
    drawing = false;
    snapshot = null;
  });

  overlay.querySelector('[data-action="close"]').addEventListener("click", () => overlay.remove());
  overlay.querySelector('[data-action="save"]').addEventListener("click", () => {
    canvas.toBlob(async (blob) => {
      try {
        const { url } = await uploadMedia(blob, "annotated.png");
        const next = existing.concat([{ kind: "annotated", url, caption: "Annotated" }]);
        await setSubstepMedia(substepId, next);
      } catch (err) {
        alert(err.message);
      }
    }, "image/png");
  });
}

function drawArrow(ctx, x1, y1, x2, y2) {
  const headlen = 12;
  const angle = Math.atan2(y2 - y1, x2 - x1);
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
  ctx.beginPath();
  ctx.moveTo(x2, y2);
  ctx.lineTo(x2 - headlen * Math.cos(angle - Math.PI / 6), y2 - headlen * Math.sin(angle - Math.PI / 6));
  ctx.lineTo(x2 - headlen * Math.cos(angle + Math.PI / 6), y2 - headlen * Math.sin(angle + Math.PI / 6));
  ctx.closePath();
  ctx.fill();
}
