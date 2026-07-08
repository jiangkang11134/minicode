"""字符串比较算法单元测试。

测试覆盖：
  - Level 1: 精确匹配、汉明距离、最长公共前缀
  - Level 2: Levenshtein 编辑距离、相似度比率、Damerau-Levenshtein 距离
  - Level 3: KMP 搜索、朴素搜索、Rabin-Karp 搜索
"""

import pytest
from string_comparison_algorithms import (
    exact_match,
    hamming_distance,
    longest_common_prefix,
    levenshtein_distance,
    levenshtein_ratio,
    damerau_levenshtein_distance,
    kmp_search,
    kmp_search_all,
    naive_search,
    naive_search_all,
    rabin_karp_search,
    rabin_karp_search_all,
)

# ============================================================================
# Level 1 — 基础比较
# ============================================================================

EXACT_MATCH_REF = {
    ("", ""): True,
    ("a", "a"): True,
    ("hello", "hello"): True,
    ("abc", "abc"): True,
    ("a", "b"): False,
    ("hello", "world"): False,
    ("abc", "abcd"): False,
    ("abc", "ab"): False,
    ("你好", "你好"): True,
    ("你好", "世界"): False,
}

HAMMING_REF = {
    ("abcde", "abcde"): 0,
    ("abcde", "abcdf"): 1,
    ("abcde", "fbcde"): 1,
    ("abcde", "fghij"): 5,
    ("", ""): 0,
    ("karolin", "kathrin"): 3,
    ("1011101", "1001001"): 2,
}

LCP_REF = [
    ([""], ""),
    (["a"], "a"),
    (["abc"], "abc"),
    (["abc", "abd"], "ab"),
    (["abc", "abc"], "abc"),
    (["abc", "ab"], "ab"),
    (["abc", "a"], "a"),
    (["abc", "abd", "abf"], "ab"),
    (["", "abc"], ""),
    (["hello", "world"], ""),
    (["你好吗", "你好"], "你好"),
]


class TestExactMatch:
    """测试 exact_match 精确匹配函数。"""

    @pytest.mark.parametrize("s1,s2,expected", [
        (s1, s2, exp) for (s1, s2), exp in EXACT_MATCH_REF.items()
    ])
    def test_reference_values(self, s1: str, s2: str, expected: bool):
        """测试已知输入输出对。"""
        assert exact_match(s1, s2) == expected

    def test_type_error_on_non_string(self):
        """非字符串输入抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须"):
            exact_match(123, "abc")
        with pytest.raises(TypeError, match="必须"):
            exact_match("abc", None)
        with pytest.raises(TypeError, match="必须"):
            exact_match([], "abc")


class TestHammingDistance:
    """测试 hamming_distance 汉明距离函数。"""

    @pytest.mark.parametrize("s1,s2,expected", [
        (s1, s2, exp) for (s1, s2), exp in HAMMING_REF.items()
    ])
    def test_reference_values(self, s1: str, s2: str, expected: int):
        """测试已知输入输出对。"""
        assert hamming_distance(s1, s2) == expected

    def test_type_error_on_non_string(self):
        """非字符串输入抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须"):
            hamming_distance(123, "abc")
        with pytest.raises(TypeError, match="必须"):
            hamming_distance("abc", None)

    def test_value_error_on_mismatched_length(self):
        """长度不同的字符串抛出 ValueError。"""
        with pytest.raises(ValueError, match="长度"):
            hamming_distance("abc", "ab")
        with pytest.raises(ValueError, match="长度"):
            hamming_distance("a", "")


class TestLongestCommonPrefix:
    """测试 longest_common_prefix 最长公共前缀函数。"""

    @pytest.mark.parametrize("strs,expected", LCP_REF)
    def test_reference_values(self, strs: list[str], expected: str):
        """测试已知输入输出对。"""
        assert longest_common_prefix(strs) == expected

    def test_type_error_on_non_list(self):
        """非列表输入抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须"):
            longest_common_prefix("abc")
        with pytest.raises(TypeError, match="必须"):
            longest_common_prefix(None)

    def test_value_error_on_empty_list(self):
        """空列表抛出 ValueError。"""
        with pytest.raises(ValueError, match="不能为空"):
            longest_common_prefix([])

    def test_type_error_on_non_string_in_list(self):
        """列表中非字符串元素抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须"):
            longest_common_prefix(["abc", 123])


# ============================================================================
# Level 2 — Levenshtein 编辑距离
# ============================================================================

