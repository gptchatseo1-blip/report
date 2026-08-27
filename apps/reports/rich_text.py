"""Small allow-list sanitizer for report-builder rich text."""

from html import escape
from html.parser import HTMLParser
from urllib.parse import urlsplit

ALLOWED_TAGS = {"p", "br", "ul", "ol", "li", "strong", "em", "a"}
TAG_ALIASES = {"b": "strong", "i": "em", "div": "p"}


def _safe_href(value):
    value = str(value or "").strip()
    parsed = urlsplit(value)
    return value if parsed.scheme in {"http", "https"} and parsed.netloc else ""


class _Sanitizer(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_starttag(self, tag, attrs):
        tag = TAG_ALIASES.get(tag.casefold(), tag.casefold())
        if tag not in ALLOWED_TAGS:
            return
        if tag == "br":
            self.parts.append("<br>")
            return
        if tag == "a":
            href = _safe_href(dict(attrs).get("href"))
            self.parts.append(f'<a href="{escape(href, quote=True)}">' if href else "<a>")
            return
        self.parts.append(f"<{tag}>")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        tag = TAG_ALIASES.get(tag.casefold(), tag.casefold())
        if tag in ALLOWED_TAGS and tag != "br":
            self.parts.append(f"</{tag}>")

    def handle_data(self, data):
        self.parts.append(escape(data, quote=False))


def sanitize_report_html(value, *, max_length=20_000):
    parser = _Sanitizer()
    parser.feed(str(value or "")[:max_length])
    parser.close()
    return "".join(parser.parts).strip()
