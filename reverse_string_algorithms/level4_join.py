"""字符串反转 — Level 4：join + reversed() 生成器 (O(n) 时间, O(n) 空间)。

使用内置 reversed() 函数配合 str.join() 方法。
reversed() 返回反向迭代器（惰性求值），不创建中间列表。
适合流式处理风格。
"""


def reverse_join(s: str) -> str:
    """反转字符串（join + reversed() 生成器法）。

    思路：reversed(s) 返回反向迭代器，逐字符惰性输出，
    str.join() 将所有字符拼接成新字符串。

    Args:
        s: 要反转的字符串。

    Returns:
        反转后的字符串。

    Raises:
        TypeError: 如果 s 不是字符串。

    Examples:
        >>> reverse_join("hello")
        'olleh'
        >>> reverse_join("Python")
        'nohtyP'
        >>> reverse_join("")
        ''
        >>> reverse_join("a")
        'a'
        >>> reverse_join("12345")
        '54321'
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    return "".join(reversed(s))