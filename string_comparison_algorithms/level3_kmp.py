"""字符串比较算法 — 高级：KMP 子串搜索 (O(n+m))."""


def kmp_build_lps(pattern: str) -> list[int]:
    """构建 KMP 算法的 LPS 数组（最长相同前后缀表）。

    LPS[i] 表示 pattern[:i+1] 的最长相等真前缀与真后缀的长度。

    Args:
        pattern: 模式字符串。

    Returns:
        LPS 数组（列表）。

    Raises:
        TypeError: 如果 pattern 不是字符串。
    """
    if not isinstance(pattern, str):
        raise TypeError("pattern 必须是字符串")

    m = len(pattern)
    lps = [0] * m
    length = 0  # 上一个最长前后缀的长度
    i = 1

    while i < m:
        if pattern[i] == pattern[length]:
            length += 1
            lps[i] = length
            i += 1
        else:
            if length != 0:
                length = lps[length - 1]
            else:
                lps[i] = 0
                i += 1

    return lps


def kmp_search(text: str, pattern: str) -> int:
    """使用 KMP 算法在文本中查找模式串首次出现的位置。

    利用已匹配部分的信息，避免回溯文本指针，实现线性时间搜索。

    时间复杂度: O(n + m)，n 为文本长度，m 为模式串长度。
    空间复杂度: O(m)。

    Args:
        text: 文本字符串。
        pattern: 要搜索的模式串。

    Returns:
        模式串首次出现的起始索引，未找到返回 -1。

    Raises:
        TypeError: 如果输入不是字符串。
        ValueError: 如果模式串为空。
    """
    if not isinstance(text, str) or not isinstance(pattern, str):
        raise TypeError("text 和 pattern 必须是字符串")
    if not pattern:
        raise ValueError("pattern 不能为空")

    n, m = len(text), len(pattern)

    if m > n:
        return -1

    lps = kmp_build_lps(pattern)
    i = 0  # text 的索引
    j = 0  # pattern 的索引

    while i < n:
        if text[i] == pattern[j]:
            i += 1
            j += 1
            if j == m:
                return i - j
        else:
            if j != 0:
                j = lps[j - 1]
            else:
                i += 1

    return -1


def kmp_search_all(text: str, pattern: str) -> list[int]:
    """使用 KMP 算法查找模式串在文本中的所有出现位置。

    时间复杂度: O(n + m)，n 为文本长度，m 为模式串长度。
    空间复杂度: O(m)。

    Args:
        text: 文本字符串。
        pattern: 要搜索的模式串。

    Returns:
        所有匹配起始索引的列表（可能为空列表）。

    Raises:
        TypeError: 如果输入不是字符串。
        ValueError: 如果模式串为空。
    """
    if not isinstance(text, str) or not isinstance(pattern, str):
        raise TypeError("text 和 pattern 必须是字符串")
    if not pattern:
        raise ValueError("pattern 不能为空")

    n, m = len(text), len(pattern)
    if m > n:
        return []

    lps = kmp_build_lps(pattern)
    i = 0
    j = 0
    result = []

    while i < n:
        if text[i] == pattern[j]:
            i += 1
            j += 1
            if j == m:
                result.append(i - j)
                j = lps[j - 1]
        else:
            if j != 0:
                j = lps[j - 1]
            else:
                i += 1

    return result


def naive_search(text: str, pattern: str) -> int:
    """朴素字符串搜索算法（参考实现，用于对比测试）。

    逐个位置检查模式串是否匹配，最坏情况 O(nm)。

    时间复杂度: O(nm)，n 为文本长度，m 为模式串长度。
    空间复杂度: O(1)。

    Args:
        text: 文本字符串。
        pattern: 要搜索的模式串。

    Returns:
        模式串首次出现的起始索引，未找到返回 -1。

    Raises:
        TypeError: 如果输入不是字符串。
        ValueError: 如果模式串为空。
    """
    if not isinstance(text, str) or not isinstance(pattern, str):
        raise TypeError("text 和 pattern 必须是字符串")
    if not pattern:
        raise ValueError("pattern 不能为空")

    n, m = len(text), len(pattern)
    if m > n:
        return -1

    for i in range(n - m + 1):
        match = True
        for j in range(m):
            if text[i + j] != pattern[j]:
                match = False
                break
        if match:
            return i
    return -1


