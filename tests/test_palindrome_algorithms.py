"""Tests for palindrome_algorithms package — correctness and edge cases."""

import pytest

from palindrome_algorithms import (
    is_palindrome,
    is_palindrome_two_pointer,
    is_palindrome_number,
    is_palindrome_sentence,
    longest_palindromic_substring,
    count_palindromic_substrings,
)

# 参考值
PALINDROME_REF = {
    "": True,
    "a": True,
    "ab": False,
    "aa": True,
    "aba": True,
    "abba": True,
    "abcba": True,
    "hello": False,
    "racecar": True,
    "madam": True,
    "level": True,
    "python": False,
}

ALL_IMPLS = [
    ("simple_reverse", is_palindrome),
    ("two_pointer", is_palindrome_two_pointer),
]


class TestPalindromeString:
    """基础回文检测。"""

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_reference_values(self, name: str, impl):
        """测试标准回文字符串。"""
        for s, expected in PALINDROME_REF.items():
            # 基础版关闭 ignore_case 和 ignore_whitespace
            assert impl(s, ignore_case=False, ignore_whitespace=False) == expected, (
                f"{name}({s!r}) = {impl(s)}, expected {expected}"
            )

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_ignore_case(self, name: str, impl):
        """忽略大小写测试。"""
        assert impl("Aba", ignore_case=True, ignore_whitespace=False) is True
        assert impl("ABa", ignore_case=True, ignore_whitespace=False) is True
        assert impl("Hello", ignore_case=False, ignore_whitespace=False) is False

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_ignore_whitespace(self, name: str, impl):
        """忽略空格测试。"""
        assert impl("a man a plan a canal panama", ignore_case=True,
                     ignore_whitespace=True) is True
        assert impl(" race car ", ignore_case=True,
                     ignore_whitespace=True) is True
        assert impl("hello world", ignore_case=False,
                     ignore_whitespace=False) is False

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_ignore_punctuation(self, name: str, impl):
        """忽略标点测试（仅双指针版支持）。"""
        if impl is is_palindrome_two_pointer:
            assert is_palindrome_sentence("A man, a plan, a canal: Panama") is True
            assert is_palindrome_sentence("Madam, I'm Adam") is True
            assert is_palindrome_sentence("Python, rules!") is False

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_type_error_on_non_string(self, name: str, impl):
        """非字符串输入必须抛出 TypeError。"""
        invalid_inputs = [123, None, [1, 2, 3], {"a": 1}, 3.14]
        for invalid in invalid_inputs:
            with pytest.raises(TypeError, match="必须是字符串|must be.*str"):
                impl(invalid)

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_edge_cases(self, name: str, impl):
        """边界值测试。"""
        # 单字符
        assert impl("a", ignore_case=False, ignore_whitespace=False) is True
        assert impl("z", ignore_case=False, ignore_whitespace=False) is True
        # 双字符
        assert impl("ab", ignore_case=False, ignore_whitespace=False) is False
        assert impl("aa", ignore_case=False, ignore_whitespace=False) is True

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_consistency_across_implementations(self, name: str, impl):
        """所有回文检测实现结果一致。"""
        samples = ["", "a", "ab", "aa", "aba", "abba", "abcba",
                    "hello", "racecar", "madam", "level",
                    "A man a plan a canal Panama"]
        for s in samples:
            # 都启用忽略大小写和空格
            expected = is_palindrome(s, ignore_case=True, ignore_whitespace=True)
            actual = impl(s, ignore_case=True, ignore_whitespace=True)
            assert actual == expected, (
                f"{name}({s!r}) = {actual}, expected {expected}"
            )


class TestPalindromeNumber:
    """整数回文检测。"""

    def test_positive_palindromes(self):
        """正回文数。"""
        assert is_palindrome_number(121) is True
        assert is_palindrome_number(1221) is True
        assert is_palindrome_number(12321) is True
        assert is_palindrome_number(0) is True
        assert is_palindrome_number(1) is True
        assert is_palindrome_number(11) is True

    def test_non_palindromes(self):
        """非回文数。"""
        assert is_palindrome_number(123) is False
        assert is_palindrome_number(10) is False
        assert is_palindrome_number(1234) is False
        assert is_palindrome_number(100) is False

    def test_negative_rejected(self):
        """负数不是回文数。"""
        assert is_palindrome_number(-121) is False
        assert is_palindrome_number(-1) is False
        assert is_palindrome_number(-11) is False

    def test_large_palindromes(self):
        """大回文数。"""
        assert is_palindrome_number(123456789987654321) is True
        assert is_palindrome_number(12345678987654321) is True

    def test_trailing_zero(self):
        """以 0 结尾的数（0 除外）不是回文数。"""
        assert is_palindrome_number(10) is False
        assert is_palindrome_number(110) is False
        assert is_palindrome_number(1000) is False


