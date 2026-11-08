"""Typed access to FastMCP's content/structured-content tool result pair."""

from typing import Any, cast

from mcp.server.fastmcp import FastMCP
from mcp.types import ContentBlock

from trading212_mcp.market.normalization import is_list, is_mapping


async def call_structured_tool(
    server: FastMCP, name: str, arguments: dict[str, Any]
) -> tuple[list[ContentBlock], dict[str, Any]]:
    result: object = await server.call_tool(name, arguments)
    if not isinstance(result, tuple):
        raise ValueError("Expected a content/structured-content result pair")
    parts = cast(tuple[object, ...], result)
    if len(parts) != 2:
        raise ValueError("Expected a content/structured-content result pair")
    content, structured = parts
    if not is_list(content) or not is_mapping(structured):
        raise ValueError("Expected structured JSON tool output")
    return cast(list[ContentBlock], content), cast(dict[str, Any], structured)
