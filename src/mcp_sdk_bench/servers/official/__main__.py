"""Stdio entrypoint: python -m mcp_sdk_bench.servers.official"""
import anyio
from mcp.server.stdio import stdio_server

from mcp_sdk_bench.servers.official.server import create_server, initialization_options


async def _main() -> None:
    server = create_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            # M3.2: includes the declared ServerTasksCapability (the SDK's
            # get_capabilities never populates `tasks`; see server.py).
            initialization_options(server),
        )


if __name__ == "__main__":
    anyio.run(_main)
