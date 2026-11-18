from fastmcp import Client

from hello_mcp.server import mcp, say_hello


def test_say_hello_plain_function():
    assert say_hello("Ada") == "Hello, Ada!"
    assert say_hello() == "Hello, world!"


async def test_tool_listed_and_callable_over_mcp():
    async with Client(mcp) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools] == ["say_hello"]
        result = await client.call_tool("say_hello", {"name": "Databricks"})
        assert result.data == "Hello, Databricks!"
