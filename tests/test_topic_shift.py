from webarhive.analysis.topic_shift import VersionFingerprint, is_shift


def fp(title="", desc="", h1=""):
    return VersionFingerprint.from_fields(title, desc, h1)


def test_permutation_is_not_a_shift():
    a = fp("Купить пицца онлайн", "Доставка домой", "Главная")
    b = fp("Онлайн доставка пицца", "Главная", "Купить домой")
    # Same significant tokens (matched forms), just rearranged
    assert a.diff_count(b) == 0
    assert not is_shift(a, b, threshold=2)


def test_below_threshold_is_not_a_shift():
    a = fp("foo bar baz", "qux", "")
    b = fp("foo bar baz qux extra", "", "")
    # one added word
    assert a.diff_count(b) == 1
    assert not is_shift(a, b, threshold=2)


def test_at_threshold_is_not_a_shift_strictly_greater():
    a = fp("foo bar", "", "")
    b = fp("foo bar baz qux", "", "")
    # two added words → diff = 2, threshold = 2 → NOT a shift
    assert a.diff_count(b) == 2
    assert not is_shift(a, b, threshold=2)


def test_above_threshold_is_a_shift():
    a = fp("пицца доставка ресторан", "", "")
    b = fp("казино рулетка слоты бонус", "", "")
    assert is_shift(a, b, threshold=2)


def test_stop_words_stripped():
    a = fp("the a of in", "", "")
    b = fp("на и в по", "", "")
    # both empty after stripping
    assert a.words == () and b.words == ()
    assert a.diff_count(b) == 0


def test_case_insensitive():
    a = fp("FOO Bar", "", "")
    b = fp("foo bar", "", "")
    assert a.diff_count(b) == 0
