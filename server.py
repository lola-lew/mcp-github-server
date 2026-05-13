import os
import base64
import hashlib
import html
import json
import secrets
import time
from contextlib import asynccontextmanager
import aiosqlite
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route
import uvicorn
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
OWNER_PASSPHRASE = os.environ["OWNER_PASSPHRASE"]
DB_PATH = os.getenv("DB_PATH", "oauth.db")
PORT = int(os.getenv("PORT", "8000"))

mcp = FastMCP("github-server", host="0.0.0.0", stateless_http=True, json_response=True)

GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS oauth_clients (
                client_id TEXT PRIMARY KEY,
                client_secret TEXT NOT NULL,
                redirect_uris TEXT NOT NULL,
                created_at INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS auth_codes (
                code TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                redirect_uri TEXT NOT NULL,
                code_challenge TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS access_tokens (
                token TEXT PRIMARY KEY,
                client_id TEXT NOT NULL,
                expires_at INTEGER NOT NULL
            )
        """)
        await db.commit()


def public_base_url(request: Request) -> str:
    return f"{request.url.scheme}://{request.url.netloc}"


def pkce_verify(code_verifier: str, stored_challenge: str) -> bool:
    computed = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return secrets.compare_digest(computed, stored_challenge)


async def validate_token(token: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT expires_at FROM access_tokens WHERE token = ?", (token,)
        ) as cursor:
            row = await cursor.fetchone()
    return row is not None and row[0] >= time.time()


async def log_requests(request: Request, call_next):
    body = await request.body()
    logger.info(f">>> {request.method} {request.url.path}")
    logger.info(f"    headers: {dict(request.headers)}")
    logger.info(f"    body: {body.decode('utf-8', errors='replace')}")
    return await call_next(request)


async def mcp_auth_dispatch(request: Request, call_next):
    if request.url.path.startswith("/mcp"):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer resource_metadata="https://mcp-github-server-production.up.railway.app/.well-known/oauth-protected-resource", scope="mcp"'},
            )
        token = auth.removeprefix("Bearer ")
        if not await validate_token(token):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer resource_metadata="https://mcp-github-server-production.up.railway.app/.well-known/oauth-protected-resource", scope="mcp"'},
            )
    return await call_next(request)


async def github_get(path: str, params: dict = None) -> dict | list:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://api.github.com{path}",
            headers=GITHUB_HEADERS,
            params=params,
        )
    if resp.status_code == 401:
        raise ValueError("GitHub authentication failed. Check GITHUB_TOKEN.")
    if resp.status_code == 403:
        raise ValueError("GitHub API rate limit exceeded or access forbidden.")
    if resp.status_code == 404:
        raise ValueError("Resource not found on GitHub.")
    resp.raise_for_status()
    return resp.json()


@mcp.tool()
async def list_contents(owner: str, repo: str, path: str = "/") -> str:
    """List files and folders at a path in a GitHub repository."""
    clean_path = path.strip("/")
    data = await github_get(f"/repos/{owner}/{repo}/contents/{clean_path}")
    if isinstance(data, list):
        return "\n".join(f"{item['type']}: {item['name']}" for item in data)
    return f"{data['type']}: {data['name']}"


@mcp.tool()
async def get_file(owner: str, repo: str, path: str) -> str:
    """Get the decoded content of a file in a GitHub repository."""
    data = await github_get(f"/repos/{owner}/{repo}/contents/{path.lstrip('/')}")
    if data.get("encoding") == "base64":
        return base64.b64decode(data["content"]).decode("utf-8")
    return data.get("content", "")


@mcp.tool()
async def search_code(owner: str, repo: str, query: str, path: str = None) -> str:
    """Search for code in a GitHub repository."""
    q = f"{query} repo:{owner}/{repo}"
    if path:
        q += f" path:{path}"
    data = await github_get("/search/code", params={"q": q, "per_page": 10})
    items = data.get("items", [])
    if not items:
        return "No results found."
    return "\n".join(f"{item['path']} (score: {item['score']:.1f})" for item in items)


@mcp.tool()
async def list_commits(owner: str, repo: str, branch: str = "main", per_page: int = 10) -> str:
    """List recent commits on a branch."""
    data = await github_get(
        f"/repos/{owner}/{repo}/commits",
        params={"sha": branch, "per_page": per_page},
    )
    lines = []
    for c in data:
        sha = c["sha"][:7]
        msg = c["commit"]["message"].split("\n")[0]
        author = c["commit"]["author"]["name"]
        lines.append(f"{sha}  {author}: {msg}")
    return "\n".join(lines)


@mcp.tool()
async def get_commit(owner: str, repo: str, sha: str) -> str:
    """Get details of a specific commit."""
    data = await github_get(f"/repos/{owner}/{repo}/commits/{sha}")
    commit = data["commit"]
    files = data.get("files", [])
    file_lines = "\n".join(f"  {f['status']}: {f['filename']}" for f in files)
    return (
        f"SHA: {data['sha']}\n"
        f"Author: {commit['author']['name']} <{commit['author']['email']}>\n"
        f"Date: {commit['author']['date']}\n"
        f"Message: {commit['message']}\n"
        f"Files changed ({len(files)}):\n{file_lines}"
    )


@mcp.tool()
async def list_branches(owner: str, repo: str) -> str:
    """List all branches in a GitHub repository."""
    data = await github_get(
        f"/repos/{owner}/{repo}/branches",
        params={"per_page": 100},
    )
    return "\n".join(b["name"] for b in data)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


async def oauth_protected_resource(request: Request) -> JSONResponse:
    base = public_base_url(request)
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
    })


async def oauth_authorization_server(request: Request) -> JSONResponse:
    base = public_base_url(request).replace("http://", "https://")
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["client_secret_post", "none"],
        "code_challenge_methods_supported": ["S256"],
        "scopes_supported": ["mcp"],
    })


async def register_get(request: Request) -> JSONResponse:
    return JSONResponse({})


async def register(request: Request) -> JSONResponse:
    body = await request.json()
    client_id = secrets.token_urlsafe(16)
    client_secret = secrets.token_urlsafe(32)
    redirect_uris = body.get("redirect_uris", [])
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO oauth_clients (client_id, client_secret, redirect_uris, created_at) VALUES (?, ?, ?, ?)",
            (client_id, client_secret, json.dumps(redirect_uris), int(time.time())),
        )
        await db.commit()
    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uris": redirect_uris,
        "client_secret_expires_at": 0,
        "token_endpoint_auth_method": "client_secret_post",
    }, status_code=201)


async def authorize_get(request: Request) -> HTMLResponse:
    client_id = request.query_params.get("client_id", "")
    redirect_uri = request.query_params.get("redirect_uri", "")
    code_challenge = request.query_params.get("code_challenge", "")
    code_challenge_method = request.query_params.get("code_challenge_method", "")
    state = request.query_params.get("state", "")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT client_id FROM oauth_clients WHERE client_id = ?", (client_id,)
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return HTMLResponse("client_id no reconocido.", status_code=400)

    def h(v: str) -> str:
        return html.escape(v, quote=True)

    page = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Autorizar acceso de Claude</title></head>
<body>
<h2>Autorizar acceso de Claude</h2>
<p>Una aplicación solicita acceso a tus repositorios de GitHub a través de este servidor MCP.</p>
<form method="POST" action="/authorize">
  <input type="hidden" name="client_id" value="{h(client_id)}">
  <input type="hidden" name="redirect_uri" value="{h(redirect_uri)}">
  <input type="hidden" name="code_challenge" value="{h(code_challenge)}">
  <input type="hidden" name="code_challenge_method" value="{h(code_challenge_method)}">
  <input type="hidden" name="state" value="{h(state)}">
  <label>Contraseña: <input type="password" name="passphrase"></label>
  <button type="submit">Autorizar</button>
</form>
</body>
</html>"""
    return HTMLResponse(page)


async def authorize_post(request: Request) -> HTMLResponse | RedirectResponse:
    form = await request.form()
    passphrase = form.get("passphrase", "")
    client_id = form.get("client_id", "")
    redirect_uri = form.get("redirect_uri", "")
    code_challenge = form.get("code_challenge", "")
    state = form.get("state", "")

    if not secrets.compare_digest(passphrase, OWNER_PASSPHRASE):
        return HTMLResponse("Contrasena incorrecta.", status_code=401)

    code = secrets.token_urlsafe(32)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO auth_codes (code, client_id, redirect_uri, code_challenge, expires_at) VALUES (?, ?, ?, ?, ?)",
            (code, client_id, redirect_uri, code_challenge, int(time.time()) + 600),
        )
        await db.commit()

    return RedirectResponse(f"{redirect_uri}?code={code}&state={state}", status_code=302)


async def token(request: Request) -> JSONResponse:
    form = await request.form()
    grant_type = form.get("grant_type", "")
    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    code = form.get("code", "")
    client_id = form.get("client_id", "")
    code_verifier = form.get("code_verifier", "")

    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT code_challenge, expires_at FROM auth_codes WHERE code = ? AND client_id = ?",
            (code, client_id),
        ) as cursor:
            row = await cursor.fetchone()

    if not row:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    code_challenge, expires_at = row
    if expires_at < time.time():
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    if not pkce_verify(code_verifier, code_challenge):
        return JSONResponse({"error": "invalid_grant"}, status_code=400)

    access_token = secrets.token_urlsafe(48)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM auth_codes WHERE code = ?", (code,))
        await db.execute(
            "INSERT INTO access_tokens (token, client_id, expires_at) VALUES (?, ?, ?)",
            (access_token, client_id, int(time.time()) + 28800),
        )
        await db.commit()

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 28800,
        "scope": "mcp",
    })


mcp_asgi = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app):
    await init_db()
    yield


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/health", health),
        Route("/.well-known/oauth-protected-resource/mcp", oauth_protected_resource),
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server),
        Route("/register", register_get, methods=["GET"]),
        Route("/register", register, methods=["POST"]),
        Route("/authorize", authorize_get, methods=["GET"]),
        Route("/authorize", authorize_post, methods=["POST"]),
        Route("/token", token, methods=["POST"]),
        Mount("/", app=mcp_asgi),
    ],
)
app.add_middleware(BaseHTTPMiddleware, dispatch=log_requests)
app.add_middleware(BaseHTTPMiddleware, dispatch=mcp_auth_dispatch)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
