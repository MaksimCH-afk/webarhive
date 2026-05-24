"""Strict input normalization (spec §2.4).

Any input must end up as `domain.com`. Removes protocol, path, query,
fragment, port, lowercases, strips `www.`, and (by default) trims down
to the registrable root using the Public Suffix List via tldextract.

With CHECK_SUBDOMAINS=True the subdomain is preserved (but `www.` is
still stripped) — this is paired with `matchType=host` in the CDX
client so a subdomain is analyzed as a separate entity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import tldextract

# Lazy private extractor — avoid suffix-list HTTP fetch at import time.
_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

# Heuristic: a "domain-ish" string must contain a dot and only valid label chars.
_LABEL_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")


@dataclass(frozen=True)
class NormalizeResult:
    original: str
    domain: str | None
    ok: bool
    reason: str | None = None  # why rejected, if ok=False


def _strip_url_envelope(raw: str) -> str:
    """Strip scheme/userinfo/path/query/fragment/port. Returns hostname-ish part."""
    s = raw.strip().strip(",;\"'<>()[]{}").strip()
    if not s:
        return ""

    # Insert a fake scheme so urlsplit can parse host-only forms reliably.
    candidate = s
    if "://" not in candidate:
        candidate = "//" + candidate

    try:
        parts = urlsplit(candidate)
    except ValueError:
        return ""

    host = parts.hostname or ""
    return host.strip().lower()


def _is_valid_labels(host: str) -> bool:
    if not host or "." not in host:
        return False
    if len(host) > 253:
        return False
    labels = host.split(".")
    if any(not _LABEL_RE.match(lbl) for lbl in labels):
        return False
    # TLD must be at least 2 chars, non-numeric
    tld = labels[-1]
    if len(tld) < 2 or tld.isdigit():
        return False
    return True


def normalize_domain(raw: str, *, check_subdomains: bool = False) -> NormalizeResult:
    """Normalize a single raw input string to a domain.

    - check_subdomains=False (default): strip down to registrable root
      (`blog.foo.co.uk` → `foo.co.uk`). Aligns with CDX matchType=domain.
    - check_subdomains=True: keep the subdomain (but always strip `www.`).
    """
    original = raw
    host = _strip_url_envelope(raw)
    if not host:
        return NormalizeResult(original, None, False, "пусто")

    # Always strip leading www.
    if host.startswith("www."):
        host = host[4:]

    if not _is_valid_labels(host):
        return NormalizeResult(original, None, False, "невалидный формат")

    if check_subdomains:
        # Strip only `www.` (already done); keep the rest.
        return NormalizeResult(original, host, True)

    # Trim to registrable root using PSL.
    ext = _TLD_EXTRACT(host)
    if not ext.domain or not ext.suffix:
        # No suffix found — could be a custom TLD or garbage. Reject.
        return NormalizeResult(original, None, False, "не распознан суффикс домена")

    root = f"{ext.domain}.{ext.suffix}".lower()
    if not _is_valid_labels(root):
        return NormalizeResult(original, None, False, "невалидный корневой домен")

    return NormalizeResult(original, root, True)
