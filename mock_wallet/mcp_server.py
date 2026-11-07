"""MCP server exposing one tool: suggest_change.

    python mcp_server.py            # stdio transport

Register it with an MCP client, e.g. for Claude Code:
    claude mcp add mock-wallet -- python /path/to/mock_wallet/mcp_server.py

The tool only writes a 'pending' row to the suggestions table. It never
touches live_config.json, and the trading engine never reads suggestions.
"""

from __future__ import annotations

from mcp.server.mcpserver import MCPServer

import control
import live_config

server = MCPServer(
    "mock-wallet",
    instructions=("Propose strategy parameter changes with suggest_change. Suggestions are "
                  "recorded as pending and validated offline on paper money; they are never "
                  "applied automatically."))


@server.tool()
def suggest_change(parameter: str, proposed_value: float | int | None, rationale: str,
                   derived_from_start: str | None = None,
                   derived_from_end: str | None = None) -> dict:
    """Propose a new value for one live strategy parameter.

    Records a pending suggestion; it changes nothing. It will be replayed on
    the window it was derived from (default: the configured research window)
    and on a holdout window it was not derived from, and only a person can
    apply it.

    Args:
        parameter: strategy parameter, e.g. "scale" or "params.regime".
        proposed_value: positive number, or null to switch an optional rule off.
        rationale: why this should be better, citing the evidence.
        derived_from_start: start of the data window the idea came from (ISO date).
        derived_from_end: end of that window (ISO date).
    """
    try:
        s = control.add_suggestion(parameter, proposed_value, rationale,
                                   derived_from_start, derived_from_end)
    except (live_config.InvalidParameter, ValueError) as e:
        cfg = live_config.load()
        return {"ok": False, "error": str(e),
                "tunable_parameters": live_config.tunable_parameters(cfg),
                "current_values": cfg["params"]}
    return {"ok": True, "id": s["id"], "status": s["status"], "parameter": s["parameter"],
            "proposed_value": s["proposed_value"],
            "derived_window": [s["derived_start"], s["derived_end"]],
            "note": "Recorded as pending. Nothing was applied."}


if __name__ == "__main__":
    server.run()
