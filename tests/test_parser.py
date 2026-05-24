from webarhive.fetcher.parser import parse_html


def test_parse_basic_fields():
    html = b"""
    <html><head>
      <title>Foo Site</title>
      <meta name="description" content="Cool site about foo">
    </head><body>
      <header><nav>menu menu menu</nav></header>
      <h1>Welcome to Foo</h1>
      <main><p>Real article body about foo. Lots of useful info here.</p></main>
      <footer>footer noise</footer>
      <script>var x = 1;</script>
    </body></html>
    """
    page = parse_html(html, text_limit=200)
    assert page.title == "Foo Site"
    assert page.description == "Cool site about foo"
    assert page.h1 == "Welcome to Foo"
    assert "Real article body" in page.body_text
    # nav/footer/script stripped
    assert "menu" not in page.body_text
    assert "footer noise" not in page.body_text
    assert "var x" not in page.body_text


def test_light_mode_skips_body():
    html = b"<html><head><title>T</title></head><body><p>x</p></body></html>"
    page = parse_html(html)  # text_limit=None
    assert page.title == "T"
    assert page.body_text == ""


def test_cyrillic_via_windows_1251():
    body = "<html><head><title>Привет</title></head><body><h1>Мир</h1></body></html>".encode("cp1251")
    page = parse_html(body, encoding="cp1251")
    assert page.title == "Привет"
    assert page.h1 == "Мир"


def test_og_description_fallback():
    html = b'<html><head><meta property="og:description" content="og desc"></head></html>'
    page = parse_html(html)
    assert page.description == "og desc"


def test_empty_html_safe():
    page = parse_html(b"", text_limit=100)
    assert page.title == "" and page.body_text == ""


def test_body_text_collapses_whitespace_and_caps_at_limit():
    paragraph = "word " * 1000
    html = f"<html><body><p>{paragraph}</p></body></html>".encode()
    page = parse_html(html, text_limit=50)
    assert len(page.body_text) == 50
    assert "  " not in page.body_text
