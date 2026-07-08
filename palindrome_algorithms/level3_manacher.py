"""回文检测 — Level 3：Manacher 算法 (O(n) 时间, O(n) 空间)。

Manacher 算法能在 O(n) 时间内找到最长回文子串，利用回文的对称性
避免重复计算。核心思想是用一个中心扩展数组记录已探测的回文半径，
遇到在已知回文内的字符时直接复用对称位置的信息。

适用场景：
    1. 求最长回文子串 (longest_palindromic_substring)
    2. 求回文子串总数 (count_palindromic_substrings)
    3. 字符串预处理 + 快速回文查询
"""

from functools import lru_cache


def _manacher_preprocess(s: str) -> list[int]:
    """Manacher 算法核心：计算每个位置的回文半径。

    通过插入分隔符 '#' 将奇数/偶数长度回文统一处理。
    返回的半径数组半径值对应原始字符串中的回文长度。

    Args:
        s: 原始字符串。

    Returns:
        radius 数组，其中 radius[i] 表示以 i 为中心的回文半径（包含中心）。
    """
    # 插入分隔符: "abc" -> "#a#b#c#"
    # 这样所有回文子串都是奇数长度，统一处理
    transformed = ['#'] * (2 * len(s) + 1)
    for i, ch in enumerate(s):
        transformed[2 * i + 1] = ch
    t = ''.join(transformed)

    n = len(t)
    radius = [0] * n

    center = 0
    right_boundary = 0  # 当前已知最右回文的右边界

    for i in range(n):
        # 镜像位置
        mirror = 2 * center - i

        # i 在已知回文范围内 → 复用对称位置的信息
        if i < right_boundary:
            radius[i] = min(radius[mirror], right_boundary - i)

        # 中心扩展
        while (i - radius[i] - 1 >= 0 and
               i + radius[i] + 1 < n and
               t[i - radius[i] - 1] == t[i + radius[i] + 1]):
            radius[i] += 1

        # 更新最右回文边界
        if i + radius[i] > right_boundary:
            center = i
            right_boundary = i + radius[i]

    return radius


def longest_palindromic_substring(s: str) -> str:
    """找出字符串中最长的回文子串（Manacher O(n) 算法）。

    思路：
        1. 用 Manacher 算法计算每个位置的回文半径
        2. 找到最大半径对应的中心位置
        3. 将中心位置映射回原始字符串并提取子串

    Args:
        s: 输入字符串。

    Returns:
        最长回文子串。如果有多个等长的，返回第一个。

    Raises:
        TypeError: 如果 s 不是字符串。

    Examples:
        >>> longest_palindromic_substring("babad")
        'bab'
        >>> longest_palindromic_substring("cbbd")
        'bb'
        >>> longest_palindromic_substring("a")
        'a'
        >>> longest_palindromic_substring("")
        ''
        >>> longest_palindromic_substring("racecar")
        'racecar'
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    if not s:
        return ""

    radius = _manacher_preprocess(s)

    # 找到最大半径
    max_radius = 0
    center_idx = 0
    for i, r in enumerate(radius):
        if r > max_radius:
            max_radius = r
            center_idx = i

    # 映射回原始字符串的起止位置
    # 在变换串中，字符在原串中的索引为 (i - 1) // 2
    # 半径对应原串中的回文长度为 radius
    start = (center_idx - max_radius) // 2
    end = start + max_radius  # 不包含 end

    return s[start:end]


def count_palindromic_substrings(s: str) -> int:
    """计算字符串中回文子串的总数（Manacher O(n) 算法）。

    思路：
        1. 用 Manacher 算法计算每个位置的回文半径
        2. 每个位置的半径 radius[i] 表示有 radius[i] 个回文子串以此为中心
        3. 求和得到总数

    Args:
        s: 输入字符串。

    Returns:
        回文子串的总数。

    Raises:
        TypeError: 如果 s 不是字符串。

    Examples:
        >>> count_palindromic_substrings("abc")
        3
        >>> count_palindromic_substrings("aaa")
        6
        >>> count_palindromic_substrings("")
        0
        >>> count_palindromic_substrings("a")
        1
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    if not s:
        return 0

    radius = _manacher_preprocess(s)
    # 每个 radius[i] 表示以 i 为中心的回文半径
    # 在变换串中，每个位置对应一个回文中心
    # 半径对 2 向上取整就是该中心贡献的回文子串数
    total = 0
    for r in radius:
        total += (r + 1) // 2

    return total


_MANACHER_CACHE: dict[str, list[int]] = {}


def _get_manacher_radius(s: str) -> list[int]:
    """获取字符串的 Manacher 半径数组（带缓存）。"""
    if s not in _MANACHER_CACHE:
        _MANACHER_CACHE[s] = _manacher_preprocess(s)
    return _MANACHER_CACHE[s]


def is_palindromic_substring(s: str, i: int, j: int) -> bool:
    """快速判断 s[i:j+1] 是否是回文子串。

    使用 Manacher 算法的预处理结果，在 O(1) 时间内判断任意子串是否为回文。
    适合需要频繁查询的场景。

    Args:
        s: 原始字符串。
        i: 子串起始索引（包含）。
        j: 子串结束索引（包含）。

    Returns:
        如果 s[i:j+1] 是回文返回 True，否则 False。

    Raises:
        IndexError: 如果索引越界。
        TypeError: 如果 s 不是字符串。

    Examples:
        >>> is_palindromic_substring("racecar", 0, 6)
        True
        >>> is_palindromic_substring("racecar", 1, 5)
        True
        >>> is_palindromic_substring("racecar", 0, 3)
        False
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")
    if i < 0 or j >= len(s):
        raise IndexError(f"索引越界: i={i}, j={j}, len={len(s)}")
    if i > j:
        return False

    # 变换串中的中心位置
    center = i + j + 1
    # 回文长度
    length = j - i + 1
    # 获取半径数组（带缓存）
    radius = _get_manacher_radius(s)

    return radius[center] >= length