"""斐波那契数列 — 快速倍增法 (O(log n))."""


def _fast_double(k: int):
    """返回 (F(k), F(k+1)) 使用快速倍增恒等式。

    使用递归进行二分，每次调用将问题规模减半。
    恒等式:
        F(2k)   = F(k) * (2*F(k+1) - F(k))
        F(2k+1) = F(k+1)^2 + F(k)^2
    """
    if k == 0:
        return (0, 1)

    a, b = _fast_double(k >> 1)
    # F(2k) = F(k) * (2*F(k+1) - F(k))
    c = a * (2 * b - a)
    # F(2k+1) = F(k+1)^2 + F(k)^2
    d = a * a + b * b

    if k & 1:
        return (d, c + d)
    else:
        return (c, d)


def fibonacci_fast_doubling(n: int) -> int:
    """计算第 n 个斐波那契数（快速倍增法，O(log n) 时间，O(log n) 栈空间）。

    Args:
        n: 非负整数索引。

    Returns:
        第 n 个斐波那契数。

    Raises:
        ValueError: 如果 n 为负数。
    """
    if n < 0:
        raise ValueError("n must be non-negative")

    return _fast_double(n)[0]