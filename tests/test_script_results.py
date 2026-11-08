import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from mcp.server.fastmcp import FastMCP


@pytest.fixture
def script_results():
    spec = importlib.util.spec_from_file_location(
        "script_results", Path(__file__).parents[1] / "scripts" / "mcp_results.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def test_script_helper_preserves_real_sdk_structured_result(script_results):
    server = FastMCP("Synthetic script result")

    @server.tool()
    async def value() -> dict[str, int]:
        return {"value": 42}

    content, payload = await script_results.call_structured_tool(server, "value", {})
    assert content and payload == {"value": 42}


@pytest.mark.parametrize(
    "result", [{"sensitive": "synthetic"}, ([],), ([], []), (None, {})]
)
async def test_script_helper_rejects_unexpected_shapes_without_dumping_data(
    script_results, result
):
    server = SimpleNamespace(call_tool=AsyncMock(return_value=result))
    with pytest.raises(ValueError) as error:
        await script_results.call_structured_tool(server, "tool", {})
    assert "synthetic" not in str(error.value)
