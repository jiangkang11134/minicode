"""斐波那契数列算法实现集 — 三层优化。

| 层级 | 算法 | 时间复杂度 | 空间复杂度 |
|------|------|-----------|-----------|
| 1    | 迭代法 | O(n)      | O(1)      |
| 2    | 快速倍增法（递归） | O(log n) | O(log n) |
| 3    | 矩阵快速幂 + LRU 缓存 | O(log n) | O(1) (缓存) |

可用函数:
    fibonacci_iterative         — 基础迭代，O(n)
    fibonacci_fast_doubling     — 快速倍增递归，O(log n)
    fibonacci_matrix            — 矩阵快速幂 + LRU 缓存，O(log n)
    fibonacci_fast_doubling_iterative — 快速倍增迭代版，O(log n)
"""
from fibonacci_algorithms.level1_iterative import fibonacci_iterative
from fibonacci_algorithms.level2_fast_doubling import fibonacci_fast_doubling
from fibonacci_algorithms.level3_matrix import fibonacci_matrix, fibonacci_fast_doubling_iterative

__all__ = [
    "fibonacci_iterative",
    "fibonacci_fast_doubling",
    "fibonacci_matrix",
    "fibonacci_fast_doubling_iterative",
]