"""测试 dict_utils 模块。"""

from dict_utils import find_all_values_by_keys, find_keys_by_value


class TestFindKeysByValue:
    def test_basic(self):
        """基础功能：找到值为 target 的所有键。"""
        result = find_keys_by_value({"a": 1, "b": 2, "c": 1}, 1)
        assert result == ["a", "c"], f"Expected ['a', 'c'], got {result}"

    def test_no_match(self):
        """target 不存在时返回空列表。"""
        result = find_keys_by_value({"x": 10}, 99)
        assert result == [], f"Expected [], got {result}"

    def test_empty_dict(self):
        """空字典返回空列表。"""
        result = find_keys_by_value({}, 1)
        assert result == [], f"Expected [], got {result}"

    def test_multiple_matches(self):
        """多个键有相同值。"""
        result = find_keys_by_value({"a": 0, "b": 0, "c": 0}, 0)
        assert result == ["a", "b", "c"], f"Expected ['a', 'b', 'c'], got {result}"

    def test_none_value(self):
        """值为 None 的情况。"""
        result = find_keys_by_value({"a": None, "b": 2}, None)
        assert result == ["a"], f"Expected ['a'], got {result}"

    def test_complex_value(self):
        """值为列表/元组的情况。"""
        result = find_keys_by_value({"a": [1, 2], "b": [3, 4]}, [1, 2])
        assert result == ["a"], f"Expected ['a'], got {result}"


class TestFindAllValuesByKeys:
    def test_basic(self):
        """基础功能：根据键列表取值。"""
        result = find_all_values_by_keys({"a": 1, "b": 2, "c": 3}, ["a", "c"])
        assert result == [1, 3], f"Expected [1, 3], got {result}"

    def test_all_keys_exist(self):
        """所有键都存在。"""
        result = find_all_values_by_keys({"x": 10, "y": 20}, ["x", "y"])
        assert result == [10, 20], f"Expected [10, 20], got {result}"

    def test_some_keys_missing(self):
        """部分键不存在时跳过。"""
        result = find_all_values_by_keys({"a": 1}, ["a", "b"])
        assert result == [1], f"Expected [1], got {result}"

    def test_all_keys_missing(self):
        """所有键都不存在返回空列表。"""
        result = find_all_values_by_keys({"a": 1}, ["x", "y"])
        assert result == [], f"Expected [], got {result}"

    def test_empty_keys(self):
        """空键列表返回空列表。"""
        result = find_all_values_by_keys({"a": 1}, [])
        assert result == [], f"Expected [], got {result}"

    def test_empty_dict(self):
        """空字典返回空列表。"""
        result = find_all_values_by_keys({}, ["a"])
        assert result == [], f"Expected [], got {result}"