LEVENSHTEIN_REF = {
    ("", ""): 0,
    ("a", ""): 1,
    ("", "a"): 1,
    ("abc", "abc"): 0,
    ("kitten", "sitting"): 3,
    ("saturday", "sunday"): 3,
    ("book", "back"): 2,
    ("abc", "abcd"): 1,
    ("abcd", "abc"): 1,
    ("abc", "abd"): 1,
    ("abc", "xyz"): 3,
    ("你好", "你好吗"): 1,
    ("abc", "acb"): 2,  # 替换 b->c + 替换 c->b => 2 (swap 需要 2)
}

LEVENSHTEIN_RATIO_REF = {
    ("", ""): 1.0,
    ("abc", "abc"): 1.0,
    ("kitten", "sitting"): (6 + 7 - 3) / (6 + 7),
    ("abc", "xyz"): (3 + 3 - 3) / (3 + 3),
}

DAMERAU_REF = {
    ("", ""): 0,
    ("a", ""): 1,
    ("", "a"): 1,
    ("abc", "abc"): 0,
    ("kitten", "sitting"): 3,
    ("ab", "ba"): 1,  # 交换操作
    ("abc", "acb"): 1,  # 交换 b 和 c
    ("book", "back"): 2,
    ("abcd", "badc"): 2,  # 两次交换
    ("你好吗", "你好"): 1,
}


class TestLevenshteinDistance:
    """测试 levenshtein_distance 编辑距离。"""

    @pytest.mark.parametrize("s1,s2,expected", [
        (s1, s2, exp) for (s1, s2), exp in LEVENSHTEIN_REF.items()
    ])
    def test_reference_values(self, s1: str, s2: str, expected: int):
        """测试已知输入输出对。"""
        assert levenshtein_distance(s1, s2) == expected

    def test_type_error_on_non_string(self):
        """非字符串输入抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须"):
            levenshtein_distance(123, "abc")
        with pytest.raises(TypeError, match="必须"):
            levenshtein_distance("abc", None)

    def test_large_strings(self):
        """大字符串不崩溃。"""
        s1 = "a" * 1000
        s2 = "b" * 1000
        assert levenshtein_distance(s1, s2) == 1000
        assert levenshtein_distance(s1, s1) == 0

    def test_symmetric(self):
        """编辑距离满足对称性。"""
        pairs = [("abc", "xyz"), ("kitten", "sitting"), ("book", "back")]
        for s1, s2 in pairs:
            assert levenshtein_distance(s1, s2) == levenshtein_distance(s2, s1)


class TestLevenshteinRatio:
    """测试 levenshtein_ratio 相似度比率。"""

    @pytest.mark.parametrize("s1,s2,expected", [
        (s1, s2, exp) for (s1, s2), exp in LEVENSHTEIN_RATIO_REF.items()
    ])
    def test_reference_values(self, s1: str, s2: str, expected: float):
        """测试已知输入输出对。"""
        result = levenshtein_ratio(s1, s2)
        assert abs(result - expected) < 1e-10

    def test_type_error_on_non_string(self):
        """非字符串输入抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须"):
            levenshtein_ratio(123, "abc")
        with pytest.raises(TypeError, match="必须"):
            levenshtein_ratio("abc", None)

    def test_ratio_range(self):
        """相似度比率应在 0.0 ~ 1.0 之间。"""
        pairs = [("abc", "xyz"), ("abc", "abc"), ("abc", ""), ("", "abc")]
        for s1, s2 in pairs:
            r = levenshtein_ratio(s1, s2)
            assert 0.0 <= r <= 1.0, f"比率 {r} 超出 [0, 1]"

    def test_identical_strings(self):
        """相同字符串比率为 1.0。"""
        assert levenshtein_ratio("abc", "abc") == 1.0

    def test_completely_different_strings(self):
        """完全不同字符串比率较低（可能不严格为 0）。"""
        r = levenshtein_ratio("abc", "xyz")
        assert r < 0.1


