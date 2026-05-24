"""HTML parsing for topic analysis (spec §6).

Two grades:
- light  (etap 1): title + meta description + h1 — pulled cheaply.
- heavy  (etap 3): the above PLUS first TEXT_LIMIT chars of *significant*
  text, where significant = body after stripping nav / footer / scripts.
  Per spec §6, title/description/h1 go ABOVE the TEXT_LIMIT — the limit
  applies only to body text.
"""

from __future__ import annotations

from dataclasses import dataclass

from selectolax.parser import HTMLParser

STRIP_TAGS = ("script", "style", "noscript", "iframe", "svg", "template")
STRIP_BY_ROLE = ("nav", "header", "footer", "aside")  # by tag and by role attr


@dataclass(frozen=True, slots=True)
class ParsedPage:
    title: str
    description: str
    h1: str
    body_text: str  # already trimmed to text_limit (heavy mode only)


def _decode(body: bytes, encoding: str | None) -> str:
    if not body:
        return ""
    if encoding:
        try:
            return body.decode(encoding, errors="replace")
        except LookupError:
            pass
    # Fallback: utf-8 lossy.
    return body.decode("utf-8", errors="replace")


def _meta_description(tree: HTMLParser) -> str:
    for node in tree.css('meta[name="description"], meta[property="og:description"]'):
        content = node.attributes.get("content")
        if content:
            return content.strip()
    return ""


def _first_h1(tree: HTMLParser) -> str:
    node = tree.css_first("h1")
    if node is None:
        return ""
    return (node.text(separator=" ") or "").strip()


def _significant_body_text(tree: HTMLParser, text_limit: int) -> str:
    # Drop noise tags wholesale.
    for sel in (*STRIP_TAGS, *STRIP_BY_ROLE):
        for node in tree.css(sel):
            node.decompose()
    # Also drop common "navigation" roles by attribute.
    for node in tree.css('[role="navigation"], [role="banner"], [role="contentinfo"]'):
        node.decompose()

    body = tree.body
    if body is None:
        return ""
    text = body.text(separator=" ")
    # Collapse whitespace.
    collapsed = " ".join(text.split())
    return collapsed[:text_limit]


def parse_html(
    body: bytes,
    *,
    encoding: str | None = None,
    text_limit: int | None = None,
) -> ParsedPage:
    """Parse HTML body.

    If text_limit is None → light mode (skip body extraction).
    """
    html = _decode(body, encoding)
    if not html.strip():
        return ParsedPage(title="", description="", h1="", body_text="")

    tree = HTMLParser(html)

    title_node = tree.css_first("title")
    title = (title_node.text() if title_node else "").strip()

    description = _meta_description(tree)
    h1 = _first_h1(tree)

    body_text = ""
    if text_limit is not None and text_limit > 0:
        body_text = _significant_body_text(tree, text_limit)

    return ParsedPage(title=title, description=description, h1=h1, body_text=body_text)
