"""字符串比较算法 — 基础级：精确匹配与汉明距离 (O(n))."""


def exact_match(s1: str, s2: str) -> bool:
    """判断两个字符串是否完全相等。

    逐个字符比较两个字符串，长度不等或字符不同时返回 False。

    Args:
        s1: 第一个字符串。
        s2: 第二个字符串。

    Returns:
        完全相等返回 True，否则返回 False。

    Raises:
        TypeError: 如果输入不是字符串。
    """
    if not isinstance(s1, str) or not isinstance(s2, str):
        raise TypeError("s1 和 s2 必须是字符串")
    if len(s1) != len(s2):
        return False
    for a, b in zip(s1, s2):
        if a != b:
            return False
    return True


def hamming_distance(s1: str, s2: str) -> int:
    """计算两个等长字符串之间的汉明距离。

    汉明距离定义为两个等长字符串在相同位置上不同字符的个数。

    Args:
        s1: 第一个字符串。
        s2: 第二个字符串。

    Returns:
        汉明距离（不同字符的位置数）。

    Raises:
        TypeError: 如果输入不是字符串。
        ValueError: 如果两个字符串长度不相等。
    """
    if not isinstance(s1, str) or not isinstance(s2, str):
        raise TypeError("s1 和 s2 必须是字符串")
    if len(s1) != len(s2):
        raise ValueError("两个字符串长度必须相等")
    return sum(1 for a, b in zip(s1, s2) if a != b)


def longest_common_prefix(strs: list[str]) -> str:
    """查找字符串列表中的最长公共前缀。

    从第一个字符开始，逐个位置比较所有字符串在该位置的字符。
    当遇到字符不匹配或某个字符串长度不足时停止。

    时间复杂度: O(S)，S 为所有字符串的总字符数。
    空间复杂度: O(1)（不计输出）。

    Args:
        strs: 字符串列表。

    Returns:
        最长公共前缀字符串（可能为空字符串）。

    Raises:
        TypeError: 如果输入不是列表。
        ValueError: 如果列表为空。
    """
    if not isinstance(strs, list):
        raise TypeError("strs 必须是列表")
    if not strs:
        raise ValueError("字符串列表不能为空")
    if any(not isinstance(s, str) for s in strs):
        raise TypeError("列表中所有元素必须是字符串")

    if not strs[0]:
        return ""

    for i, ch in enumerate(strs[0]):
        for s in strs[1:]:
            if i >= len(s) or s[i] != ch:
                return strs[0][:i]
    return strs[0]