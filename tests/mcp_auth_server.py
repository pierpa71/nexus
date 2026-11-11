"""Server MCP di prova protetto da Bearer token (porta 9101, token: segreto123)."""
import uvicorn
from starlette.responses import JSONResponse
from mcp_test_server import srv

TOKEN = "segreto123"
inner = srv.streamable_http_app()

async def app(scope, receive, send):
    if scope["type"] == "http":
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        if auth != f"Bearer {TOKEN}":
            return await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
    await inner(scope, receive, send)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=9101, log_level="warning")
