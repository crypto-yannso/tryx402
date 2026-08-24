"""Lightweight MCP protocol tests.

Run: python3 -m pytest gateway/tests/test_mcp.py -v
"""

import json
import sys
import types


class TestMCPProtocol:
    def test_initialize(self):
        import gateway.mcp_server as mcp

        req = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18"}}
        resp = mcp._dispatch(req)
        assert resp["protocolVersion"] == "2025-06-18"
        assert resp["serverInfo"]["name"] == "tryx402"

    def test_tools_list_includes_hosted_tools(self):
        import gateway.mcp_server as mcp

        req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}
        resp = mcp._dispatch(req)
        names = [t["name"] for t in resp["tools"]]
        for tool in ["gateway_search", "gateway_call", "gateway_plan",
                     "gateway_receipt", "gateway_check_balance", "gateway_recharge"]:
            assert tool in names, f"missing tool: {tool}"

    def test_gateway_check_balance_without_key(self):
        # Since anonymous session auth, check_balance works WITHOUT an API
        # key: it mints a session and returns the zero balance of that
        # session's wallet. The old "No API key configured" error only
        # appears when the hosted service is unreachable.
        import gateway.mcp_server as mcp

        req = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
               "params": {"name": "gateway_check_balance", "arguments": {}}}
        resp = mcp._dispatch(req)
        data = json.loads(resp["content"][0]["text"])
        if "error" in data:
            # hosted service unreachable -> legacy error message
            assert data["error"] == "No API key configured"
        else:
            assert data["balance_cents"] >= 0
            assert "customer_id" in data

    def test_gateway_recharge_requires_amount(self):
        import gateway.mcp_server as mcp

        req = {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
               "params": {"name": "gateway_recharge", "arguments": {}}}
        resp = mcp._dispatch(req)
        text = resp["content"][0]["text"]
        data = json.loads(text)
        assert "error" in data

    def test_ping(self):
        import gateway.mcp_server as mcp

        req = {"jsonrpc": "2.0", "id": 5, "method": "ping"}
        resp = mcp._dispatch(req)
        assert resp == {}
