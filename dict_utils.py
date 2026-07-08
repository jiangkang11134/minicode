"""字典查询工具函数。

提供根据值查找键和根据键列表取值的功能。

Functions:
    find_keys_by_value(d, target): 查找字典中所有值为 target 的键
    find_all_values_by_keys(d, keys): 根据键列表从字典中取值
"""

from __future__ import annotations

from typing import Any


def find_keys_by_value(d: dict, target: Any) -> list:
    """返回字典中所有值为 target 的键。

    Args:
        d: 要查询的字典
        target: 要查找的目标值

    Returns:
        包含所有匹配键的列表；若未找到则返回空列表
    """
    return [key for key, value in d.items() if value == target]


def find_all_values_by_keys(d: dict, keys: list) -> list:
    """根据键列表从字典中取出对应的值。

    若某个键不存在于字典中，则跳过该键。

    Args:
        d: 源字典
        keys: 要取值的键列表

    Returns:
        与 keys 中存在的键对应的值列表
    """
    return [d[key] for key in keys if key in d]