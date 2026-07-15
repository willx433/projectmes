// Rich-text toolbar for library step/substep body_html textareas (P2-05).
// Wraps the current selection in an allowlisted tag pair; the server
// sanitizes on save (app/domain/sanitize.py) so this is purely a typing
// convenience, never the source of truth for what's allowed.
function rtWrap(textareaId, open, close) {
  const ta = document.getElementById(textareaId);
  if (!ta) return;
  const start = ta.selectionStart;
  const end = ta.selectionEnd;
  const before = ta.value.slice(0, start);
  const selected = ta.value.slice(start, end);
  const after = ta.value.slice(end);
  ta.value = before + open + selected + close + after;
  ta.focus();
  ta.selectionStart = start + open.length;
  ta.selectionEnd = start + open.length + selected.length;
}

// Substep media (P2-06, DD §7.2/§12.3): client-side resize to <=2MP before
// upload (tablets shoot much bigger than that), then attach/remove entries
// by overwriting the substep's whole media list. Shared with annotate.js.
const MEDIA_MAX_PIXELS = 2_000_000;

function resizeImageFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => {
      const img = new Image();
      img.onerror = reject;
      img.onload = () => {
        let { width, height } = img;
        const pixels = width * height;
        if (pixels > MEDIA_MAX_PIXELS) {
          const scale = Math.sqrt(MEDIA_MAX_PIXELS / pixels);
          width = Math.round(width * scale);
          height = Math.round(height * scale);
        }
        const canvas = document.createElement("canvas");
        canvas.width = width;
        canvas.height = height;
        canvas.getContext("2d").drawImage(img, 0, 0, width, height);
        canvas.toBlob(
          (blob) => resolve(blob),
          file.type === "image/png" ? "image/png" : "image/jpeg",
          0.9
        );
      };
      img.src = reader.result;
    };
    reader.readAsDataURL(file);
  });
}

async function uploadMedia(blob, filename) {
  const fd = new FormData();
  fd.append("file", blob, filename);
  const resp = await fetch("/admin/media", { method: "POST", body: fd });
  if (!resp.ok) throw new Error(`upload failed (${resp.status})`);
  return resp.json();
}

async function setSubstepMedia(substepId, mediaList, who) {
  const body = new URLSearchParams({ media: JSON.stringify(mediaList), who: who || "" });
  const resp = await fetch(`/admin/library/substeps/${substepId}/media`, {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body,
  });
  if (!resp.ok) throw new Error(`save failed (${resp.status})`);
  window.location.reload();
}

async function libAddMedia(button) {
  const form = button.closest(".media-add-form");
  const substepId = form.dataset.substepId;
  const existing = JSON.parse(form.dataset.existing || "[]");
  const fileInput = form.querySelector(".media-file");
  const captionInput = form.querySelector(".media-caption");
  const file = fileInput.files[0];
  if (!file) return;
  button.disabled = true;
  try {
    const blob = await resizeImageFile(file);
    const { url } = await uploadMedia(blob, file.name || "photo.jpg");
    const next = existing.concat([{ kind: "photo", url, caption: captionInput.value || "" }]);
    await setSubstepMedia(substepId, next);
  } catch (err) {
    alert(err.message);
    button.disabled = false;
  }
}

function libRemoveMedia(substepId, existing, url) {
  const next = existing.filter((m) => m.url !== url);
  setSubstepMedia(substepId, next).catch((err) => alert(err.message));
}
