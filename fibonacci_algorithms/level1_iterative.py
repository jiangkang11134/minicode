"""斐波那契数列 — 基础迭代实现 (O(n))."""


def fibonacci_iterative(n: int) -> int:
    """计算第 n 个斐波那契数（迭代法，O(n) 时间，O(1) 空间）。

    Args:
        n: 非负整数索引。

    Returns:
        第 n 个斐波那契数。

    Raises:
        ValueError: 如果 n 为负数。
    """
    if n < 0:
        raise ValueError("n must be non-negative")
    if n <= 1:
        return n

    a, b = 0, 1
    for _ in range(2, n + 1):
        a, b = b, a + b
    return b