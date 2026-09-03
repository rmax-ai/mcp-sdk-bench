"""Stdio entrypoint: python -m mcp_sdk_bench.servers.official"""
import anyio
from mcp.server.stdio import stdio_server

from mcp_sdk_bench.servers.official.server import create_server


async def _main() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


if __name__ == "__main__":
    anyio.run(_main)
