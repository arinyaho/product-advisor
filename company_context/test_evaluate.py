"""Exercise the shared evaluator/proxy HTTP boundary."""

import httpx

from company_context.evaluate import rpc


def test_rpc_uses_exact_mcp_path_without_following_redirects():
    def respond(request):
        if request.url.path != "/mcp":
            return httpx.Response(307, headers={"Location": "/mcp"})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})

    with httpx.Client(base_url="http://127.0.0.1:8766/mcp", transport=httpx.MockTransport(respond)) as client:
        assert rpc(client, "tools/list", {}) == {"tools": []}
