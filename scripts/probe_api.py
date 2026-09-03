"""API-shape probe for the three MCP candidates (Phase 1 research evidence).

Run: uv run python scripts/probe_api.py
Output: JSON — import/attribute existence, protocol versions, key signatures.
Rerun after any dependency bump to re-verify capability claims.
"""
import importlib.util
import inspect
import json
import re
from importlib.metadata import version as pkg_version

def find_spec(mod: str) -> bool:
    return importlib.util.find_spec(mod) is not None

def safe_dir(mod: str, pattern: str) -> list[str]:
    try:
        m = __import__(mod, fromlist=["x"])
        return sorted(n for n in dir(m) if re.search(pattern, n))
    except Exception as e:
        return [f"ERR {type(e).__name__}: {e}"]

def safe_attr(mod: str, name: str):
    try:
        m = __import__(mod, fromlist=[name])
        return getattr(m, name)
    except Exception:
        return None

def sig(mod: str, cls: str) -> str:
    try:
        m = __import__(mod, fromlist=[cls])
        return str(inspect.signature(getattr(m, cls)))
    except Exception as e:
        return f"ERR {type(e).__name__}: {e}"

out = {"versions": {}, "mcp_sdk": {}, "fastmcp": {}, "google_adk": {}}

for pkg in ("mcp", "fastmcp", "google-adk", "langgraph", "langchain-core"):
    out["versions"][pkg] = pkg_version(pkg)

# ---- official mcp SDK v2 ----
m = safe_attr("mcp", "types")
out["mcp_sdk"]["LATEST_PROTOCOL_VERSION"] = getattr(m, "LATEST_PROTOCOL_VERSION", None) if m else None
out["mcp_sdk"]["SUPPORTED_PROTOCOL_VERSIONS"] = getattr(m, "SUPPORTED_PROTOCOL_VERSIONS", None) if m else None
out["mcp_sdk"]["types_Task_Elicit_App_Ext"] = safe_dir("mcp.types", r"(Task|Elicit|App|Ext|Progress|Sampling|Negotiat)")
out["mcp_sdk"]["server_fastmcp_module"] = find_spec("mcp.server.fastmcp")
out["mcp_sdk"]["server_lowlevel"] = find_spec("mcp.server.lowlevel")
out["mcp_sdk"]["server_auth"] = find_spec("mcp.server.auth")
out["mcp_sdk"]["client_auth"] = find_spec("mcp.client.auth")
out["mcp_sdk"]["client_stdio"] = find_spec("mcp.client.stdio")
out["mcp_sdk"]["client_streamable_http"] = find_spec("mcp.client.streamable_http")
out["mcp_sdk"]["client_sse"] = find_spec("mcp.client.sse")
out["mcp_sdk"]["extensions_module"] = find_spec("mcp.extensions") or find_spec("mcp.server.extensions") or find_spec("mcp.client.extensions")
out["mcp_sdk"]["server_Session_sig"] = sig("mcp.server.session", "ServerSession")
out["mcp_sdk"]["session_methods"] = safe_dir("mcp.server.session", r"(task|elicit|sampl|progress|resource|prompt)")

# ---- FastMCP 4.x ----
out["fastmcp"]["FastMCP_sig"] = sig("fastmcp.server", "FastMCP")
out["fastmcp"]["Client_sig"] = sig("fastmcp.client", "Client")
out["fastmcp"]["server_extensions"] = find_spec("fastmcp.server.extensions")
out["fastmcp"]["extensions"] = find_spec("fastmcp.extensions")
out["fastmcp"]["tasks"] = find_spec("fastmcp.server.tasks") or find_spec("fastmcp.tasks")
out["fastmcp"]["client_methods"] = safe_dir("fastmcp.client", r"(sampl|elicit|task|resource|prompt|list_|call_)")
out["fastmcp"]["ctx_methods"] = safe_dir("fastmcp.server.context", r"(sampl|elicit|report|task|progress)")
out["fastmcp"]["protocol_version_attrs"] = safe_dir("fastmcp.utilities", r"(PROTOCOL|VERSION)")

# ---- Google ADK 2.8 ----
out["google_adk"]["mcp_toolset"] = find_spec("google.adk.tools.mcp_toolset")
out["google_adk"]["mcp_tool"] = find_spec("google.adk.tools.mcp_tool")
out["google_adk"]["mcp_server"] = find_spec("google.adk.tools.mcp_server")
out["google_adk"]["MCPToolset_sig"] = sig("google.adk.tools.mcp_toolset", "MCPToolset")
out["google_adk"]["MCPTool_sig"] = sig("google.adk.tools.mcp_tool", "MCPTool")
out["google_adk"]["mcp_toolset_attrs"] = safe_dir("google.adk.tools.mcp_toolset", r"(tool|filter|connect|protocol|version)")

print(json.dumps(out, indent=2, default=str))
