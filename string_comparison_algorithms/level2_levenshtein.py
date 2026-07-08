"""字符串比较算法 — 进阶级：Levenshtein 编辑距离 (O(mn))."""


def levenshtein_distance(s1: str, s2: str) -> int:
    """计算两个字符串之间的 Levenshtein 编辑距离。

    编辑距离定义为将一个字符串转换为另一个所需的最少单字符编辑操作次数，
    编辑操作包括：插入、删除、替换三种。

    使用动态规划实现，DP 表大小为 (m+1) × (n+1)。

    时间复杂度: O(mn)，m、n 分别为两个字符串的长度。
    空间复杂度: O(min(m, n))，滚动数组优化。

    Args:
        s1: 第一个字符串。
        s2: 第二个字符串。

    Returns:
        编辑距离（非负整数）。

    Raises:
        TypeError: 如果输入不是字符串。
    """
    if not isinstance(s1, str) or not isinstance(s2, str):
        raise TypeError("s1 和 s2 必须是字符串")

    m, n = len(s1), len(s2)

    # 如果任一为空字符串，距离等于另一字符串的长度
    if m == 0:
        return n
    if n == 0:
        return m

    # 保证 n 是较小的，用于空间优化（用较短的作为列）
    if m < n:
        s1, s2 = s2, s1
        m, n = n, m

    # 滚动数组：只保留前一行
    prev = list(range(n + 1))

    for i, ch1 in enumerate(s1, 1):
        curr = [0] * (n + 1)
        curr[0] = i
        for j, ch2 in enumerate(s2, 1):
            if ch1 == ch2:
                curr[j] = prev[j - 1]
            else:
                curr[j] = 1 + min(
                    prev[j],      # 删除
                    curr[j - 1],  # 插入
                    prev[j - 1],  # 替换
                )
        prev = curr

    return prev[n]


def levenshtein_ratio(s1: str, s2: str) -> float:
    """计算两个字符串的编辑距离相似度比率。

    比率 = (len(s1) + len(s2) - distance) / (len(s1) + len(s2)
    返回 0.0 ~ 1.0 之间的浮点数，1.0 表示完全相同。

    Args:
        s1: 第一个字符串。
        s2: 第二个字符串。

    Returns:
        相似度比率（0.0 ~ 1.0）。

    Raises:
        TypeError: 如果输入不是字符串。
    """
    if not isinstance(s1, str) or not isinstance(s2, str):
        raise TypeError("s1 和 s2 必须是字符串")

    if not s1 and not s2:
        return 1.0

    dist = levenshtein_distance(s1, s2)
    return (len(s1) + len(s2) - dist) / (len(s1) + len(s2))


def damerau_levenshtein_distance(s1: str, s2: str) -> int:
    """计算两个字符串之间的 Damerau-Levenshtein 距离。

    在标准 Levenshtein 操作（插入、删除、替换）基础上增加了相邻字符交换（transposition）。

    时间复杂度: O(mn)，m、n 分别为两个字符串的长度。
    空间复杂度: O(mn)。

    Args:
        s1: 第一个字符串。
        s2: 第二个字符串。

    Returns:
        Damerau-Levenshtein 距离（非负整数）。

    Raises:
        TypeError: 如果输入不是字符串。
    """
    if not isinstance(s1, str) or not isinstance(s2, str):
        raise TypeError("s1 和 s2 必须是字符串")

    m, n = len(s1), len(s2)

    if m == 0:
        return n
    if n == 0:
        return m

    # 完整 DP 表（需要访问左上角相邻）
    dp = [[0] * (n + 1) for _ in range(m + 1)]

    for i in range(m + 1):
        dp[i][0] = i
    for j in range(n + 1):
        dp[0][j] = j

    for i in range(1, m + 1):
        for j in range(1, n + 1):
            cost = 0 if s1[i - 1] == s2[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,      # 删除
                dp[i][j - 1] + 1,      # 插入
                dp[i - 1][j - 1] + cost,  # 替换
            )
            # 相邻字符交换
            if (i > 1 and j > 1
                    and s1[i - 1] == s2[j - 2]
                    and s1[i - 2] == s2[j - 1]):
                dp[i][j] = min(
                    dp[i][j],
                    dp[i - 2][j - 2] + 1,  # 交换算一次操作（固定 cost=1）
                )

    return dp[m][n]