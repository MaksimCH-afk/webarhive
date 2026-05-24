from datetime import datetime

from webarhive.analysis.drops import find_gaps, score_drops
from webarhive.analysis.topics import TopicEpoch


def test_find_gaps_threshold():
    ts = [
        datetime(2010, 1, 1),
        datetime(2010, 6, 1),
        datetime(2012, 1, 1),  # 19-month gap
        datetime(2012, 5, 1),
    ]
    gaps = find_gaps(ts, min_days=365)
    assert len(gaps) == 1
    assert gaps[0].before == datetime(2010, 6, 1)
    assert gaps[0].after == datetime(2012, 1, 1)


def test_score_drops_marks_topic_change_as_drop():
    epochs = [
        TopicEpoch(datetime(2010, 1, 1), datetime(2010, 12, 31),
                   "информационный_контентный", 0.9, "blog", "u1"),
        TopicEpoch(datetime(2012, 6, 1), datetime(2013, 1, 1),
                   "гемблинг_казино", 0.95, "casino", "u2"),
    ]
    gaps = find_gaps([datetime(2010, 12, 31), datetime(2012, 6, 1)], min_days=365)
    signals = score_drops(gaps, epochs)
    assert len(signals) == 1
    assert signals[0].is_drop is True
    assert "информационный_контентный → гемблинг_казино" in signals[0].reason


def test_score_drops_same_topic_is_not_drop():
    epochs = [
        TopicEpoch(datetime(2010, 1, 1), datetime(2010, 12, 31), "услуги_бизнес", 0.9, "", "u"),
        TopicEpoch(datetime(2012, 6, 1), datetime(2013, 1, 1), "услуги_бизнес", 0.9, "", "u"),
    ]
    gaps = find_gaps([datetime(2010, 12, 31), datetime(2012, 6, 1)], min_days=365)
    signals = score_drops(gaps, epochs)
    assert signals[0].is_drop is False


def test_no_gaps_no_signals():
    epochs = [TopicEpoch(datetime(2020, 1, 1), datetime(2021, 1, 1),
                         "коммерция_магазин", 0.9, "", "u")]
    assert score_drops([], epochs) == []
