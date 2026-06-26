"""
tools/mcp_readonly_server.py — Read-only JARVIS tools as a stdio MCP server
===========================================================================
CofCITIP — Phase 2, Session 4.

WHY THIS FILE EXISTS (correction to the session assumption):
  mcpo wraps an MCP *server* (a stdio JSON-RPC process) and re-exposes it as an
  OpenAPI REST API. It does NOT wrap bare Python functions directly. The
  existing JARVIS connector functions (intune.query_compliance, etc.) are plain
  (params: dict) -> dict callables, not an MCP server — so there is nothing for
  mcpo to point at until we expose them over MCP. This file is that thin MCP
  server: it imports the SAME connector functions jarvis_core already uses and
  registers each as an MCP tool. mcpo (see config/mcpo_config.json) launches
  this over stdio and turns it into /jarvis-readonly/* REST endpoints.

SCOPE — READ-ONLY, FULL STOP:
  Only the safe, non-mutating query tools are registered here:
    - intune.query_compliance
    - intune.query_device_detail
    - jamf.query_fleet
    - jamf.query_patch_status
    - servicenow.search_kb
  NO write actions (create_ticket / update_ticket / create_incident /
  package_pipeline stages / teams.send_approval_request) are exposed. Those
  remain behind the human-confirmation-gated path inside jarvis_core.py ONLY.
  Adding a write tool here would route a mutation around that gate — never do it.

  Note also: these tools return institutional ops data. Exposing them as REST is
  for on-campus, authenticated consumers (the on-ramp to proper MCP servers for
  Graph/ServiceNow/Teams later). mcpo should be bound to loopback / campus LAN
  and key-protected (see the install unit) — this REST surface is NOT an egress
  path and must never be reachable off-campus.

DEPENDENCY: fastmcp (MCP server SDK). Installed by jarvis-install.sh.
  Run standalone for debugging:  python3 -m tools.mcp_readonly_server
  Normally launched by mcpo via the config, not by hand.
"""

from __future__ import annotations

import structlog
from mcp.server.fastmcp import FastMCP  # fastmcp ships under mcp.server.fastmcp

log = structlog.get_logger("jarvis.tools.mcp_readonly")

# Server name becomes the mcpo route prefix context; mcpo uses the config key
# ("jarvis-readonly") for the actual URL path, this name is the MCP identity.
mcp = FastMCP("jarvis-readonly")


# ── INTUNE (read-only) ────────────────────────────────────────────────────────
@mcp.tool()
def query_intune_compliance(filter: str = "all", platform: str = "") -> dict:
    """Device compliance stats from Microsoft Intune. filter: 'non-compliant',
    'compliant', or 'all'. platform: optional, e.g. 'Windows', 'macOS'."""
    from tools import intune
    return intune.query_compliance({"filter": filter, "platform": platform})


@mcp.tool()
def query_intune_device_detail(identifier: str) -> dict:
    """Look up one Intune-managed device by hostname or user principal name."""
    from tools import intune
    return intune.query_device_detail({"identifier": identifier})


# ── JAMF (read-only) ──────────────────────────────────────────────────────────
@mcp.tool()
def query_jamf_fleet(filter: str = "all") -> dict:
    """Mac fleet health/compliance from Jamf Pro. filter: 'all', 'stale', or a
    macOS version string."""
    from tools import jamf
    return jamf.query_fleet({"filter": filter})


@mcp.tool()
def query_jamf_patch_status(title: str = "") -> dict:
    """macOS patch-management status from Jamf Pro. title: optional software
    title filter."""
    from tools import jamf
    return jamf.query_patch_status({"title": title})


# ── SERVICENOW KB (read-only) ─────────────────────────────────────────────────
@mcp.tool()
def search_knowledge_base(query: str, limit: int = 5) -> dict:
    """Search the CofC ServiceNow knowledge base for help articles. READ-ONLY:
    this is search_kb only — ticket/incident CRUD is deliberately not exposed
    here (stays behind jarvis_core's confirmation gate)."""
    from tools import servicenow
    return servicenow.search_kb({"query": query, "limit": limit})


if __name__ == "__main__":
    # stdio transport is what mcpo speaks to. No print(); FastMCP owns stdout
    # for the JSON-RPC channel, our logs go through structlog to stderr/journald.
    log.info("mcp_readonly.start", transport="stdio",
             tools=["query_intune_compliance", "query_intune_device_detail",
                    "query_jamf_fleet", "query_jamf_patch_status",
                    "search_knowledge_base"])
    mcp.run()  # defaults to stdio transport
