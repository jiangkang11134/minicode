"""字符串反转 — Level 3：递归法 (O(n) 时间, O(n) 栈空间)。

使用递归思想反转字符串：每次取首字符放到末尾，递归处理剩余部分。
纯函数式风格，无副作用。适合理解递归思维的示范场景。

注意：Python 默认递归深度约 1000，超长字符串会触发 RecursionError。
"""


def reverse_recursive(s: str) -> str:
    """反转字符串（递归法）。

    递归公式：
        reverse_recursive(s) = reverse_recursive(s[1:]) + s[0]
        基准条件: len(s) <= 1 时返回 s 自身

    Args:
        s: 要反转的字符串。

    Returns:
        反转后的字符串。

    Raises:
        TypeError: 如果 s 不是字符串。
        RecursionError: 如果字符串过长（Python 默认递归深度限制 ~1000）。

    Examples:
        >>> reverse_recursive("hello")
        'olleh'
        >>> reverse_recursive("Python")
        'nohtyP'
        >>> reverse_recursive("")
        ''
        >>> reverse_recursive("a")
        'a'
        >>> reverse_recursive("ab")
        'ba'
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    # 基准条件：空字符串或单字符
    if len(s) <= 1:
        return s

    # 递归：反转除首字符外的子串，首字符放到末尾
    return reverse_recursive(s[1:]) + s[0]