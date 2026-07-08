"""字符串编码 — Level 4：哈夫曼编码 (O(n log n) 时间, O(n) 空间)。

哈夫曼编码是一种用于无损数据压缩的最优前缀编码算法。
它根据字符出现频率构建最优二叉树（哈夫曼树），频率越高的字符编码越短。

编码过程：
    1. 统计每个字符的频率
    2. 构建哈夫曼树（最小堆 + 贪心合并）
    3. 生成前缀编码表（左0右1）
    4. 将输入字符串编码为二进制字符串

解码过程：
    1. 根据序列化的哈夫曼树重建树结构
    2. 按位遍历，从树根走到叶子节点即为一个字符

本实现输出的编码结果为"0"/"1"字符串形式，便于理解算法原理。
"""

from __future__ import annotations

import heapq
from typing import Any, Optional


class _HuffmanNode:
    """哈夫曼树节点。

    叶子节点：char 不为 None，left 和 right 为 None。
    内部节点：char 为 None，left 和 right 不为 None。
    """

    def __init__(
        self,
        freq: int,
        char: Optional[str] = None,
        left: Optional[_HuffmanNode] = None,
        right: Optional[_HuffmanNode] = None,
    ):
        self.freq = freq
        self.char = char
        self.left = left
        self.right = right

    def __lt__(self, other: _HuffmanNode) -> bool:
        """支持 heapq 比较，频率低优先。频率相同时无特定顺序。"""
        return self.freq < other.freq

    def is_leaf(self) -> bool:
        """判断是否为叶子节点。"""
        return self.char is not None


def _build_freq_table(s: str) -> dict[str, int]:
    """统计字符串中各字符的频率。"""
    freq: dict[str, int] = {}
    for ch in s:
        freq[ch] = freq.get(ch, 0) + 1
    return freq


def _build_huffman_tree(freq: dict[str, int]) -> Optional[_HuffmanNode]:
    """根据频率表构建哈夫曼树。

    使用最小堆：（频率, 节点），每次合并两个最小频率的节点。

    Args:
        freq: 字符频率表 {char: count}。

    Returns:
        哈夫曼树的根节点，如果频率表为空则返回 None。
    """
    if not freq:
        return None

    # 构建最小堆
    heap: list[_HuffmanNode] = []
    for ch, count in freq.items():
        heapq.heappush(heap, _HuffmanNode(count, char=ch))

    # 贪心合并：每次取出两个最小频率的节点，合并为一个新节点
    while len(heap) > 1:
        left = heapq.heappop(heap)
        right = heapq.heappop(heap)
        merged = _HuffmanNode(left.freq + right.freq, left=left, right=right)
        heapq.heappush(heap, merged)

    return heap[0]


