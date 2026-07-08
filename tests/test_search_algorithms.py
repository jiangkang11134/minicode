"""Tests for search_algorithms package — correctness, edge cases, and performance."""

import pytest

from search_algorithms import (
    linear_search,
    linear_search_all,
    binary_search,
    binary_search_recursive,
    binary_search_first,
    binary_search_last,
    jump_search,
    jump_search_all,
)

# 参考值
UNSORTED = [5, 3, 8, 1, 9, 3, 7, 2, 8, 4]
SORTED = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
SORTED_DUP = [1, 2, 2, 2, 3, 4, 4, 5, 6, 6, 6, 7]
EMPTY: list[int] = []
SINGLE = [42]

# 基础查找实现（无序兼容）
BASE_SEARCH = [("linear", linear_search)]
# 有序查找实现（要求输入有序）
ORDERED_SEARCH = [
    ("binary", binary_search),
    ("binary_recursive", binary_search_recursive),
    ("jump", jump_search),
]


class TestLinearSearch:
    """线性查找测试 — O(n)，无序/有序均可。"""

    def test_found(self):
        """能找到目标值。"""
        assert linear_search(UNSORTED, 8) == 2
        assert linear_search(UNSORTED, 1) == 3
        assert linear_search(UNSORTED, 4) == 9

    def test_first_occurrence(self):
        """返回第一次出现的位置。"""
        assert linear_search(UNSORTED, 3) == 1  # 3 出现在索引 1 和 5
        assert linear_search(UNSORTED, 8) == 2  # 8 出现在索引 2 和 8

    def test_not_found(self):
        """找不到时返回 -1。"""
        assert linear_search(UNSORTED, 999) == -1
        assert linear_search(EMPTY, 1) == -1

    def test_single_element(self):
        """单元素列表。"""
        assert linear_search(SINGLE, 42) == 0
        assert linear_search(SINGLE, 0) == -1

    def test_unsorted_input(self):
        """无序列表也能正常工作。"""
        assert linear_search(UNSORTED, 5) == 0
        assert linear_search(UNSORTED, 9) == 4
        assert linear_search(UNSORTED, 10) == -1

    def test_type_error_on_non_list(self):
        """非列表输入抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须|list"):
            linear_search("abc", 1)
        with pytest.raises(TypeError, match="必须|list"):
            linear_search(123, 1)
        with pytest.raises(TypeError, match="必须|list"):
            linear_search(None, 1)

    @pytest.mark.parametrize("target", [0, 1, 10, 999, -1, None, "abc", 3.14])
    def test_various_target_types(self, target):
        """多种目标值类型。"""
        arr = [0, 1, 10, -1, "abc", 3.14, None]
        result = linear_search(arr, target)
        expected = arr.index(target) if target in arr else -1
        assert result == expected, f"target={target!r}"

    def test_reference_values(self):
        """与 Python 内建行为一致。"""
        arr = [5, 3, 8, 1, 9, 3, 7, 2, 8, 4]
        for target in [5, 3, 8, 1, 9, 7, 2, 4, 0, 10]:
            expected = arr.index(target) if target in arr else -1
            assert linear_search(arr, target) == expected


class TestLinearSearchAll:
    """线性查找所有匹配测试。"""

    def test_found_multiple(self):
        """找到所有匹配位置。"""
        assert linear_search_all([1, 2, 1, 3, 1], 1) == [0, 2, 4]
        assert linear_search_all(UNSORTED, 3) == [1, 5]
        assert linear_search_all(UNSORTED, 8) == [2, 8]

    def test_found_single(self):
        """只有一个匹配。"""
        assert linear_search_all(UNSORTED, 5) == [0]
        assert linear_search_all(UNSORTED, 1) == [3]

    def test_not_found(self):
        """无匹配返回空列表。"""
        assert linear_search_all(UNSORTED, 999) == []
        assert linear_search_all(EMPTY, 1) == []

    def test_all_same(self):
        """全部元素相同的列表。"""
        assert linear_search_all([7, 7, 7, 7], 7) == [0, 1, 2, 3]
        assert linear_search_all([7, 7, 7, 7], 8) == []

    def test_type_error_on_non_list(self):
        """非列表输入抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须|list"):
            linear_search_all("abc", 1)

    def test_empty_list(self):
        """空列表返回空列表。"""
        assert linear_search_all([], 1) == []


