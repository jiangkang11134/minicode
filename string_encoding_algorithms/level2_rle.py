"""字符串编码 — Level 2：游程编码 (O(n) 时间, O(n) 空间)。

游程编码（Run-Length Encoding, RLE）是一种简单的无损数据压缩算法。
将连续重复的字符替换为该字符及其重复次数，适合压缩含有大量重复数据的场景。

例如: "AAABBBCCC" → "A3B3C3"

对于无重复或重复很少的数据，编码后可能反而变大（需要处理单字符情况）。
"""


def rle_encode(s: str) -> str:
    """对字符串进行游程编码压缩。

    将连续的重复字符编码为"字符+次数"的形式。
    单字符（连续出现 1 次）编码为字符本身，次数 1 省略。

    Args:
        s: 要压缩的字符串。

    Returns:
        压缩后的字符串。

    Raises:
        TypeError: 如果 s 不是字符串。

    Examples:
        >>> rle_encode("AAABBBCCC")
        'A3B3C3'
        >>> rle_encode("ABC")
        'ABC'
        >>> rle_encode("AABBBCCCC")
        'A2B3C4'
        >>> rle_encode("")
        ''
        >>> rle_encode("A")
        'A'
        >>> rle_encode("aabbbaa")
        'a2b3a2'
        >>> rle_encode("111222333")
        '132333'
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    if not s:
        return ""

    result: list[str] = []
    count = 1

    for i in range(1, len(s)):
        if s[i] == s[i - 1]:
            count += 1
        else:
            result.append(s[i - 1])
            if count > 1:
                result.append(str(count))
            count = 1

    # 处理最后一组
    result.append(s[-1])
    if count > 1:
        result.append(str(count))

    return "".join(result)


def rle_decode(s: str) -> str:
    """对游程编码的字符串进行解码还原。

    将"字符+次数"格式解码为原始字符串。
    遇到的数字表示前一个字符的重复次数（不含 1，单字符不计数）。

    注意：这要求原始字符串中不包含数字字符，或数字字符本身也被编码。
    如果数字出现在字符串开头，行为未定义。

    Args:
        s: 要解压的字符串（游程编码格式）。

    Returns:
        还原后的字符串。

    Raises:
        TypeError: 如果 s 不是字符串。
        ValueError: 如果输入格式无效。

    Examples:
        >>> rle_decode("A3B3C3")
        'AAABBBCCC'
        >>> rle_decode("ABC")
        'ABC'
        >>> rle_decode("A2B3C4")
        'AABBBCCCC'
        >>> rle_decode("")
        ''
        >>> rle_decode("A")
        'A'
        >>> rle_decode("a2b3a2")
        'aabbbaa'
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    if not s:
        return ""

    result: list[str] = []
    i = 0
    n = len(s)

    while i < n:
        ch = s[i]
        i += 1

        # 收集后续的数字作为重复次数
        count_str: list[str] = []
        while i < n and s[i].isdigit():
            count_str.append(s[i])
            i += 1

        if count_str:
            count = int("".join(count_str))
            if count < 2:
                raise ValueError(f"无效的编码格式：次数 {count} 应 >= 2")
            result.append(ch * count)
        else:
            result.append(ch)

    return "".join(result)