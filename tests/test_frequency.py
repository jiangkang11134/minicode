"""Tests for frequency.py - Top K frequency statistics."""

import pytest
from frequency import top_k_frequent, FrequencyCounter, DEFAULT_TOP_K, MAX_TOP_K


class TestTopKFrequent:
    """Tests for top_k_frequent function."""

    def test_basic(self):
        """基本测试：返回频率最高的 k 个元素。"""
        assert top_k_frequent([1, 1, 1, 2, 2, 3], 2) == [1, 2]

    def test_strings(self):
        """字符串类型测试。"""
        assert top_k_frequent(['a', 'b', 'a', 'c', 'a', 'b'], 2) == ['a', 'b']

    def test_single_element(self):
        """单个元素测试。"""
        assert top_k_frequent([5], 1) == [5]
        assert top_k_frequent([7, 7, 7, 7], 1) == [7]

    def test_all_unique(self):
        """所有元素频率相同，返回前 k 个。"""
        result = top_k_frequent([1, 2, 3, 4], 4)
        assert len(result) == 4

    def test_empty(self):
        """空数组返回空列表。"""
        assert top_k_frequent([], 2) == []

    def test_default_k(self):
        """不传 k 时使用默认值 DEFAULT_TOP_K。"""
        # 准备超过 DEFAULT_TOP_K 个不同元素来验证默认值
        items = list(range(DEFAULT_TOP_K + 5)) * 2  # 每个元素出现 2 次
        result = top_k_frequent(items)
        assert len(result) == DEFAULT_TOP_K

    def test_k_larger_than_unique(self):
        """k 大于不同元素数时返回所有元素。"""
        result = top_k_frequent([1, 1, 2, 3], 10)
        assert len(result) == 3

    def test_mixed_types(self):
        """混合类型（int 和 bool）测试。"""
        result = top_k_frequent([1, True, 1, False, True, True], 2)
        assert 1 in result or True in result

    def test_type_error_on_non_int_k(self):
        """k 不是整数时抛出 TypeError。"""
        with pytest.raises(TypeError):
            top_k_frequent([1, 2], k="2")

    def test_value_error_on_non_positive_k(self):
        """k <= 0 时抛出 ValueError。"""
        with pytest.raises(ValueError):
            top_k_frequent([1, 2], k=0)
        with pytest.raises(ValueError):
            top_k_frequent([1, 2], k=-1)

    def test_value_error_on_exceed_max_k(self):
        """k > MAX_TOP_K 时抛出 ValueError。"""
        with pytest.raises(ValueError, match=f"不能超过 {MAX_TOP_K}"):
            top_k_frequent([1, 2], k=MAX_TOP_K + 1)

    def test_frequency_order(self):
        """验证按频率降序排列。"""
        result = top_k_frequent([3, 3, 3, 1, 1, 2], 3)
        assert result[0] == 3  # 频率最高


class TestFrequencyCounter:
    """Tests for FrequencyCounter class."""

    def test_add_and_get_top(self):
        """添加元素后能正确获取 top k。"""
        fc = FrequencyCounter()
        fc.add("apple")
        fc.add("banana")
        fc.add("apple")
        assert fc.get_top(2) == ["apple", "banana"]

    def test_add_all(self):
        """批量添加元素。"""
        fc = FrequencyCounter()
        fc.add_all(["a", "b", "a", "c", "a", "b"])
        assert fc.get_top(2) == ["a", "b"]

    def test_empty_counter(self):
        """空计数器返回空列表。"""
        fc = FrequencyCounter()
        assert fc.get_top(1) == []

    def test_total_count(self):
        """total_count 属性返回正确总数。"""
        fc = FrequencyCounter()
        fc.add_all(["a", "b", "a"])
        assert fc.total_count == 3

    def test_unique_count(self):
        """unique_count 属性返回正确不同元素数。"""
        fc = FrequencyCounter()
        fc.add_all(["a", "b", "a"])
        assert fc.unique_count == 2

    def test_clear(self):
        """clear 后重置计数器。"""
        fc = FrequencyCounter()
        fc.add("test")
        fc.clear()
        assert fc.get_top(1) == []
        assert fc.total_count == 0

    def test_get_top_raises_on_non_positive_k(self):
        """get_top 传入无效 k 时抛出 ValueError。"""
        fc = FrequencyCounter()
        fc.add("x")
        with pytest.raises(ValueError):
            fc.get_top(0)

    def test_get_top_single(self):
        """get_top(1) 返回频率最高的单个元素。"""
        fc = FrequencyCounter()
        fc.add_all(["a", "b", "a"])
        assert fc.get_top(1) == ["a"]

    def test_repr(self):
        """__repr__ 输出正确。"""
        fc = FrequencyCounter()
        fc.add("a")
        fc.add("b")
        assert "FrequencyCounter" in repr(fc)