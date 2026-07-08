"""回文检测 — 基础实现：反转比较法 (O(n) 时间, O(n) 空间)。"""

import unicodedata


def is_palindrome(s: str, ignore_case: bool = True,
                  ignore_whitespace: bool = True,
                  ignore_punctuation: bool = False,
                  normalize_unicode: bool = False) -> bool:
    """判断字符串是否是回文（反转比较法）。

    思路：清洗字符串 → 反转 → 比较原串和反转串是否相等。

    Args:
        s: 要检查的字符串。
        ignore_case: 是否忽略大小写，默认 True。
        ignore_whitespace: 是否忽略空格，默认 True。
        ignore_punctuation: 是否忽略标点符号，默认 False。
        normalize_unicode: 是否进行 Unicode 规范化（NFKC），默认 False。
            对含有组合字符的 Unicode 字符串启用可提高准确性。

    Returns:
        如果 s 是回文返回 True，否则返回 False。

    Raises:
        TypeError: 如果 s 不是字符串。

    Examples:
        >>> is_palindrome("A man a plan a canal Panama")
        True
        >>> is_palindrome("racecar")
        True
        >>> is_palindrome("hello")
        False
        >>> is_palindrome("Was it a car or a cat I saw")
        True
        >>> is_palindrome("", ignore_case=True)
        True
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    # 空字符串或单字符是回文
    if len(s) <= 1:
        return True

    # Unicode 规范化（将组合字符标准化）
    cleaned = unicodedata.normalize('NFKC', s) if normalize_unicode else s

    # 按选项清洗字符串
    if ignore_case:
        cleaned = cleaned.lower()

    if ignore_whitespace:
        cleaned = "".join(cleaned.split())

    if ignore_punctuation:
        # 移除标点符号（保留字母、数字、空格）
        import string
        cleaned = "".join(ch for ch in cleaned if ch not in string.punctuation)

    # 反转比较
    return cleaned == cleaned[::-1]