"""字符串编码 — Level 3：Base64 编码 (O(n) 时间, O(n) 空间)。

Base64 是一种将二进制数据编码为文本的编码方案，广泛用于在文本协议中传输二进制数据。
它将每 3 个字节（24 bits）编码为 4 个可打印字符（每个字符 6 bits）。

本实现手动展示 Base64 的核心编码/解码逻辑，而非直接调用标准库。
标准库实现见 Python 的 base64 模块（用于对比验证）。

Base64 字母表（标准 RFC 4648）:
    A-Z, a-z, 0-9, +, /
"""

# Base64 标准字母表
BASE64_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
# 解码映射表
BASE64_DECODE = {ch: i for i, ch in enumerate(BASE64_ALPHABET)}


def base64_encode(s: str, encoding: str = "utf-8") -> str:
    """将字符串编码为 Base64 格式。

    先将字符串按指定编码转为字节，再对字节进行 Base64 编码。

    Args:
        s: 要编码的字符串。
        encoding: 字符编码方式（默认 utf-8），如 "ascii", "latin-1"。

    Returns:
        Base64 编码后的字符串。

    Raises:
        TypeError: 如果 s 不是字符串。
        ValueError: 如果编码方式无效。

    Examples:
        >>> base64_encode("Hello")
        'SGVsbG8='
        >>> base64_encode("Base64")
        'QmFzZTY0'
        >>> base64_encode("")
        ''
        >>> base64_encode("A")
        'QQ=='
        >>> base64_encode("AB")
        'QUI='
        >>> base64_encode("ABC")
        'QUJD'
        >>> base64_encode("Hello, World!")
        'SGVsbG8sIFdvcmxkIQ=='
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    if not s:
        return ""

    try:
        data = s.encode(encoding)
    except (LookupError, UnicodeEncodeError) as e:
        raise ValueError(f"编码方式无效: {e}") from e

    result: list[str] = []
    n = len(data)

    for i in range(0, n, 3):
        # 取 3 个字节（24 bits）
        chunk = data[i:i + 3]
        chunk_len = len(chunk)

        # 将字节拼接为 24 位整数
        value = 0
        for b in chunk:
            value = (value << 8) | b

        # 处理不足 3 字节的情况
        if chunk_len == 3:
            # 24 bits → 4 个 6-bit 值
            for j in range(3, -1, -1):
                idx = (value >> (j * 6)) & 0x3F
                result.append(BASE64_ALPHABET[idx])
        elif chunk_len == 2:
            # 16 bits → 左移 8 bits 凑 24，取前 3 个 6-bit 值，补 1 个 =
            value <<= 8
            for j in range(3, 0, -1):
                idx = (value >> (j * 6)) & 0x3F
                result.append(BASE64_ALPHABET[idx])
            result.append("=")
        else:  # chunk_len == 1
            # 8 bits → 左移 16 bits 凑 24，取前 2 个 6-bit 值，补 2 个 =
            value <<= 16
            for j in range(3, 1, -1):
                idx = (value >> (j * 6)) & 0x3F
                result.append(BASE64_ALPHABET[idx])
            result.append("==")

    return "".join(result)


def base64_decode(s: str, encoding: str = "utf-8") -> str:
    """将 Base64 编码的字符串解码回原始字符串。

    先进行 Base64 解码得到字节，再按指定编码转回字符串。

    Args:
        s: Base64 编码的字符串。
        encoding: 字符编码方式（默认 utf-8）。

    Returns:
        解码后的字符串。

    Raises:
        TypeError: 如果 s 不是字符串。
        ValueError: 如果输入不是有效的 Base64 格式，或编码方式无效。

    Examples:
        >>> base64_decode("SGVsbG8=")
        'Hello'
        >>> base64_decode("QmFzZTY0")
        'Base64'
        >>> base64_decode("")
        ''
        >>> base64_decode("QQ==")
        'A'
        >>> base64_decode("QUI=")
        'AB'
        >>> base64_decode("QUJD")
        'ABC'
        >>> base64_decode("SGVsbG8sIFdvcmxkIQ==")
        'Hello, World!'
    """
    if not isinstance(s, str):
        raise TypeError("输入必须是字符串")

    if not s:
        return ""

    # 移除可能的空白字符
    s = s.strip()

    # 验证长度必须是 4 的倍数
    if len(s) % 4 != 0:
        raise ValueError("Base64 字符串长度必须是 4 的倍数")

    # 计算填充字符数
    padding = s.count("=")
    if padding > 2:
        raise ValueError("Base64 填充字符 '=' 最多 2 个")

    result_bytes: list[int] = []
    n = len(s)

    for i in range(0, n, 4):
        # 取 4 个字符（24 bits 或带填充）
        chunk = s[i:i + 4]

        value = 0
        valid_chars = 0
        for ch in chunk:
            if ch == "=":
                break
            if ch not in BASE64_DECODE:
                raise ValueError(f"无效的 Base64 字符: {ch!r}")
            value = (value << 6) | BASE64_DECODE[ch]
            valid_chars += 1

        # 根据有效字符数决定输出的字节数
        if valid_chars == 4:
            # 24 bits → 3 字节
            result_bytes.append((value >> 16) & 0xFF)
            result_bytes.append((value >> 8) & 0xFF)
            result_bytes.append(value & 0xFF)
        elif valid_chars == 3:
            # 18 bits → 2 字节
            result_bytes.append((value >> 10) & 0xFF)
            result_bytes.append((value >> 2) & 0xFF)
        elif valid_chars == 2:
            # 12 bits → 1 字节
            result_bytes.append((value >> 4) & 0xFF)

    try:
        return bytes(result_bytes).decode(encoding)
    except (LookupError, UnicodeDecodeError) as e:
        raise ValueError(f"解码方式无效: {e}") from e