class TestOrderedSearch:
    """有序查找基础测试（二分查找、递归二分、跳跃查找）。"""

    @pytest.mark.parametrize("name,impl", ORDERED_SEARCH)
    def test_found(self, name: str, impl):
        """能找到目标值。"""
        for i, val in enumerate(SORTED):
            assert impl(SORTED, val) == i, f"{name}({SORTED}, {val}) expected {i}"

    @pytest.mark.parametrize("name,impl", ORDERED_SEARCH)
    def test_not_found(self, name: str, impl):
        """找不到返回 -1。"""
        assert impl(SORTED, 0) == -1
        assert impl(SORTED, 11) == -1
        assert impl(SORTED, 100) == -1

    @pytest.mark.parametrize("name,impl", ORDERED_SEARCH)
    def test_edge_cases(self, name: str, impl):
        """边界值测试。"""
        # 空列表
        assert impl([], 1) == -1
        # 单元素列表
        assert impl([5], 5) == 0
        assert impl([5], 3) == -1
        # 双元素
        assert impl([1, 2], 1) == 0
        assert impl([1, 2], 2) == 1

    @pytest.mark.parametrize("name,impl", ORDERED_SEARCH)
    def test_type_error_on_non_list(self, name: str, impl):
        """非列表输入抛出 TypeError。"""
        with pytest.raises(TypeError, match="必须|list"):
            impl("abc", 1)

    @pytest.mark.parametrize("name,impl", ORDERED_SEARCH)
    def test_unsorted_rejected(self, name: str, impl):
        """无序列表抛出 ValueError。"""
        with pytest.raises(ValueError, match="有序|sorted"):
            impl(UNSORTED, 1)

    @pytest.mark.parametrize("name,impl", ORDERED_SEARCH)
    def test_first_and_last(self, name: str, impl):
        """首尾元素。"""
        assert impl(SORTED, SORTED[0]) == 0  # 第一个
        assert impl(SORTED, SORTED[-1]) == len(SORTED) - 1  # 最后一个


class TestBinarySearchFirst:
    """二分查找首次出现测试。"""

    def test_first_with_duplicates(self):
        """多个重复元素时找首次出现。"""
        assert binary_search_first(SORTED_DUP, 2) == 1
        assert binary_search_first(SORTED_DUP, 4) == 5
        assert binary_search_first(SORTED_DUP, 6) == 9

    def test_single_occurrence(self):
        """只有一个匹配时。"""
        assert binary_search_first(SORTED, 5) == 4

    def test_not_found(self):
        """找不到返回 -1。"""
        assert binary_search_first(SORTED_DUP, 0) == -1
        assert binary_search_first(SORTED_DUP, 8) == -1
        assert binary_search_first([], 1) == -1

    def test_all_same(self):
        """全部相同元素。"""
        assert binary_search_first([5, 5, 5, 5, 5], 5) == 0

    def test_type_error(self):
        """类型错误。"""
        with pytest.raises(TypeError, match="必须|list"):
            binary_search_first("abc", 1)

    def test_unsorted_rejected(self):
        """无序列表抛出 ValueError。"""
        with pytest.raises(ValueError, match="有序|sorted"):
            binary_search_first(UNSORTED, 1)


class TestBinarySearchLast:
    """二分查找最后出现测试。"""

    def test_last_with_duplicates(self):
        """多个重复元素时找最后出现。"""
        assert binary_search_last(SORTED_DUP, 2) == 3
        assert binary_search_last(SORTED_DUP, 4) == 6
        assert binary_search_last(SORTED_DUP, 6) == 11

    def test_single_occurrence(self):
        """只有一个匹配时。"""
        assert binary_search_last(SORTED, 5) == 4

    def test_not_found(self):
        """找不到返回 -1。"""
        assert binary_search_last(SORTED_DUP, 0) == -1
        assert binary_search_last(SORTED_DUP, 8) == -1
        assert binary_search_last([], 1) == -1

    def test_all_same(self):
        """全部相同元素。"""
        assert binary_search_last([5, 5, 5, 5, 5], 5) == 4

    def test_type_error(self):
        """类型错误。"""
        with pytest.raises(TypeError, match="必须|list"):
            binary_search_last("abc", 1)

    def test_unsorted_rejected(self):
        """无序列表抛出 ValueError。"""
        with pytest.raises(ValueError, match="有序|sorted"):
            binary_search_last(UNSORTED, 1)


class TestJumpSearch:
    """跳跃查找测试 — O(√n)。"""

    def test_found(self):
        """能找到目标值。"""
        assert jump_search(SORTED, 1) == 0
        assert jump_search(SORTED, 5) == 4
        assert jump_search(SORTED, 10) == 9

    def test_not_found(self):
        """找不到返回 -1。"""
        assert jump_search(SORTED, 0) == -1
        assert jump_search(SORTED, 11) == -1
        assert jump_search([], 1) == -1

    def test_single_element(self):
        """单元素列表。"""
        assert jump_search([5], 5) == 0
        assert jump_search([5], 3) == -1

    def test_two_elements(self):
        """双元素列表。"""
        assert jump_search([1, 2], 1) == 0
        assert jump_search([1, 2], 2) == 1

    def test_small_array(self):
        """小数组（n < step = sqrt(n)）。"""
        assert jump_search([1, 2, 3], 1) == 0
        assert jump_search([1, 2, 3], 2) == 1
        assert jump_search([1, 2, 3], 3) == 2

    def test_large_array(self):
        """大数组测试。"""
        large = list(range(0, 10000, 2))  # 0, 2, 4, ..., 9998
        assert jump_search(large, 0) == 0
        assert jump_search(large, 5000) == 2500
        assert jump_search(large, 9998) == 4999
        assert jump_search(large, 9999) == -1

    def test_unsorted_rejected(self):
        """无序列表抛出 ValueError。"""
        with pytest.raises(ValueError, match="有序|sorted"):
            jump_search(UNSORTED, 1)

    def test_type_error(self):
        """类型错误。"""
        with pytest.raises(TypeError, match="必须|list"):
            jump_search("abc", 1)


