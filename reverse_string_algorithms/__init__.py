"""字符串反转算法实现集 — 四层递进。

| 层级 | 算法 | 时间复杂度 | 空间复杂度 | 特点 |
|------|------|-----------|-----------|------|
| 1    | 切片法 s[::-1] | O(n) | O(n) | Pythonic，最简洁 |
| 2    | 双指针列表交换 | O(n) | O(1) (额外) | 原地反转，节省内存 |
| 3    | 递归法 | O(n) | O(n) (栈) | 函数式思维，示意递归 |
| 4    | join + reversed() 生成器 | O(n) | O(n) | 生成器 + 字符串拼接 |

可用函数:
    reverse_slice            — 切片法，O(n) 时间，O(n) 空间
    reverse_two_pointer      — 双指针列表交换，O(n) 时间，O(1) 额外空间
    reverse_recursive        — 递归法，O(n) 时间，O(n) 栈空间
    reverse_join             — join + reversed() 生成器，O(n) 时间，O(n) 空间
"""
from reverse_string_algorithms.level1_slice import reverse_slice
from reverse_string_algorithms.level2_two_pointer import reverse_two_pointer
from reverse_string_algorithms.level3_recursive import reverse_recursive
from reverse_string_algorithms.level4_join import reverse_join

__all__ = [
    "reverse_slice",
    "reverse_two_pointer",
    "reverse_recursive",
    "reverse_join",
]