from S01 import is_even


def test_is_even():
    assert is_even(0) is True
    assert is_even(1) is False
    assert is_even(2) is True
    assert is_even(-2) is True
    assert is_even(-3) is False
    assert is_even(100) is True