def _generate_codes(
    node: Optional[_HuffmanNode],
    prefix: str = "",
    code_map: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """从哈夫曼树递归生成前缀编码表。

    Args:
        node: 当前节点。
        prefix: 当前路径编码（左0右1）。
        code_map: 编码表（递归过程中累积）。

    Returns:
        字符到编码的映射 {char: "0101"}。
    """
    if code_map is None:
        code_map = {}

    if node is None:
        return code_map

    if node.is_leaf():
        # 叶子节点：使用当前路径作为编码
        # 如果只有一个字符，编码特殊处理为 "0"
        code_map[node.char] = prefix if prefix else "0"
    else:
        _generate_codes(node.left, prefix + "0", code_map)
        _generate_codes(node.right, prefix + "1", code_map)

    return code_map


def _serialize_tree(node: Optional[_HuffmanNode]) -> Any:
    """将哈夫曼树序列化为可 JSON 序列化的结构。

    格式：
        叶子节点: {"t": "leaf", "c": "A"}
        内部节点: {"t": "node", "l": left_child, "r": right_child}
        None:     None

    Args:
        node: 树节点。

    Returns:
        可 JSON 序列化的树结构。
    """
    if node is None:
        return None

    if node.is_leaf():
        return {"t": "leaf", "c": node.char}

    return {
        "t": "node",
        "l": _serialize_tree(node.left),
        "r": _serialize_tree(node.right),
    }


def _deserialize_tree(data: Any) -> Optional[_HuffmanNode]:
    """从序列化结构重建哈夫曼树。

    Args:
        data: 序列化的树结构。

    Returns:
        重建的树根节点。
    """
    if data is None:
        return None

    if data["t"] == "leaf":
        return _HuffmanNode(0, char=data["c"])

    left = _deserialize_tree(data["l"])
    right = _deserialize_tree(data["r"])
    return _HuffmanNode(0, left=left, right=right)


def _bits_to_bytes(bits: str) -> bytes:
    """将二进制字符串（"01010101"）转为字节。

    不足 8 位的在末尾补 0，并记录有效位数。

    Args:
        bits: 二进制字符串，如 "01001000"。

    Returns:
        字节数据 + 末尾 1 字节记录有效位数。
    """
    # 计算需要的字节数
    n = len(bits)
    padding = (8 - n % 8) % 8
    padded_bits = bits + "0" * padding

    # 每 8 位转为一个字节
    result = bytearray()
    for i in range(0, len(padded_bits), 8):
        byte = 0
        for j in range(8):
            if padded_bits[i + j] == "1":
                byte |= 1 << (7 - j)
        result.append(byte)

    # 末尾记录有效位数
    result.append(n % 256)

    return bytes(result)


def _bytes_to_bits(data: bytes) -> str:
    """将字节转回二进制字符串。

    根据末尾记录的有效位数截断。

    Args:
        data: 字节数据（末尾 1 字节为有效位数）。

    Returns:
        二进制字符串。
    """
    if not data:
        return ""

    bit_count = data[-1]
    raw_data = data[:-1]

    bits: list[str] = []
    for byte in raw_data:
        for j in range(7, -1, -1):
            bits.append("1" if (byte >> j) & 1 else "0")

    full_str = "".join(bits)
    if bit_count > 0:
        return full_str[:bit_count]

    return full_str


def huffman_encode(s: str) -> tuple[str, dict]:
    """使用哈夫曼编码对字符串进行编码。

    返回编码后的二进制字符串（"0"/"1"字符）和序列化的哈夫曼树。

    Args:
        s: 要编码的字符串。

    Returns:
        二元组 (encoded_bits, tree_dict)：
            - encoded_bits: 编码后的二进制字符串（如 "01001101"）
            - tree_dict: 序列化的哈夫曼树（用于解码）

    Raises:
        TypeError: 如果 s 不是字符串。

    Examples:
        >>> bits, tree = huffman_encode("AAAAABBBCC")
        >>> isinstance(bits, str)
        True
        >>> isinstance(tree, dict)
        True
        >>> # 编码后的比特串比原始 ASCII 短（压缩有效）
        >>> len(bits) < len("AAAAABBBCC") * 8
        True
        >>> huffman_decode(bits, tree) == "AAAAABBBCC"
        True

        >>> bits2, tree2 = huffman_encode("hello")
        >>> huffman_decode(bits2, tree2) == "hello"
        True

        >>> bits3, tree3 = huffman_encode("")
        >>> bits3 == ""
        True
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    if not s:
        return "", {}

    # 1. 统计频率
    freq = _build_freq_table(s)

    if len(freq) == 1:
        # 只有一个字符的特例：哈夫曼树只有一个叶子节点
        char = next(iter(freq))
        # 编码 = 重复 count 次 "0"
        encoded = "0" * freq[char]
        tree_dict = _serialize_tree(_HuffmanNode(0, char=char))
        return encoded, tree_dict

    # 2. 构建哈夫曼树
    root = _build_huffman_tree(freq)

    # 3. 生成编码表
    code_map = _generate_codes(root)

    # 4. 编码
    encoded_bits = "".join(code_map[ch] for ch in s)

    # 5. 序列化树
    tree_dict = _serialize_tree(root)

    return encoded_bits, tree_dict


def huffman_decode(encoded: str, tree: dict) -> str:
    """使用哈夫曼树解码二进制字符串。

    从树根开始，按位遍历（0 向左，1 向右），
    到达叶子节点时输出该字符，然后回到树根继续。

    Args:
        encoded: 哈夫曼编码后的二进制字符串（"0"/"1"）。
        tree: 序列化的哈夫曼树（从 encode 返回）。

    Returns:
        解码还原后的字符串。

    Raises:
        TypeError: 如果 encoded 不是字符串或 tree 不是字典。
        ValueError: 如果编码数据无效或树结构不完整。

    Examples:
        >>> bits, tree = huffman_encode("hello")
        >>> huffman_decode(bits, tree)
        'hello'
        >>> huffman_decode("", {})
        ''
    """
    if not isinstance(encoded, str):
        raise TypeError("编码数据必须是字符串")

    if not isinstance(tree, dict):
        raise TypeError("哈夫曼树必须是字典")

    if not encoded or not tree:
        return ""

    # 重建哈夫曼树
    root = _deserialize_tree(tree)
    if root is None:
        raise ValueError("无效的哈夫曼树")

    # 单字符特例
    if root.is_leaf():
        # 所有位都是该字符
        return root.char * len(encoded)

    result: list[str] = []
    current = root

    for bit in encoded:
        if bit not in ("0", "1"):
            raise ValueError(f"无效的编码位: {bit!r}，仅允许 '0' 或 '1'")

        if bit == "0":
            if current.left is None:
                raise ValueError("无效的编码：树中没有左子节点")
            current = current.left
        else:
            if current.right is None:
                raise ValueError("无效的编码：树中没有右子节点")
            current = current.right

        if current.is_leaf():
            result.append(current.char)
            current = root

    if current is not root:
        # 编码没有完整走完所有位
        raise ValueError("无效的编码：最后一位未到达叶子节点")

    return "".join(result)