class TestDamerauLevenshteinDistance:
    """测试 damerau_levenshtein_distance 距离。"""

    @pytest.mark.parametrize("s1,s2,expected", [
        (s1, s2, exp) for (s1, s2), exp in DAMERAU_REF.items()
    ])
    def test_reference_values(self, s1: str, s2: str, expected: int):
        """测试已知输入输出对。"""
        assert damerau_levenshtein_distance(s1, s2) == expected

    def test_type_error_on_non_string(self):
        """非字符串输入抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须"):
            damerau_levenshtein_distance(123, "abc")
        with pytest.raises(TypeError, match="必须"):
            damerau_levenshtein_distance("abc", None)

    def test_swap_is_one_operation(self):
        """相邻字符交换（transposition）计为 1 次操作。"""
        assert damerau_levenshtein_distance("ab", "ba") == 1
        assert damerau_levenshtein_distance("abc", "acb") == 1
        assert damerau_levenshtein_distance("abcd", "abdc") == 1

    def test_symmetric(self):
        """Damerau-Levenshtein 距离满足对称性。"""
        pairs = [("ab", "ba"), ("abc", "acb"), ("abc", "xyz")]
        for s1, s2 in pairs:
            assert (damerau_levenshtein_distance(s1, s2)
                    == damerau_levenshtein_distance(s2, s1))


# ============================================================================
# Level 3 — 子串搜索
# ============================================================================

SEARCH_REF_TEXT = "abcabcabcdabc"
SEARCH_REF = {
    "abc": [0, 3, 6, 10],
    "abcd": [6],
    "abcabc": [0, 3],
    "xyz": [],
    "a": [0, 3, 6, 10],
    "d": [9],
    "": ValueError,
    "abcabcabcdabc": [0],
}

# 所有搜索实现（统一的 (name, search_fn, search_all_fn) 三元组）
ALL_SEARCH_IMPLS = [
    ("kmp", kmp_search, kmp_search_all),
    ("naive", naive_search, naive_search_all),
    ("rabin_karp", rabin_karp_search, rabin_karp_search_all),
]


class TestSearchFirst:
    """测试首次匹配搜索。"""

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    @pytest.mark.parametrize("pattern,expected_first", [
        ("abc", 0),
        ("abcd", 6),
        ("abcabc", 0),
        ("xyz", -1),
        ("a", 0),
        ("d", 9),
        ("abcabcabcdabc", 0),
    ])
    def test_first_occurrence(
        self, name: str, search_fn, search_all_fn, pattern: str, expected_first: int
    ):
        """所有搜索算法首次匹配结果应一致。"""
        assert search_fn(SEARCH_REF_TEXT, pattern) == expected_first

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_not_found(self, name: str, search_fn, search_all_fn):
        """不存在的模式返回 -1。"""
        assert search_fn("abc", "xyz") == -1
        assert search_fn("", "a") == -1

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_pattern_longer_than_text(self, name: str, search_fn, search_all_fn):
        """模式比文本长时返回 -1。"""
        assert search_fn("abc", "abcd") == -1

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_type_error_on_non_string(self, name: str, search_fn, search_all_fn):
        """非字符串输入抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须"):
            search_fn(123, "abc")
        with pytest.raises(TypeError, match="必须"):
            search_fn("abc", None)

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_empty_pattern_raises_value_error(self, name: str, search_fn, search_all_fn):
        """空模式抛出 ValueError。"""
        with pytest.raises(ValueError, match="不能为空|empty"):
            search_fn("abc", "")


