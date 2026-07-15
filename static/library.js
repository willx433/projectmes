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
