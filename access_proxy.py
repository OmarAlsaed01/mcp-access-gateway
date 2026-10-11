# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "starlette>=0.40",
#     "uvicorn>=0.30",
#     "httpx>=0.27",
#     "pyjwt[crypto]>=2.8",
# ]
# ///
"""Origin-side auth layer between cloudflared and a local MCP server.

Every request must carry a Cloudflare Access JWT (Cf-Access-Jwt-Assertion)
signed by the configured team, issued for the configured Access application
(aud), for an allowed email. Anything else gets 403 and never reaches the
upstream server. Fails closed: missing or invalid config stops the proxy from
starting.

Config comes from proxy-config.json next to this file (or the path in
GATEWAY_CONFIG); environment variables override individual keys.
"""

import asyncio
import contextlib
import json
import logging
import logging.handlers
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import jwt
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = HERE / "proxy-config.json"

# Deliberately not configurable: the proxy is only ever reached through the
# local cloudflared connector, never directly from the network.
LISTEN_HOST = "127.0.0.1"

REQUIRED_KEYS = ("team_domain", "aud", "allowed_emails")
DEFAULTS = {"listen_port": 8485, "upstream": "http://127.0.0.1:8484", "log_dir": "logs"}
ENV_OVERRIDES = {
    "team_domain": "ACCESS_TEAM_DOMAIN",
    "aud": "ACCESS_AUD",
    "allowed_emails": "ACCESS_ALLOWED_EMAILS",  # comma-separated
    "listen_port": "GATEWAY_LISTEN_PORT",
    "upstream": "GATEWAY_UPSTREAM",
    "log_dir": "GATEWAY_LOG_DIR",
}

# Not forwarded upstream: hop-by-hop headers, credentials the MCP server has no
# use for, and Origin (MCP SDK servers' DNS-rebinding guard rejects
# non-localhost origins).
DROP_REQUEST_HEADERS = {
    "host", "connection", "keep-alive", "transfer-encoding", "upgrade",
    "content-length", "accept-encoding", "origin", "cookie", "authorization",
    "cf-access-jwt-assertion", "cf-access-client-id", "cf-access-client-secret",
}
DROP_RESPONSE_HEADERS = {"connection", "keep-alive", "transfer-encoding", "server", "date"}

log = logging.getLogger("access_proxy")


class ConfigError(Exception):
    pass


def load_config(path: Path | None = None, env: dict | None = None) -> dict:
    """Merges the config file with env overrides and validates the result.

    Raises ConfigError on anything missing or malformed.
    """
    env = os.environ if env is None else env
    explicit = path is not None or bool(env.get("GATEWAY_CONFIG"))
    path = Path(path or env.get("GATEWAY_CONFIG") or DEFAULT_CONFIG_PATH)

    config = dict(DEFAULTS)
    if path.exists():
        try:
            config.update(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    elif explicit:
        raise ConfigError(f"{path} is missing")
    for key, var in ENV_OVERRIDES.items():
        if env.get(var):
            config[key] = env[var]

    if isinstance(config.get("allowed_emails"), str):
        config["allowed_emails"] = config["allowed_emails"].split(",")
    config["allowed_emails"] = {
        e.strip().lower() for e in config.get("allowed_emails") or [] if e.strip()
    }
    missing = [k for k in REQUIRED_KEYS if not config.get(k)]
    if missing:
        raise ConfigError(f"no value for {', '.join(missing)} (checked {path} and env)")
    if any("@" not in e or "*" in e for e in config["allowed_emails"]):
        raise ConfigError("allowed_emails must be exact addresses (no wildcards)")

    try:
        config["listen_port"] = int(config["listen_port"])
    except (TypeError, ValueError):
        raise ConfigError(f"listen_port {config['listen_port']!r} is not a number") from None
    if not 1 <= config["listen_port"] <= 65535:
        raise ConfigError(f"listen_port {config['listen_port']} is out of range")
    upstream = urlsplit(config["upstream"])
    if upstream.scheme not in ("http", "https") or not upstream.hostname:
        raise ConfigError(f"upstream {config['upstream']!r} must be an http(s) URL")

    team = config["team_domain"].removeprefix("https://").rstrip("/")
    config["issuer"] = f"https://{team}"
    config["certs_url"] = f"https://{team}/cdn-cgi/access/certs"
    config["log_dir"] = HERE / config["log_dir"]  # an absolute log_dir stays as-is
    return config


class AccessVerifier:
    def __init__(self, config: dict):
        self.issuer = config["issuer"]
        self.aud = config["aud"]
        self.allowed_emails = config["allowed_emails"]
        # Keys are cached for an hour; an unknown key ID triggers a refetch, so
        # rotating the Access signing keys needs no restart.
        self.jwks = jwt.PyJWKClient(config["certs_url"], cache_keys=True, lifespan=3600)

    def verify(self, token: str) -> str:
        """Returns the verified email, or raises."""
        key = self.jwks.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=self.aud,
            issuer=self.issuer,
            options={"require": ["exp", "iat", "aud", "iss"]},
            leeway=30,
        )
        email = (claims.get("email") or "").lower()
        if email not in self.allowed_emails:
            raise PermissionError(f"email {email or '<none>'} is not allowed")
        return email


def build_app(verifier: AccessVerifier, upstream: str) -> Starlette:
    client = httpx.AsyncClient(
        base_url=upstream,
        timeout=httpx.Timeout(connect=5, read=None, write=30, pool=None),
    )

    async def proxy(request: Request):
        token = request.headers.get("cf-access-jwt-assertion")
        if not token:
            log.warning("403 %s %s: no Access JWT", request.method, request.url.path)
            return JSONResponse({"error": "forbidden"}, status_code=403)
        try:
            email = await asyncio.to_thread(verifier.verify, token)
        except Exception as exc:
            log.warning("403 %s %s: %s", request.method, request.url.path, exc)
            return JSONResponse({"error": "forbidden"}, status_code=403)

        headers = {
            k: v for k, v in request.headers.items() if k.lower() not in DROP_REQUEST_HEADERS
        }
        upstream_request = client.build_request(
            request.method,
            request.url.path,
            params=request.query_params,
            headers=headers,
            content=await request.body(),
        )
        try:
            response = await client.send(upstream_request, stream=True)
        except httpx.HTTPError as exc:
            log.error("502 %s %s: upstream unreachable: %s", request.method, request.url.path, exc)
            return JSONResponse({"error": "upstream unavailable"}, status_code=502)

        log.info("%s %s %s -> %s", email, request.method, request.url.path, response.status_code)
        return StreamingResponse(
            response.aiter_raw(),
            status_code=response.status_code,
            headers={
                k: v for k, v in response.headers.items() if k.lower() not in DROP_RESPONSE_HEADERS
            },
            background=BackgroundTask(response.aclose),
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        yield
        await client.aclose()

    return Starlette(
        routes=[
            Route("/{path:path}", proxy, methods=["GET", "POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"])
        ],
        lifespan=lifespan,
    )


def setup_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_dir / "proxy.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    logging.basicConfig(level=logging.INFO, handlers=[handler, console])
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main() -> None:
    try:
        config = load_config()
    except ConfigError as exc:
        sys.exit(f"refusing to start: {exc}")
    setup_logging(config["log_dir"])
    app = build_app(AccessVerifier(config), config["upstream"])
    log.info("listening on %s:%s -> %s", LISTEN_HOST, config["listen_port"], config["upstream"])
    uvicorn.run(app, host=LISTEN_HOST, port=config["listen_port"], log_level="warning")


if __name__ == "__main__":
    main()
