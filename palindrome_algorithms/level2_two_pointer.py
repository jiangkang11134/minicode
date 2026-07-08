"""回文检测 — Level 2：双指针法 (O(n) 时间, O(1) 空间)。

双指针法比反转比较法更节省内存，无需创建新的反转头字符串。
支持原地检测，适合处理大字符串或内存受限场景。
"""

import unicodedata


def is_palindrome(s: str, ignore_case: bool = True,
                  ignore_whitespace: bool = True,
                  ignore_punctuation: bool = False) -> bool:
    """判断字符串是否是回文（双指针法）。

    思路：左右各一个指针，逐步向中间靠拢，比较对应字符是否相等。
    遇到不符合条件的字符（空格/标点）则跳过，不创建新字符串。

    Args:
        s: 要检查的字符串。
        ignore_case: 是否忽略大小写，默认 True。
        ignore_whitespace: 是否忽略空格，默认 True。
        ignore_punctuation: 是否忽略标点符号，默认 False。

    Returns:
        如果 s 是回文返回 True，否则返回 False。

    Raises:
        TypeError: 如果 s 不是字符串。

    Examples:
        >>> is_palindrome("A man a plan a canal Panama")
        True
        >>> is_palindrome("race a car")
        False
        >>> is_palindrome("Never odd or even")
        True
        >>> is_palindrome("")
        True
        >>> is_palindrome("a")
        True
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    n = len(s)
    if n <= 1:
        return True

    left, right = 0, n - 1

    while left < right:
        # 跳过左边非字符（空格/标点）
        if ignore_whitespace and s[left] == ' ':
            left += 1
            continue
        if ignore_punctuation and s[left] in '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~':
            left += 1
            continue

        # 跳过右边非字符
        if ignore_whitespace and s[right] == ' ':
            right -= 1
            continue
        if ignore_punctuation and s[right] in '!"#$%&\'()*+,-./:;<=>?@[\\]^_`{|}~':
            right -= 1
            continue

        # 比较字符
        left_char = s[left].lower() if ignore_case else s[left]
        right_char = s[right].lower() if ignore_case else s[right]

        if left_char != right_char:
            return False

        left += 1
        right -= 1

    return True


def is_palindrome_number(x: int) -> bool:
    """判断整数是否是回文数（不借助字符串转换，O(log n) 时间, O(1) 空间）。

    思路：反转整数的一半，与另一半比较。
    负数和以 0 结尾的正数（0 除外）不是回文数。

    Args:
        x: 要检查的整数。

    Returns:
        如果 x 是回文数返回 True，否则返回 False。

    Examples:
        >>> is_palindrome_number(121)
        True
        >>> is_palindrome_number(-121)
        False
        >>> is_palindrome_number(10)
        False
        >>> is_palindrome_number(0)
        True
        >>> is_palindrome_number(1221)
        True
    """
    # 负数和以 0 结尾（非 0）的不是回文数
    if x < 0 or (x % 10 == 0 and x != 0):
        return False

    reversed_half = 0
    while x > reversed_half:
        reversed_half = reversed_half * 10 + x % 10
        x //= 10

    # 偶数位: x == reversed_half
    # 奇数位: x == reversed_half // 10
    return x == reversed_half or x == reversed_half // 10


def is_palindrome_sentence(s: str) -> bool:
    """判断一个英文句子是否是回文（忽略大小写、空格和标点）。

    专门用于检测英语句子回文的快捷方式。

    Args:
        s: 要检查的句子。

    Returns:
        如果是回文句子返回 True。

    Examples:
        >>> is_palindrome_sentence("A man, a plan, a canal: Panama")
        True
        >>> is_palindrome_sentence("Madam, I'm Adam")
        True
    """
    return is_palindrome(s, ignore_case=True, ignore_whitespace=True,
                         ignore_punctuation=True)