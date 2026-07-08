"""字符串编码 — Level 1：凯撒密码 (O(n) 时间, O(n) 空间)。

最经典的替换密码之一，通过将字母在字母表上移动固定位数来实现加密。
支持英文大小写字母的编码和解码，非字母字符保持不变。

凯撒密码是一种仿射密码的特例（a=1），在现代密码学中仅用于教学目的。
"""

# 标准英文字母表
UPPER_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
LOWER_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
ALPHABET_SIZE = 26


def _caesar_transform(s: str, shift: int) -> str:
    """凯撒变换核心函数（编码和解码共用）。

    Args:
        s: 输入字符串。
        shift: 偏移量（正数向右移位，负数向左移位）。

    Returns:
        变换后的字符串。
    """
    result: list[str] = []

    for ch in s:
        if "A" <= ch <= "Z":
            idx = (ord(ch) - ord("A") + shift) % ALPHABET_SIZE
            result.append(chr(idx + ord("A")))
        elif "a" <= ch <= "z":
            idx = (ord(ch) - ord("a") + shift) % ALPHABET_SIZE
            result.append(chr(idx + ord("a")))
        else:
            result.append(ch)

    return "".join(result)


def caesar_encode(s: str, shift: int = 3) -> str:
    """使用凯撒密码编码字符串。

    将每个英文字母在字母表上向右移动 shift 位。
    非字母字符保持不变。

    Args:
        s: 要编码的字符串。
        shift: 偏移量（默认 3），必须为整数且在 [-25, 25] 范围内。

    Returns:
        编码后的字符串。

    Raises:
        TypeError: 如果 s 不是字符串或 shift 不是整数。
        ValueError: 如果 shift 不在 [-25, 25] 范围内。

    Examples:
        >>> caesar_encode("HELLO")
        'KHOOR'
        >>> caesar_encode("hello", shift=3)
        'khoor'
        >>> caesar_encode("abc", shift=1)
        'bcd'
        >>> caesar_encode("xyz", shift=3)
        'abc'
        >>> caesar_encode("Hello, World!", shift=5)
        'Mjqqt, Btwqi!'
        >>> caesar_encode("", shift=5)
        ''
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    if not isinstance(shift, int):
        raise TypeError("偏移量必须是整数")

    if shift < -25 or shift > 25:
        raise ValueError("偏移量必须在 [-25, 25] 范围内")

    if not s:
        return ""

    return _caesar_transform(s, shift)


def caesar_decode(s: str, shift: int = 3) -> str:
    """解码凯撒密码编码的字符串。

    将每个英文字母在字母表上向左移动 shift 位（编码的逆操作）。

    Args:
        s: 要解码的字符串。
        shift: 编码时使用的偏移量（默认 3），必须为整数且在 [-25, 25] 范围内。

    Returns:
        解码后的字符串。

    Raises:
        TypeError: 如果 s 不是字符串或 shift 不是整数。
        ValueError: 如果 shift 不在 [-25, 25] 范围内。

    Examples:
        >>> caesar_decode("KHOOR")
        'HELLO'
        >>> caesar_decode("khoor", shift=3)
        'hello'
        >>> caesar_decode("bcd", shift=1)
        'abc'
        >>> caesar_decode("abc", shift=3)
        'xyz'
        >>> caesar_decode("Mjqqt, Btwqi!", shift=5)
        'Hello, World!'
        >>> caesar_decode("", shift=5)
        ''
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    if not isinstance(shift, int):
        raise TypeError("偏移量必须是整数")

    if shift < -25 or shift > 25:
        raise ValueError("偏移量必须在 [-25, 25] 范围内")

    if not s:
        return ""

    # 解码 = 编码的逆操作，即向左移动 shift 位
    return _caesar_transform(s, -shift)