class TestJumpSearchAll:
    """跳跃查找所有匹配测试。"""

    def test_with_duplicates(self):
        """查找所有匹配。"""
        result = jump_search_all(SORTED_DUP, 2)
        assert result == [1, 2, 3], f"Expected [1, 2, 3], got {result}"

        result = jump_search_all(SORTED_DUP, 4)
        assert result == [5, 6], f"Expected [5, 6], got {result}"

    def test_single_match(self):
        """只有一个匹配。"""
        assert jump_search_all(SORTED_DUP, 3) == [4]
        assert jump_search_all(SORTED_DUP, 7) == [12]

    def test_no_match(self):
        """无匹配返回空列表。"""
        assert jump_search_all(SORTED_DUP, 0) == []
        assert jump_search_all([], 1) == []

    def test_all_same(self):
        """全部相同元素。"""
        assert jump_search_all([5, 5, 5, 5, 5], 5) == [0, 1, 2, 3, 4]

    def test_type_error(self):
        """类型错误。"""
        with pytest.raises(TypeError, match="必须|list"):
            jump_search_all("abc", 1)


class TestConsistency:
    """所有实现的一致性测试。"""

    CONSISTENT_SEARCH = [
        ("linear", linear_search),
        ("binary", binary_search),
        ("binary_recursive", binary_search_recursive),
        ("jump", jump_search),
    ]

    @pytest.mark.parametrize("name,impl", CONSISTENT_SEARCH)
    def test_sorted_consistency(self, name: str, impl):
        """有序列表上所有算法结果一致。"""
        for target in [1, 3, 5, 7, 10, 0, 11]:
            ref = linear_search(SORTED, target)
            assert impl(SORTED, target) == ref, (
                f"{name}({SORTED}, {target}) = {impl(SORTED, target)}, "
                f"expected {ref}"
            )

    def test_binary_first_last_consistency(self):
        """binary_search_first ≤ binary_search_last。"""
        unique_vals = set(SORTED_DUP)
        for val in unique_vals:
            first = binary_search_first(SORTED_DUP, val)
            last = binary_search_last(SORTED_DUP, val)
            assert first <= last, f"first={first} > last={last} for val={val}"
            for i in range(first, last + 1):
                assert SORTED_DUP[i] == val, (
                    f"SORTED_DUP[{i}] = {SORTED_DUP[i]}, expected {val}"
                )

    def test_jump_search_matches_binary(self):
        """跳跃查找与二分查找结果一致。"""
        for target in [1, 2, 3, 6, 7, 0, 8]:
            b = binary_search(SORTED_DUP, target)
            j = jump_search(SORTED_DUP, target)
            assert b == j, f"binary={b} != jump={j} for target={target}"

    @pytest.mark.parametrize("val", [1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
    def test_jump_vs_linear_sorted(self, val: int):
        """跳跃查找与线性查找结果一致（有序列表）。"""
        s = SORTED
        l = linear_search(s, val)
        j = jump_search(s, val)
        assert l == j, f"linear={l} != jump={j} for val={val}"

    def test_linear_all_contains_binary_result(self):
        """linear_search_all 的结果包含 binary_search_first 的位置。"""
        for val in set(SORTED_DUP):
            all_pos = linear_search_all(SORTED_DUP, val)
            first = binary_search_first(SORTED_DUP, val)
            assert first in all_pos, (
                f"binary_search_first({val})={first} not in {all_pos}"
            )


class TestPerformance:
    """性能基准测试。"""

    LARGE_SORTED = list(range(0, 100_000))

    def test_linear_search_linear_ok(self):
        """线性查找 O(n) 能在合理时间内完成。"""
        result = linear_search(self.LARGE_SORTED, 99_999)
        assert result == 99_999

    def test_binary_search_log(self):
        """二分查找 O(log n) 应极快。"""
        result = binary_search(self.LARGE_SORTED, 99_999)
        assert result == 99_999

    def test_binary_recursive_log(self):
        """递归二分查找 O(log n)。"""
        result = binary_search_recursive(self.LARGE_SORTED, 0)
        assert result == 0

    def test_jump_search_sqrt(self):
        """跳跃查找 O(√n)。"""
        result = jump_search(self.LARGE_SORTED, 99_999)
        assert result == 99_999

    def test_not_found_in_large(self):
        """大列表中找不到目标。"""
        assert binary_search(self.LARGE_SORTED, -1) == -1
        assert jump_search(self.LARGE_SORTED, 100_000) == -1

    def test_binary_first_last_in_large_with_dup(self):
        """大列表中查找首尾出现。"""
        large_dup = [1] * 50_000 + [2] * 50_000
        assert binary_search_first(large_dup, 1) == 0
        assert binary_search_last(large_dup, 1) == 49_999
        assert binary_search_first(large_dup, 2) == 50_000
        assert binary_search_last(large_dup, 2) == 99_999