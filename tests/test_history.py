from webarhive.analysis.history import bucketize, summarize_history
from webarhive.cdx.client import CdxRow


def _row(ts: str, status: str) -> CdxRow:
    return CdxRow(
        urlkey="com,foo)/",
        timestamp=ts,
        original=f"http://foo.com/?t={ts}",
        mimetype="text/html",
        statuscode=status,
        digest="X" * 32,
        length="1",
    )


def test_bucketize_drops_5xx_and_garbage():
    rows = [
        _row("20100101000000", "200"),
        _row("20110101000000", "301"),
        _row("20120101000000", "404"),
        _row("20130101000000", "503"),
        _row("20140101000000", "-"),
    ]
    buckets = bucketize(rows)
    assert len(buckets["200"]) == 1
    assert len(buckets["3xx"]) == 1
    assert len(buckets["404"]) == 1
    assert "5xx" not in buckets


def test_age_is_first_archive_activity():
    rows = [
        _row("20180101000000", "200"),
        _row("20100315120000", "200"),
        _row("20220101000000", "404"),
    ]
    s = summarize_history(rows)
    assert s.first_capture_at is not None
    assert s.first_capture_at.year == 2010
    assert s.last_capture_at.year == 2022
    assert s.total_captures == 3


def test_timeline_sorted_and_filtered():
    rows = [
        _row("20120101000000", "200"),
        _row("20100101000000", "200"),
        _row("20110101000000", "500"),  # dropped from timeline
    ]
    s = summarize_history(rows)
    ts = [e.timestamp for e in s.timeline]
    assert ts == sorted(ts)
    assert len(s.timeline) == 2


def test_empty():
    s = summarize_history([])
    assert s.total_captures == 0
    assert s.first_capture_at is None
    assert s.age_days is None
