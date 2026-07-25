"""Real MCP server for the Eco-Loop tools, connected to the LLM agent
in-process over an in-memory MCP transport (mcp.shared.memory) rather than a
subprocess -- same protocol, tool schemas, and client/server dispatch as a
stdio MCP server, without the IPC overhead of spawning a subprocess for
every hourly decision cycle.

(A subprocess+stdio MCP server was considered, but the EnergyPlus Python
Plugin embeds its own separate CPython 3.12 interpreter with no access to
this venv's site-packages -- see docs/ARCHITECTURE.md "Process boundary"
section. The agent, including this MCP server, therefore runs as an
independent long-running service (agent/service.py) that the EnergyPlus
plugin talks to over a small local HTTP bridge, not by importing agent code
directly.)
"""

from contextlib import asynccontextmanager

from mcp.shared.memory import create_connected_server_and_client_session

from agent.tools import mcp_app


@asynccontextmanager
async def connected_session():
    async with create_connected_server_and_client_session(mcp_app._mcp_server) as session:
        yield session
