from webarhive.domains.loader import load_from_bytes, load_from_text


def test_load_text_dedup_and_normalize():
    text = """
    https://foo.com/x
    www.foo.com
    bar.com
    bar.com
    нет домена
    """
    rep = load_from_text(text)
    assert rep.valid_unique == ["foo.com", "bar.com"]
    assert len(rep.rejected) == 1
    assert rep.raw_lines == 5
    assert rep.dropped == 3  # 2 dups of foo, 1 garbage


def test_load_text_with_commas():
    rep = load_from_text("foo.com, bar.com; baz.com")
    assert sorted(rep.valid_unique) == ["bar.com", "baz.com", "foo.com"]


def test_load_txt_file():
    data = b"foo.com\nbar.com\n\n"
    rep = load_from_bytes("list.txt", data)
    assert rep.valid_unique == ["foo.com", "bar.com"]


def test_load_csv_first_column_only():
    data = b"domain,note\nfoo.com,first\nbar.com,second\n"
    rep = load_from_bytes("list.csv", data)
    # Header row "domain" is rejected as no dot.
    assert rep.valid_unique == ["foo.com", "bar.com"]


def test_load_with_subdomains_flag():
    rep = load_from_text("blog.foo.com\nshop.foo.com\n", check_subdomains=True)
    assert sorted(rep.valid_unique) == ["blog.foo.com", "shop.foo.com"]
