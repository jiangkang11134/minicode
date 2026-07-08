"""回文检测算法实现集 — 三层优化。

| 层级 | 算法 | 时间复杂度 | 空间复杂度 |
|------|------|-----------|-----------|
| 1    | 反转比较法 | O(n)      | O(n)      |
| 2    | 双指针法 | O(n)       | O(1)      |
| 3    | Manacher 算法 | O(n)    | O(n)      |

可用函数:
    is_palindrome              — 反转比较法，O(n)（基础级）
    is_palindrome_number       — 整数回文检测（双指针版）
    is_palindrome_sentence     — 英文句子回文检测（快捷版）
    is_palindrome_two_pointer  — 双指针法，O(n) 时间 O(1) 空间
    longest_palindromic_substring — Manacher O(n) 算法
    count_palindromic_substrings  — 回文子串计数（Manacher）
"""
from palindrome_algorithms.level1_simple import is_palindrome
from palindrome_algorithms.level2_two_pointer import (
    is_palindrome as is_palindrome_two_pointer,
    is_palindrome_number,
    is_palindrome_sentence,
)
from palindrome_algorithms.level3_manacher import (
    longest_palindromic_substring,
    count_palindromic_substrings,
)

__all__ = [
    "is_palindrome",
    "is_palindrome_two_pointer",
    "is_palindrome_number",
    "is_palindrome_sentence",
    "longest_palindromic_substring",
    "count_palindromic_substrings",
]