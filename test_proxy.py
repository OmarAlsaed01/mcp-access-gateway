# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "starlette>=0.40",
#     "uvicorn>=0.30",
#     "httpx>=0.27",
#     "pyjwt[crypto]>=2.8",
# ]
# ///
"""Self-contained test of access_proxy: `uv run --script test_proxy.py`.

Starts a tiny fake MCP server as the upstream and signs tokens with a throwaway
RSA key standing in for Cloudflare Access. No network access or real MCP
server needed.
"""

import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import jwt
import uvicorn
from cryptography.hazmat.primitives.asymmetric import rsa
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

import access_proxy

TEAM = "https://example-team.cloudflareaccess.com"
AUD = "test-aud-tag"
EMAIL = "allowed@example.com"

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.05)
    return server


# --- fake upstream: just enough streamable-HTTP MCP to initialize and list tools

upstream_seen = []  # headers of every request that reached the upstream
FAKE_TOOLS = [{"name": "echo", "inputSchema": {"type": "object"}},
              {"name": "add", "inputSchema": {"type": "object"}}]


async def fake_mcp(request: Request):
    upstream_seen.append({k.lower(): v for k, v in request.headers.items()})
    msg = await request.json()
    if msg.get("method") == "initialize":
        result = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
                  "serverInfo": {"name": "fake-upstream", "version": "0"}}
        return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "result": result},
                            headers={"mcp-session-id": "fake-session"})
    if "id" not in msg:  # notification
        return Response(status_code=202)
    if msg.get("method") == "tools/list":
        if request.headers.get("mcp-session-id") != "fake-session":
            return JSONResponse({"error": "no session"}, status_code=400)
        event = json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": {"tools": FAKE_TOOLS}})
        return Response(f"event: message\ndata: {event}\n\n", media_type="text/event-stream")
    return JSONResponse({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601}})


upstream_port, proxy_port, dead_port, dead_proxy_port = (free_port() for _ in range(4))
upstream = serve(Starlette(routes=[Route("/mcp", fake_mcp, methods=["POST"])]), upstream_port)

verifier = access_proxy.AccessVerifier(
    {"issuer": TEAM, "aud": AUD, "allowed_emails": {EMAIL}, "certs_url": "http://unused"}
)
verifier.jwks = SimpleNamespace(
    get_signing_key_from_jwt=lambda _t: SimpleNamespace(key=key.public_key())
)
proxy = serve(access_proxy.build_app(verifier, f"http://127.0.0.1:{upstream_port}"), proxy_port)
# Same proxy pointed at a port nothing listens on.
dead = serve(access_proxy.build_app(verifier, f"http://127.0.0.1:{dead_port}"), dead_proxy_port)


def token(email=EMAIL, aud=AUD, iss=TEAM, exp_in=300, signer=key):
    now = int(time.time())
    return jwt.encode(
        {"email": email, "aud": [aud], "iss": iss, "iat": now, "exp": now + exp_in},
        signer,
        algorithm="RS256",
        headers={"kid": "k1"},
    )


INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "proxy-test", "version": "0"}},
}
BASE_HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "origin": "https://claude.ai",
    "host": "tools.example.com",
    "cookie": "CF_Authorization=edge-cookie",
    "authorization": "Bearer edge-oauth-token",
}

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name} {detail}")


def post(body, tok=None, extra=None, port=proxy_port):
    headers = dict(BASE_HEADERS, **(extra or {}))
    if tok is not None:
        headers["cf-access-jwt-assertion"] = tok
    return httpx.post(f"http://127.0.0.1:{port}/mcp", json=body, headers=headers, timeout=30)


def parse(resp):
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        data = [l[5:].strip() for l in resp.text.splitlines() if l.startswith("data:")]
        return json.loads(data[-1])
    return resp.json()


# --- requests without a valid Access JWT never reach the upstream

for name, tok in [
    ("no token", None),
    ("garbage token", "not-a-jwt"),
    ("wrong signing key", token(signer=other_key)),
    ("wrong audience", token(aud="other-app")),
    ("wrong issuer", token(iss="https://evil.cloudflareaccess.com")),
    ("wrong email", token(email="someone@else.example")),
    ("expired", token(exp_in=-120)),
]:
    r = post(INIT, tok)
    check(f"reject {name}", r.status_code == 403, f"(got {r.status_code})")
check("rejected requests never reached upstream", not upstream_seen, f"({len(upstream_seen)} did)")

# --- a valid token passes through, MCP session and SSE responses intact

r = post(INIT, token())
check("initialize with valid token", r.status_code == 200, f"(got {r.status_code} {r.text[:200]})")
session = r.headers.get("mcp-session-id")
extra = {"mcp-session-id": session} if session else {}
post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token(), extra)
r = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token(), extra)
tools = [t["name"] for t in parse(r).get("result", {}).get("tools", [])] if r.status_code == 200 else []
check("tools/list with valid token", tools == ["echo", "add"], f"(got {r.status_code}, tools {tools})")

leaked = {h for seen in upstream_seen for h in seen} & {
    "cf-access-jwt-assertion", "cookie", "authorization", "origin"}
check("credentials and Origin stripped before upstream", upstream_seen and not leaked,
      f"(leaked: {sorted(leaked) or 'none'})")
check("upstream sees its own host, not the public one",
      upstream_seen and upstream_seen[0].get("host") == f"127.0.0.1:{upstream_port}",
      f"(got {upstream_seen[0].get('host') if upstream_seen else None})")

r = post(INIT, token(), port=dead_proxy_port)
check("upstream down -> 502", r.status_code == 502, f"(got {r.status_code})")
r = post(INIT, None, port=dead_proxy_port)
check("upstream down, no token -> still 403", r.status_code == 403, f"(got {r.status_code})")

# --- config fails closed

GOOD = {"team_domain": "example-team.cloudflareaccess.com", "aud": AUD, "allowed_emails": [EMAIL]}


def load(contents=None, env=None):
    """Returns (config, None) or (None, error message)."""
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "proxy-config.json"
        if contents is not None:
            path.write_text(contents if isinstance(contents, str) else json.dumps(contents))
        try:
            return access_proxy.load_config(path, env or {}), None
        except access_proxy.ConfigError as exc:
            return None, str(exc)


for name, contents in [
    ("missing file", None),
    ("invalid JSON", "{not json"),
    ("no aud", {**GOOD, "aud": ""}),
    ("empty allowlist", {**GOOD, "allowed_emails": []}),
    ("wildcard email", {**GOOD, "allowed_emails": ["*@example.com"]}),
    ("bad port", {**GOOD, "listen_port": "eighty"}),
    ("non-http upstream", {**GOOD, "upstream": "file:///etc/passwd"}),
]:
    _, err = load(contents)
    check(f"config refuses {name}", err is not None, f"({err})")

config, err = load({**GOOD, "aud": "", "allowed_emails": []},
                   env={"ACCESS_AUD": "from-env", "ACCESS_ALLOWED_EMAILS": "A@x.example, b@x.example"})
check("env vars fill missing keys", config and config["aud"] == "from-env"
      and config["allowed_emails"] == {"a@x.example", "b@x.example"}, f"({err or config['allowed_emails']})")

proxy.should_exit = dead.should_exit = upstream.should_exit = True
print(f"\n{sum(results)}/{len(results)} passed")
raise SystemExit(0 if all(results) else 1)
