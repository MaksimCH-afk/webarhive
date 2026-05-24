from webarhive.domains.normalize import normalize_domain


def test_strip_protocol_and_path():
    r = normalize_domain("https://Example.com/page?x=1#frag")
    assert r.ok and r.domain == "example.com"


def test_strip_www_always():
    assert normalize_domain("www.foo.com").domain == "foo.com"
    assert normalize_domain("www.blog.foo.com").domain == "foo.com"
    assert normalize_domain("www.foo.com", check_subdomains=True).domain == "foo.com"


def test_strip_port():
    assert normalize_domain("http://foo.com:8080/").domain == "foo.com"


def test_compound_tld_via_psl():
    # blog.foo.co.uk → foo.co.uk (without PSL would mistakenly become co.uk)
    assert normalize_domain("blog.foo.co.uk").domain == "foo.co.uk"
    assert normalize_domain("shop.foo.com.br").domain == "foo.com.br"


def test_subdomain_preserved_with_flag():
    r = normalize_domain("blog.foo.com", check_subdomains=True)
    assert r.ok and r.domain == "blog.foo.com"


def test_subdomain_stripped_by_default():
    r = normalize_domain("blog.foo.com")
    assert r.ok and r.domain == "foo.com"


def test_lowercase():
    assert normalize_domain("FOO.COM").domain == "foo.com"


def test_reject_empty():
    assert not normalize_domain("").ok
    assert not normalize_domain("   ").ok


def test_reject_no_dot():
    assert not normalize_domain("localhost").ok


def test_reject_garbage():
    assert not normalize_domain("not a domain at all").ok
    assert not normalize_domain("///").ok


def test_trim_surrounding_punctuation():
    assert normalize_domain('"foo.com",').domain == "foo.com"


def test_userinfo_stripped():
    assert normalize_domain("https://user:pass@foo.com/path").domain == "foo.com"