class TestSearchAll:
    """测试所有匹配搜索。"""

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    @pytest.mark.parametrize("pattern,expected_all", [
        ("abc", [0, 3, 6, 10]),
        ("abcd", [6]),
        ("abcabc", [0, 3]),
        ("xyz", []),
        ("a", [0, 3, 6, 10]),
        ("d", [9]),
        ("abcabcabcdabc", [0]),
    ])
    def test_all_occurrences(
        self, name: str, search_fn, search_all_fn, pattern: str, expected_all: list[int]
    ):
        """所有搜索算法全部匹配结果应一致。"""
        assert search_all_fn(SEARCH_REF_TEXT, pattern) == expected_all

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_not_found(self, name: str, search_fn, search_all_fn):
        """不存在的模式返回空列表。"""
        assert search_all_fn("abc", "xyz") == []
        assert search_all_fn("", "a") == []

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_pattern_longer_than_text(self, name: str, search_fn, search_all_fn):
        """模式比文本长时返回空列表。"""
        assert search_all_fn("abc", "abcd") == []

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_type_error_on_non_string(self, name: str, search_fn, search_all_fn):
        """非字符串输入抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须"):
            search_all_fn(123, "abc")
        with pytest.raises(TypeError, match="必须"):
            search_all_fn("abc", None)

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_empty_pattern_raises_value_error(self, name: str, search_fn, search_all_fn):
        """空模式抛出 ValueError。"""
        with pytest.raises(ValueError, match="不能为空|empty"):
            search_all_fn("abc", "")


class TestSearchConsistency:
    """所有搜索算法之间的一致性。"""

    TEXTS = [
        "abcabcabc",
        "aaaaaa",
        "abababab",
        "abcdefgh",
        "",
        "a",
        "你好世界你好",
        "1234567890",
        "abacabadabacaba",
    ]
    PATTERNS = [
        "abc", "a", "ab", "aba", "abcabc",
        "xyz", "你好", "0", "aba",
    ]

    @pytest.mark.parametrize("text", TEXTS)
    @pytest.mark.parametrize("pattern", PATTERNS)
    def test_all_impls_agree_first(self, text: str, pattern: str):
        """所有搜索算法的首次匹配结果应一致。"""
        results = set()
        for _, search_fn, _ in ALL_SEARCH_IMPLS:
            try:
                results.add(search_fn(text, pattern))
            except (ValueError, TypeError):
                continue
        if len(results) > 1:
            # 允许所有返回 -1（未找到）一致
            assert len(results) == 1, (
                f"text={text!r}, pattern={pattern!r}: 搜索算法结果不一致: {results}"
            )

    @pytest.mark.parametrize("text", TEXTS)
    @pytest.mark.parametrize("pattern", PATTERNS)
    def test_all_impls_agree_all(self, text: str, pattern: str):
        """所有搜索算法的全部匹配结果应一致。"""
        results = set()
        for _, _, search_all_fn in ALL_SEARCH_IMPLS:
            try:
                results.add(tuple(search_all_fn(text, pattern)))
            except (ValueError, TypeError):
                continue
        if len(results) > 1:
            assert len(results) == 1, (
                f"text={text!r}, pattern={pattern!r}: 搜索算法结果不一致: {results}"
            )


class TestSearchEdgeCases:
    """搜索边界情况。"""

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_single_char_text(self, name: str, search_fn, search_all_fn):
        """单字符文本搜索。"""
        assert search_fn("a", "a") == 0
        assert search_fn("a", "b") == -1
        assert search_all_fn("a", "a") == [0]
        assert search_all_fn("a", "b") == []

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_overlapping_patterns(self, name: str, search_fn, search_all_fn):
        """重叠模式搜索。"""
        assert search_all_fn("aaaaa", "aa") == [0, 1, 2, 3]
        assert search_all_fn("ababa", "aba") == [0, 2]

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_unicode_text(self, name: str, search_fn, search_all_fn):
        """Unicode 文本搜索。"""
        assert search_fn("你好世界", "世界") == 2
        assert search_fn("你好世界", "你好") == 0
        assert search_fn("你好世界", "再见") == -1
        assert search_all_fn("你好你好你好", "你好") == [0, 2, 4]

    @pytest.mark.parametrize("name,search_fn,search_all_fn", ALL_SEARCH_IMPLS)
    def test_large_text(self, name: str, search_fn, search_all_fn):
        """大文本搜索不崩溃。"""
        text = "a" * 10000 + "b"
        assert search_fn(text, "b") == 10000
        assert search_fn(text, "c") == -1


# ============================================================================
# KMP 专用测试
# ============================================================================

class TestKmpBuildLps:
    """测试 KMP 的 LPS 构建函数（内部函数）。"""

    def test_lps_basic(self):
        """基本模式串的 LPS 表。"""
        # 通过 kmp_search 间接验证（LPS 不影响正确性）
        assert kmp_search("abcabc", "abc") == 0
        assert kmp_search("abcabc", "abcabc") == 0

    def test_lps_repeating_pattern(self):
        """重复模式的 LPS。"""
        assert kmp_search("aaaaa", "aa") == 0
        assert kmp_search_all("aaaaa", "aa") == [0, 1, 2, 3]

    def test_lps_partial_match(self):
        """部分匹配场景。"""
        assert kmp_search("ababcabc", "abc") == 4
        assert kmp_search("abababab", "aba") == 0
        assert kmp_search_all("abababab", "aba") == [0, 2, 4]


# ============================================================================
# 跨级别一致性测试
# ============================================================================

class TestCrossLevelConsistency:
    """跨级别一致性测试。"""

    def test_search_and_hamming_consistency(self):
        """精确匹配时的汉明距离应为 0。"""
        patterns = ["abc", "hello", "test"]
        for p in patterns:
            result = kmp_search(p, p)
            assert result == 0
            assert hamming_distance(p, p) == 0

    def test_edit_distance_and_search(self):
        """编辑距离为 0 等价于精确匹配。"""
        strings = ["abc", "hello", "kitten"]
        for s in strings:
            assert levenshtein_distance(s, s) == 0
            assert exact_match(s, s) is True
            assert kmp_search(s, s) == 0