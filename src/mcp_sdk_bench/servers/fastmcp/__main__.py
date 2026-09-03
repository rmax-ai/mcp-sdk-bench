"""Stdio entrypoint: python -m mcp_sdk_bench.servers.fastmcp"""
from mcp_sdk_bench.servers.fastmcp.server import create_server


def main() -> None:
    create_server().run(transport="stdio")


if __name__ == "__main__":
    main()
