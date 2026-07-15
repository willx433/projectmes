"""HTML sanitizer for substep `body_html`/`action` rich-text (P2-05, DD §7.2).

Rich text is a deliberately small subset — bold, lists, paragraphs, and a
"warning" callout div — authored via toolbar buttons that wrap the current
textarea selection (see templates/admin/library_set.html), never a full
WYSIWYG/contenteditable surface. Sanitized server-side on every save so a
pasted `<script>` or stray attribute never reaches the station tablet or the
PDF renderer.

stdlib only (html.parser) per CR-003 — no bleach/lxml dependency for an
allowlist this small.
"""
from __future__ import annotations

from html import escape
from html.parser import HTMLParser

ALLOWED_TAGS = {"b", "strong", "i", "em", "ul", "ol", "li", "p", "br", "div"}
# Only `div` may carry an attribute, and only `class="warning"`.
ALLOWED_DIV_CLASS = "warning"
VOID_TAGS = {"br"}
# HTMLParser hands these tags' raw inner text to handle_data (CDATA content
# elements) even though we never emit a start tag for them -- must drop the
# text too, not just the tag, or `<script>alert(1)</script>` leaks its body.
RAW_TEXT_TAGS = {"script", "style", "textarea", "title"}


class _Sanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._open_stack: list[str] = []
        self._raw_text_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in RAW_TEXT_TAGS:
            self._raw_text_depth += 1
            return
        self._emit_start(tag, attrs, self_closing=False)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._emit_start(tag, attrs, self_closing=True)

    def _emit_start(
        self, tag: str, attrs: list[tuple[str, str | None]], *, self_closing: bool
    ) -> None:
        if tag not in ALLOWED_TAGS:
            return
        if tag == "div":
            attr_dict = dict(attrs)
            if attr_dict.get("class") == ALLOWED_DIV_CLASS:
                self.out.append('<div class="warning">')
            else:
                self.out.append("<div>")
        else:
            self.out.append(f"<{tag}>")
        if tag in VOID_TAGS or self_closing:
            return
        self._open_stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in RAW_TEXT_TAGS:
            self._raw_text_depth = max(0, self._raw_text_depth - 1)
            return
        if tag not in ALLOWED_TAGS or tag in VOID_TAGS:
            return
        if tag in self._open_stack:
            # Close back to (and including) the matching open tag, in case of
            # unbalanced input -- never emit an end tag with no matching start.
            while self._open_stack:
                open_tag = self._open_stack.pop()
                self.out.append(f"</{open_tag}>")
                if open_tag == tag:
                    break

    def handle_data(self, data: str) -> None:
        if self._raw_text_depth:
            return
        self.out.append(escape(data, quote=False))

    def close(self) -> None:
        super().close()
        while self._open_stack:
            self.out.append(f"</{self._open_stack.pop()}>")


def sanitize_html(raw: str | None) -> str | None:
    """Strip everything but the allowlisted tags (script/style/attributes
    beyond `div class="warning"` are dropped, not escaped-and-kept)."""
    if raw is None:
        return None
    parser = _Sanitizer()
    parser.feed(raw)
    parser.close()
    return "".join(parser.out)


def _demo() -> None:
    assert sanitize_html(None) is None
    assert sanitize_html("") == ""
    assert sanitize_html("<b>bold</b>") == "<b>bold</b>"
    assert sanitize_html("<script>alert(1)</script>hi") == "hi"
    assert sanitize_html('<div class="warning">careful</div>') == '<div class="warning">careful</div>'
    assert sanitize_html('<div class="evil">x</div>') == "<div>x</div>"
    assert sanitize_html("<p onclick='x()'>hi</p>") == "<p>hi</p>"
    assert sanitize_html("<ul><li>a</li><li>b</li></ul>") == "<ul><li>a</li><li>b</li></ul>"
    assert sanitize_html("<b>unclosed") == "<b>unclosed</b>"
    assert sanitize_html("plain & <b>text</b>") == "plain &amp; <b>text</b>"
    print("ok")


if __name__ == "__main__":
    _demo()
