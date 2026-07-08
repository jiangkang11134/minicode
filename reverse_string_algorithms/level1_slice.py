"""字符串反转 — Level 1：切片法 (O(n) 时间, O(n) 空间)。

最简洁的 Pythonic 方式，利用切片步进 -1 实现反转。
一行代码完成，可读性最高。
"""


def reverse_slice(s: str) -> str:
    """反转字符串（切片法）。

    利用 Python 切片语法 s[::-1] 一步完成反转。
    原理：start=end=留空（取全部），step=-1（反向步进）。

    Args:
        s: 要反转的字符串。

    Returns:
        反转后的字符串。

    Raises:
        TypeError: 如果 s 不是字符串。

    Examples:
        >>> reverse_slice("hello")
        'olleh'
        >>> reverse_slice("Python")
        'nohtyP'
        >>> reverse_slice("")
        ''
        >>> reverse_slice("a")
        'a'
        >>> reverse_slice("12345")
        '54321'
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    return s[::-1]