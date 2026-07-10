"""网络搜索工具，通过 DuckDuckGo HTML 接口实现无需 API Key 的网页搜索。

解析 DuckDuckGo 搜索结果页面，提取标题、URL 和摘要片段，支持结果数量限制。
"""

from __future__ import annotations

import urllib.parse
import urllib.request

from minicode.tooling import ToolDefinition, ToolResult

MAX_RESULTS = 10


def _validate(input_data: dict) -> dict:
    """验证搜索工具的输入参数。

    检查 query 是否为非空字符串，num_results 是否在 1-10 范围内。

    参数:
        input_data: 包含 "query"（必需）和可选 "num_results" 的字典。

    返回:
        包含 query 和 num_results 字段的字典。

    抛出:
        ValueError: 当 query 为空或 num_results 超出范围时。

    重要程度: """
    query = input_data.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query is required and must be non-empty")
    num_results = int(input_data.get("num_results", 5))
    if num_results < 1 or num_results > MAX_RESULTS:
        raise ValueError(f"num_results must be between 1 and {MAX_RESULTS}")
    return {"query": query.strip(), "num_results": num_results}


def _run(input_data: dict, context) -> ToolResult:
    """执行网页搜索。

    使用 DuckDuckGo HTML 搜索引擎（无需 API Key），构建搜索 URL 并发起 HTTP 请求。
    解析返回的 HTML 结果页面，提取标题、URL 和摘要片段，格式化为编号列表。

    参数:
        input_data: 包含 "query" 和 "num_results" 的字典。
        context: 工具调用上下文。

    返回:
        ToolResult: 格式化的搜索结果文本，包含搜索词、编号结果列表（标题、URL、摘要）和结果总数。

    重要程度: """
    query = input_data["query"]
    num_results = input_data["num_results"]

    try:
        # Use DuckDuckGo HTML search (no API key required)
        search_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"

        req = urllib.request.Request(
            search_url,
            headers={
                "User-Agent": "SmartCode-Python/0.5.0 (Terminal Coding Assistant)",
                "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.8",
            },
        )

        with urllib.request.urlopen(req, timeout=15) as response:
            html = response.read().decode("utf-8", errors="replace")

        results = _parse_duckduckgo_results(html, num_results)

        if not results:
            return ToolResult(
                ok=False,
                output=f"No search results found for: {query}\n\nTry a different query or check your internet connection.",
            )

        # Format results
        lines = [f"Search results for: {query}", "=" * 60, ""]

        for i, result in enumerate(results, 1):
            lines.extend([
                f"{i}. {result['title']}",
                f"   URL: {result['url']}",
                f"   {result['snippet']}",
                "",
            ])

        lines.append(f"Total results: {len(results)}")

        return ToolResult(ok=True, output="\n".join(lines))

    except urllib.error.URLError as e:
        return ToolResult(
            ok=False,
            output=f"Search failed: {e.reason}\nQuery: {query}\n\nCheck your internet connection.",
        )
    except Exception as e:
        return ToolResult(
            ok=False,
            output=f"Search error: {e}\nQuery: {query}",
        )


def _parse_duckduckgo_results(html: str, max_results: int) -> list[dict[str, str]]:
    """解析 DuckDuckGo HTML 搜索结果页面。

    使用正则表达式提取每个结果块的标题、URL 和摘要片段，
    并清理其中的 HTML 标签和实体编码（&amp;、&quot;、&#x27;）。

    参数:
        html: DuckDuckGo 返回的原始 HTML 字符串。
        max_results: 最大返回结果数。

    返回:
        包含字典（title、url、snippet）的列表，每项表示一个搜索结果。

    重要程度: """
    import re

    results = []

    # Find all result blocks
    result_pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )

    for match in result_pattern.finditer(html):
        if len(results) >= max_results:
            break

        url = match.group(1)
        title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
        snippet = re.sub(r"<[^>]+>", "", match.group(3)).strip()

        # Clean up entities
        title = title.replace("&amp;", "&").replace("&quot;", '"').replace("&#x27;", "'")
        snippet = snippet.replace("&amp;", "&").replace("&quot;", '"').replace("&#x27;", "'")

        if url and title:
            results.append({
                "title": title,
                "url": url,
                "snippet": snippet[:200],
            })

    return results


web_search_tool = ToolDefinition(
    name="web_search",
    description="Search the web for information. Returns search results with titles, URLs, and snippets. No API key required.",
    input_schema={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
            "num_results": {"type": "number", "description": "Number of results to return (1-10, default: 5)"},
        },
        "required": ["query"],
    },
    validator=_validate,
    run=_run,
)
