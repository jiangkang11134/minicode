"""字符串编码算法实现集 — 四层递进。

| 层级 | 算法 | 时间复杂度 | 空间复杂度 | 特点 |
|------|------|-----------|-----------|------|
| 1    | 凯撒密码 | O(n) | O(n) | 经典替换密码，支持编码/解码 |
| 2    | 游程编码 | O(n) | O(n) | 无损数据压缩，适合重复数据 |
| 3    | Base64 | O(n) | O(n) | 二进制到文本编码，可逆 |
| 4    | 哈夫曼编码 | O(n log n) | O(n) | 最优前缀编码，压缩比最高 |

可用函数:
    caesar_encode          — 凯撒密码编码，O(n) 时间，O(n) 空间
    caesar_decode          — 凯撒密码解码，O(n) 时间，O(n) 空间
    rle_encode             — 游程编码压缩，O(n) 时间，O(n) 空间
    rle_decode             — 游程编码解压，O(n) 时间，O(n) 空间
    base64_encode          — Base64 编码，O(n) 时间，O(n) 空间
    base64_decode          — Base64 解码，O(n) 时间，O(n) 空间
    huffman_encode         — 哈夫曼编码，O(n log n) 时间，O(n) 空间
    huffman_decode         — 哈夫曼解码，O(n) 时间，O(n) 空间
"""
from string_encoding_algorithms.level1_caesar import caesar_encode, caesar_decode
from string_encoding_algorithms.level2_rle import rle_encode, rle_decode
from string_encoding_algorithms.level3_base64 import base64_encode, base64_decode
from string_encoding_algorithms.level4_huffman import huffman_encode, huffman_decode

__all__ = [
    "caesar_encode",
    "caesar_decode",
    "rle_encode",
    "rle_decode",
    "base64_encode",
    "base64_decode",
    "huffman_encode",
    "huffman_decode",
]