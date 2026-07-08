"""字符串比较算法实现集 — 三层递进。

| 层级 | 算法 | 时间复杂度 | 空间复杂度 | 特点 |
|------|------|-----------|-----------|------|
| 1    | 基本比较 | O(n) | O(1) | 精确匹配、汉明距离、最长公共前缀 |
| 2    | 编辑距离 | O(mn) | O(min(m,n)) | Levenshtein 距离、Damerau-Levenshtein 距离 |
| 3    | 子串搜索 | O(n+m) | O(m) | KMP 算法、Rabin-Karp 算法、朴素搜索 |

可用函数:
    exact_match(s1, s2)                      — 精确匹配，O(n)
    hamming_distance(s1, s2)                 — 汉明距离，O(n)
    longest_common_prefix(strs)              — 最长公共前缀，O(S)

    levenshtein_distance(s1, s2)             — Levenshtein 编辑距离，O(mn)
    levenshtein_ratio(s1, s2)                — 编辑距离相似度比率，O(mn)
    damerau_levenshtein_distance(s1, s2)     — Damerau-Levenshtein 距离，O(mn)

    kmp_search(text, pattern)                — KMP 首次匹配，O(n+m)
    kmp_search_all(text, pattern)            — KMP 所有匹配，O(n+m)
    naive_search(text, pattern)              — 朴素搜索（参考），O(nm)
    naive_search_all(text, pattern)          — 朴素搜索全部（参考），O(nm)
    rabin_karp_search(text, pattern)         — Rabin-Karp 首次匹配，平均 O(n+m)
    rabin_karp_search_all(text, pattern)     — Rabin-Karp 所有匹配，平均 O(n+m)
"""
from string_comparison_algorithms.level1_basic import (
    exact_match,
    hamming_distance,
    longest_common_prefix,
)
from string_comparison_algorithms.level2_levenshtein import (
    levenshtein_distance,
    levenshtein_ratio,
    damerau_levenshtein_distance,
)
from string_comparison_algorithms.level3_kmp import (
    kmp_search,
    kmp_search_all,
    naive_search,
    naive_search_all,
    rabin_karp_search,
    rabin_karp_search_all,
)

__all__ = [
    "exact_match",
    "hamming_distance",
    "longest_common_prefix",
    "levenshtein_distance",
    "levenshtein_ratio",
    "damerau_levenshtein_distance",
    "kmp_search",
    "kmp_search_all",
    "naive_search",
    "naive_search_all",
    "rabin_karp_search",
    "rabin_karp_search_all",
]