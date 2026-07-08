"""Tests for reverse_string_algorithms package — correctness and edge cases."""

import pytest

from reverse_string_algorithms import (
    reverse_slice,
    reverse_two_pointer,
    reverse_recursive,
    reverse_join,
)

# 参考值
REVERSE_REF = {
    "": "",
    "a": "a",
    "ab": "ba",
    "abc": "cba",
    "hello": "olleh",
    "Python": "nohtyP",
    "12345": "54321",
    "racecar": "racecar",  # 回文，反转等于自身
    "!@#": "#@!",
    "你好世界": "界世好你",  # Unicode 支持
}

ALL_IMPLS = [
    ("slice", reverse_slice),
    ("two_pointer", reverse_two_pointer),
    ("recursive", reverse_recursive),
    ("join", reverse_join),
]


class TestReverseStringCorrectness:
    """所有实现必须匹配参考值。"""

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_reference_values(self, name: str, impl):
        """测试预定义的参考反转值。"""
        for input_str, expected in REVERSE_REF.items():
            assert impl(input_str) == expected, (
                f"{name}({input_str!r}) = {impl(input_str)!r}, "
                f"expected {expected!r}"
            )

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_edge_cases(self, name: str, impl):
        """边界值测试。"""
        # 空字符串
        assert impl("") == ""
        # 单字符
        assert impl("a") == "a"
        assert impl("z") == "z"
        # 双字符
        assert impl("ab") == "ba"
        assert impl("xy") == "yx"

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_palindrome_reverse(self, name: str, impl):
        """回文串反转后等于自身。"""
        palindromes = ["racecar", "madam", "level", "上海自来水来自海上"]
        for p in palindromes:
            assert impl(p) == p, f"{name}({p!r}) should equal itself"

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_double_reverse(self, name: str, impl):
        """两次反转还原原始字符串。"""
        samples = ["hello", "Python", "abc123", "你好世界", "!@#$%"]
        for s in samples:
            assert impl(impl(s)) == s, (
                f"double {name}({s!r}) should restore original"
            )

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_negative_rejected(self, name: str, impl):
        """非字符串输入必须抛出 TypeError。"""
        invalid_inputs = [123, None, [1, 2, 3], {"a": 1}, 3.14, True]
        for invalid in invalid_inputs:
            with pytest.raises(TypeError, match="必须是字符串|must be.*str"):
                impl(invalid)

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_consistency_across_implementations(self, name: str, impl):
        """所有实现结果一致。"""
        samples = ["", "a", "ab", "hello", "Python", "12345", "你好世界", "racecar"]
        for s in samples:
            ref = reverse_slice(s)
            assert impl(s) == ref, (
                f"{name}({s!r}) = {impl(s)!r}, expected {ref!r}"
            )


class TestReverseStringPerformance:
    """性能基准：确保实现能在合理时间内完成。"""

    def test_long_string_slice(self):
        """切片法处理长字符串。"""
        s = "a" * 100_000
        result = reverse_slice(s)
        assert len(result) == 100_000
        assert result == "a" * 100_000

    def test_long_string_two_pointer(self):
        """双指针法处理长字符串。"""
        s = "x" * 100_000
        result = reverse_two_pointer(s)
        assert len(result) == 100_000
        assert result == "x" * 100_000

    def test_long_string_join(self):
        """join + reversed 法处理长字符串。"""
        s = "z" * 100_000
        result = reverse_join(s)
        assert len(result) == 100_000
        assert result == "z" * 100_000

    def test_reverse_mixed_content(self):
        """混合内容反转验证。"""
        original = "Hello, 世界! 123"
        expected = "321 !界世 ,olleH"
        for name, impl in ALL_IMPLS:
            if name == "recursive" and len(original) > 900:
                continue  # 递归法跳过超长字符串
            result = impl(original)
            assert result == expected, f"{name} failed: {result!r} != {expected!r}"


class TestReverseStringSpecialCases:
    """特殊场景测试。"""

    def test_recursive_depth_limit(self):
        """递归法在超长字符串时预期抛出 RecursionError。"""
        with pytest.raises(RecursionError):
            reverse_recursive("a" * 2000)

    def test_whitespace_preserved(self):
        """所有实现应保留空白字符。"""
        samples = [
            ("hello world", "dlrow olleh"),
            ("  abc  ", "  cba  "),
            ("\t\n", "\n\t"),
        ]
        for name, impl in ALL_IMPLS:
            for input_str, expected in samples:
                assert impl(input_str) == expected, (
                    f"{name}({input_str!r}) = {impl(input_str)!r}, "
                    f"expected {expected!r}"
                )

    def test_unicode_support(self):
        """Unicode 支持验证。"""
        samples = {
            "αβγ": "γβα",
            "абв": "вба",
            "가나다": "다나가",
            "🐍🔥": "🔥🐍",  # emoji
        }
        for name, impl in ALL_IMPLS:
            for input_str, expected in samples.items():
                assert impl(input_str) == expected, (
                    f"{name}({input_str!r}) = {impl(input_str)!r}, "
                    f"expected {expected!r}"
                )