def naive_search_all(text: str, pattern: str) -> list[int]:
    """朴素字符串搜索全部匹配（参考实现，用于对比测试）。

    Args:
        text: 文本字符串。
        pattern: 要搜索的模式串。

    Returns:
        所有匹配起始索引的列表。

    Raises:
        TypeError: 如果输入不是字符串。
        ValueError: 如果模式串为空。
    """
    if not isinstance(text, str) or not isinstance(pattern, str):
        raise TypeError("text 和 pattern 必须是字符串")
    if not pattern:
        raise ValueError("pattern 不能为空")

    n, m = len(text), len(pattern)
    if m > n:
        return []

    result = []
    for i in range(n - m + 1):
        match = True
        for j in range(m):
            if text[i + j] != pattern[j]:
                match = False
                break
        if match:
            result.append(i)
    return result


def rabin_karp_search(text: str, pattern: str) -> int:
    """使用 Rabin-Karp 算法查找模式串首次出现的位置。

    使用滚动哈希（Rolling Hash）在 O(n) 时间内比较所有可能的子串。
    哈希冲突时使用朴素比较验证。

    时间复杂度: 平均 O(n + m)，最坏 O(nm)。
    空间复杂度: O(1)。

    Args:
        text: 文本字符串。
        pattern: 要搜索的模式串。

    Returns:
        模式串首次出现的起始索引，未找到返回 -1。

    Raises:
        TypeError: 如果输入不是字符串。
        ValueError: 如果模式串为空。
    """
    if not isinstance(text, str) or not isinstance(pattern, str):
        raise TypeError("text 和 pattern 必须是字符串")
    if not pattern:
        raise ValueError("pattern 不能为空")

    n, m = len(text), len(pattern)
    if m > n:
        return -1
    if m == 0:
        return 0

    # 哈希参数
    d = 256  # 字符集大小
    q = 101  # 质数（减少冲突）

    # 计算 d^(m-1) % q
    h = 1
    for _ in range(m - 1):
        h = (h * d) % q

    # 计算模式串和文本第一个窗口的哈希值
    p_hash = 0
    t_hash = 0
    for i in range(m):
        p_hash = (d * p_hash + ord(pattern[i])) % q
        t_hash = (d * t_hash + ord(text[i])) % q

    # 滑动窗口比较
    for i in range(n - m + 1):
        if p_hash == t_hash:
            # 哈希值匹配，朴素验证
            match = True
            for j in range(m):
                if text[i + j] != pattern[j]:
                    match = False
                    break
            if match:
                return i

        # 计算下一个窗口的哈希值
        if i < n - m:
            t_hash = (d * (t_hash - ord(text[i]) * h) + ord(text[i + m])) % q
            # 处理负数
            if t_hash < 0:
                t_hash += q

    return -1


def rabin_karp_search_all(text: str, pattern: str) -> list[int]:
    """Rabin-Karp 算法查找所有匹配位置。

    Args:
        text: 文本字符串。
        pattern: 要搜索的模式串。

    Returns:
        所有匹配起始索引的列表。

    Raises:
        TypeError: 如果输入不是字符串。
        ValueError: 如果模式串为空。
    """
    if not isinstance(text, str) or not isinstance(pattern, str):
        raise TypeError("text 和 pattern 必须是字符串")
    if not pattern:
        raise ValueError("pattern 不能为空")

    n, m = len(text), len(pattern)
    if m > n:
        return []

    d = 256
    q = 101

    h = 1
    for _ in range(m - 1):
        h = (h * d) % q

    p_hash = 0
    t_hash = 0
    for i in range(m):
        p_hash = (d * p_hash + ord(pattern[i])) % q
        t_hash = (d * t_hash + ord(text[i])) % q

    result = []
    for i in range(n - m + 1):
        if p_hash == t_hash:
            match = True
            for j in range(m):
                if text[i + j] != pattern[j]:
                    match = False
                    break
            if match:
                result.append(i)

        if i < n - m:
            t_hash = (d * (t_hash - ord(text[i]) * h) + ord(text[i + m])) % q
            if t_hash < 0:
                t_hash += q

    return result