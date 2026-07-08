"""斐波那契数列 — 矩阵快速幂 + 缓存 (O(log n))."""

import functools

# 斐波那契矩阵: [[1, 1], [1, 0]]
# F(n) = [[1,1],[1,0]]^(n-1) 的 [0][0] 元素
# (当 n=0 时返回 0)


def _mat_mul(a: tuple, b: tuple) -> tuple:
    """2x2 矩阵乘法，返回扁平元组 (a,b,c,d)。"""
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
    )


def _mat_pow(mat: tuple, exp: int) -> tuple:
    """2x2 矩阵快速幂 (O(log exp))。"""
    # 单位矩阵
    result = (1, 0, 0, 1)

    base = mat
    while exp > 0:
        if exp & 1:
            result = _mat_mul(result, base)
        base = _mat_mul(base, base)
        exp >>= 1

    return result


_FIB_MATRIX = (1, 1, 1, 0)


@functools.lru_cache(maxsize=256)
def fibonacci_matrix(n: int) -> int:
    """计算第 n 个斐波那契数（矩阵快速幂 + LRU 缓存）。

    使用 [[1,1],[1,0]]^n 的矩阵快速幂计算，O(log n) 时间。
    lru_cache 缓存最近 256 次调用的结果。

    Args:
        n: 非负整数索引。

    Returns:
        第 n 个斐波那契数。

    Raises:
        ValueError: 如果 n 为负数。
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if n == 0:
        return 0
    if n == 1:
        return 1

    # F(n) = (M^(n-1))[0][0]
    result_mat = _mat_pow(_FIB_MATRIX, n - 1)
    return result_mat[0]


def fibonacci_fast_doubling_iterative(n: int) -> int:
    """计算第 n 个斐波那契数（快速倍增迭代版，避免递归栈）。

    与 level2 的函数等价但使用迭代实现，作为性能对比基准。

    Args:
        n: 非负整数索引。

    Returns:
        第 n 个斐波那契数。

    Raises:
        ValueError: 如果 n 为负数。
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if n == 0:
        return 0
    if n == 1:
        return 1

    # 使用二进制展开的迭代快速倍增
    # 从高位到低位扫描 n 的二进制位
    a, b = 0, 1  # F(0), F(1)
    bit = 1 << (n.bit_length() - 1)

    while bit:
        # 从 (F(k), F(k+1)) 计算 (F(2k), F(2k+1))
        c = a * (2 * b - a)
        d = a * a + b * b

        if n & bit:
            # 下一项: (F(2k+1), F(2k+2))
            a, b = d, c + d
        else:
            # 下一项: (F(2k), F(2k+1))
            a, b = c, d

        bit >>= 1

    return a