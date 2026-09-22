from mcp.server.fastmcp import FastMCP

server = FastMCP("test-echo")


@server.tool()
def echo(text: str) -> str:
    """Return supplied text."""
    return text


if __name__ == "__main__":
    server.run(transport="stdio")
