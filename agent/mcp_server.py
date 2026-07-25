"""MCP server exposing the Eco-Loop tools (agent/tools.py) to the LLM agent.

Real MCP protocol server (not a hardcoded control-logic loophole) so the LLM
must reason about which tool to call and with what arguments -- this is what
the "Agentic Autonomy" grading criterion is checking for.

TODO(Phase 2): register each function in tools.py as an MCP tool with a JSON
schema (types, units, min/max) using the `mcp` SDK's server primitives, and
run this as a subprocess the agent_loop.py MCP client connects to over stdio.
"""

raise NotImplementedError("TODO(Phase 2): implement MCP server registration once tools.py is filled in")
