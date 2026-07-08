"""Tests for fibonacci_algorithms package — correctness and performance."""

import sys

import pytest

# Python 3.10.11+ 默认限制整数转字符串最大 4300 位
# 斐波那契大数测试需要提高上限
sys.set_int_max_str_digits(10_000_000)
from fibonacci_algorithms import (
    fibonacci_iterative,
    fibonacci_fast_doubling,
    fibonacci_matrix,
    fibonacci_fast_doubling_iterative,
)

# 参考值: 前 20 个斐波那契数
FIB_REF = [0, 1, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89, 144, 233, 377, 610, 987, 1597, 2584, 4181]

ALL_IMPLS = [
    ("iterative", fibonacci_iterative),
    ("fast_doubling", fibonacci_fast_doubling),
    ("matrix", fibonacci_matrix),
    ("fast_doubling_iterative", fibonacci_fast_doubling_iterative),
]


class TestFibonacciCorrectness:
    """所有实现必须匹配参考值。"""

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_small_values(self, name: str, impl):
        """测试前 20 个值。"""
        for n, expected in enumerate(FIB_REF):
            assert impl(n) == expected, f"{name}({n}) = {impl(n)}, expected {expected}"

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_large_values(self, name: str, impl):
        """测试大索引值，确保递归深度/性能不崩溃。"""
        # F(100) = 354224848179261915075
        fib_100 = 354224848179261915075
        assert impl(100) == fib_100, f"{name}(100) = {impl(100)}, expected {fib_100}"

        # F(200) — 大数验证
        fib_200 = 280571172992510140037611932413038677189525
        assert impl(200) == fib_200, f"{name}(200) = {impl(200)}, expected {fib_200}"

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_edge_cases(self, name: str, impl):
        """边界值测试。"""
        assert impl(0) == 0
        assert impl(1) == 1
        assert impl(2) == 1

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_negative_rejected(self, name: str, impl):
        """负数必须抛出 ValueError。"""
        with pytest.raises(ValueError, match="non-negative|negative"):
            impl(-1)

    @pytest.mark.parametrize("name,impl", ALL_IMPLS)
    def test_consistency(self, name: str, impl):
        """所有实现结果一致。"""
        for n in [0, 1, 2, 5, 10, 20, 50, 100]:
            ref = fibonacci_iterative(n)
            assert impl(n) == ref, f"{name}({n}) = {impl(n)}, expected {ref}"


class TestFibonacciPerformance:
    """性能基准: 确保高效实现不比 O(n) 慢。"""

    def test_iterative_linear(self):
        """迭代法 O(n)，n=100_000 应在合理时间内。"""
        result = fibonacci_iterative(100_000)
        # 只验证结果的位数，不验证具体值
        assert len(str(result)) > 10000

    def test_fast_doubling_log(self):
        """快速倍增法 O(log n)，n=10_000_000 应秒级内完成。"""
        result = fibonacci_fast_doubling(10_000_000)
        assert len(str(result)) > 100_000

    def test_matrix_log(self):
        """矩阵快速幂 O(log n)，n=10_000_000。"""
        result = fibonacci_matrix(10_000_000)
        assert len(str(result)) > 100_000

    def test_fast_doubling_iterative_log(self):
        """快速倍增迭代版 O(log n)，n=10_000_000。"""
        result = fibonacci_fast_doubling_iterative(10_000_000)
        assert len(str(result)) > 100_000