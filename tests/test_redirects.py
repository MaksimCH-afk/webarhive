from webarhive.analysis.redirects import _classify_pair, _resolve_target
from webarhive.db.models import RedirectClass


def test_same_domain_is_technical():
    cls, reason, root = _classify_pair(
        source_domain="foo.com", target_url="https://foo.com/about"
    )
    assert cls is RedirectClass.TECHNICAL
    assert root == "foo.com"


def test_www_to_root_is_technical():
    cls, reason, _ = _classify_pair(
        source_domain="foo.com", target_url="https://www.foo.com/"
    )
    assert cls is RedirectClass.TECHNICAL


def test_subdomain_to_root_is_technical():
    cls, _, _ = _classify_pair(
        source_domain="foo.com", target_url="https://blog.foo.com/post"
    )
    assert cls is RedirectClass.TECHNICAL


def test_zone_change_is_same_site():
    cls, reason, root = _classify_pair(
        source_domain="foo.com", target_url="https://foo.net/"
    )
    assert cls is RedirectClass.SAME_SITE
    assert root == "foo.net"


def test_compound_tld_zone_change_via_psl():
    cls, _, root = _classify_pair(
        source_domain="foo.co.uk", target_url="https://foo.com/"
    )
    assert cls is RedirectClass.SAME_SITE
    assert root == "foo.com"


def test_different_core_defaults_to_review_safety():
    cls, reason, root = _classify_pair(
        source_domain="foo.com", target_url="https://casino-evil.net/"
    )
    assert cls is RedirectClass.REVIEW
    assert root == "casino-evil.net"


def test_unresolvable_target_is_review():
    cls, reason, _ = _classify_pair(source_domain="foo.com", target_url=None)
    assert cls is RedirectClass.REVIEW


def test_resolve_target_unwraps_wayback_url():
    final = "https://web.archive.org/web/20200101120000/https://target.com/path"
    target = _resolve_target(
        from_original="http://foo.com/",
        final_url=final,
        location_header=None,
        body=b"",
        encoding=None,
    )
    assert target == "https://target.com/path"


def test_resolve_target_relative_location_against_original():
    target = _resolve_target(
        from_original="http://foo.com/old/page",
        final_url="",
        location_header="/new/page",
        body=b"",
        encoding=None,
    )
    assert target == "http://foo.com/new/page"


def test_resolve_target_meta_refresh_fallback():
    body = b'<html><head><meta http-equiv="refresh" content="0;url=https://target.com/x"></head></html>'
    target = _resolve_target(
        from_original="http://foo.com/",
        final_url="",
        location_header=None,
        body=body,
        encoding="utf-8",
    )
    assert target == "https://target.com/x"
