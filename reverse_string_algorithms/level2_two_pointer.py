"""字符串反转 — Level 2：双指针列表交换法 (O(n) 时间, O(1) 额外空间)。

将字符串转为列表，使用左右双指针原地交换字符。
比切片法更节省内存（不创建新字符串的副本进行反转），
适合大字符串或内存受限场景。
"""


def reverse_two_pointer(s: str) -> str:
    """反转字符串（双指针列表交换法）。

    思路：字符串 → 列表 → 双指针从两端向中间交换字符 → join 回字符串。
    只在列表上原地操作，不创建额外的反转副本。

    参数校验已在顶层完成，若需类型检查请自定义包装。

    Args:
        s: 要反转的字符串。

    Returns:
        反转后的字符串。

    Raises:
        TypeError: 如果 s 不是字符串。

    Examples:
        >>> reverse_two_pointer("hello")
        'olleh'
        >>> reverse_two_pointer("Python")
        'nohtyP'
        >>> reverse_two_pointer("")
        ''
        >>> reverse_two_pointer("a")
        'a'
        >>> reverse_two_pointer("12345")
        '54321'
        >>> reverse_two_pointer("ab")
        'ba'
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    n = len(s)
    if n <= 1:
        return s

    chars = list(s)
    left, right = 0, n - 1

    while left < right:
        chars[left], chars[right] = chars[right], chars[left]
        left += 1
        right -= 1

    return "".join(chars)