class TestPalindromeSentence:
    """英文句子回文检测。"""

    def test_known_sentences(self):
        """已知英文回文句。"""
        assert is_palindrome_sentence("A man, a plan, a canal: Panama") is True
        assert is_palindrome_sentence("Madam, I'm Adam") is True
        assert is_palindrome_sentence("Never odd or even") is True
        assert is_palindrome_sentence("Was it a car or a cat I saw") is True

    def test_non_palindrome_sentences(self):
        """非回文句。"""
        assert is_palindrome_sentence("Hello world") is False
        assert is_palindrome_sentence("Python is great") is False
        assert is_palindrome_sentence("This is not a palindrome") is False


class TestLongestPalindromicSubstring:
    """最长回文子串（Manacher O(n)）。"""

    def test_basic_cases(self):
        """基本测试。"""
        result = longest_palindromic_substring("babad")
        assert result in ("bab", "aba"), f"Expected 'bab' or 'aba', got {result!r}"

        assert longest_palindromic_substring("cbbd") == "bb"
        assert longest_palindromic_substring("a") == "a"
        assert longest_palindromic_substring("ac") in ("a", "c")

    def test_edge_cases(self):
        """边界测试。"""
        assert longest_palindromic_substring("") == ""
        assert longest_palindromic_substring("a") == "a"
        assert longest_palindromic_substring("aa") == "aa"
        assert longest_palindromic_substring("ab") in ("a", "b")

    def test_whole_string_is_palindrome(self):
        """整个字符串是回文。"""
        assert longest_palindromic_substring("racecar") == "racecar"
        assert longest_palindromic_substring("abba") == "abba"
        assert longest_palindromic_substring("aabbaa") == "aabbaa"

    def test_type_error(self):
        """类型错误。"""
        with pytest.raises(TypeError, match="必须是字符串|must be.*str"):
            longest_palindromic_substring(123)


class TestCountPalindromicSubstrings:
    """回文子串计数。"""

    def test_basic_cases(self):
        """基本测试。"""
        assert count_palindromic_substrings("abc") == 3  # a, b, c
        assert count_palindromic_substrings("aaa") == 6  # a,a,a,aa,aa,aaa
        assert count_palindromic_substrings("abba") == 6  # a,b,b,a,bb,abba

    def test_edge_cases(self):
        """边界测试。"""
        assert count_palindromic_substrings("") == 0
        assert count_palindromic_substrings("a") == 1
        assert count_palindromic_substrings("aa") == 3  # a, a, aa

    def test_type_error(self):
        """类型错误。"""
        with pytest.raises(TypeError, match="必须是字符串|must be.*str"):
            count_palindromic_substrings(123)


class TestPalindromicSubstringQuery:
    """快速回文子串查询。"""

    def test_substring_queries(self):
        """子串回文查询。"""
        from palindrome_algorithms.level3_manacher import is_palindromic_substring

        assert is_palindromic_substring("racecar", 0, 6) is True   # racecar
        assert is_palindromic_substring("racecar", 1, 4) is True   # acea
        assert is_palindromic_substring("racecar", 0, 3) is False  # race
        assert is_palindromic_substring("racecar", 2, 2) is True   # c
        assert is_palindromic_substring("abba", 0, 3) is True      # abba
        assert is_palindromic_substring("abba", 1, 2) is True      # bb

    def test_invalid_indices(self):
        """无效索引。"""
        from palindrome_algorithms.level3_manacher import is_palindromic_substring

        with pytest.raises(IndexError):
            is_palindromic_substring("abc", -1, 1)
        with pytest.raises(IndexError):
            is_palindromic_substring("abc", 0, 5)