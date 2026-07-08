"""查找算法实现集 — 三层递进。

| 层级 | 算法 | 时间复杂度 | 空间复杂度 | 输入要求 |
|------|------|-----------|-----------|---------|
| 1    | 线性查找 | O(n) | O(1) | 无序/有序均可 |
| 2    | 二分查找 | O(log n) | O(1)（迭代）/ O(log n)（递归） | 必须有序（升序） |
| 3    | 跳跃查找 | O(√n) | O(1) | 必须有序（升序） |

可用函数:
    linear_search               — 线性查找，O(n)，无序/有序均可
    linear_search_all           — 查找所有匹配位置（线性）
    binary_search               — 二分查找（迭代版），O(log n)
    binary_search_recursive     — 二分查找（递归版），O(log n)
    binary_search_first         — 二分查找首次出现（重复元素），O(log n)
    binary_search_last          — 二分查找最后出现（重复元素），O(log n)
    jump_search                 — 跳跃查找，O(√n)
    jump_search_all             — 跳跃查找所有匹配，O(√n)
"""
from search_algorithms.level1_linear import linear_search, linear_search_all
from search_algorithms.level2_binary import (
    binary_search,
    binary_search_recursive,
    binary_search_first,
    binary_search_last,
)
from search_algorithms.level3_jump import jump_search, jump_search_all

__all__ = [
    "linear_search",
    "linear_search_all",
    "binary_search",
    "binary_search_recursive",
    "binary_search_first",
    "binary_search_last",
    "jump_search",
    "jump_search_all",
]