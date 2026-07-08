"""Tests for S05.py: is_palindrome function."""
from S05 import is_palindrome


def test_simple_palindrome():
    assert is_palindrome("racecar") is True


def test_case_insensitive():
    assert is_palindrome("RaceCar") is True


def test_with_punctuation():
    assert is_palindrome("A man, a plan, a canal: Panama") is True


def test_not_palindrome():
    assert is_palindrome("hello world") is False


def test_empty_string():
    assert is_palindrome("") is True


def test_single_char():
    assert is_palindrome("a") is True


def test_numbers():
    assert is_palindrome("12321") is True


def test_mixed_alphanumeric():
    assert is_palindrome("A1b2b1a") is True


def test_chinese_palindrome():
    assert is_palindrome("上海自来水来自海上") is True
