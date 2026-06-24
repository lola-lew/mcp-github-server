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
BASE_URL = os.getenv("BASE_URL", "")
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
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource", scope="mcp"'},
            )
        token = auth.removeprefix("Bearer ")
        if not await validate_token(token):
            return JSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={"WWW-Authenticate": f'Bearer resource_metadata="{BASE_URL}/.well-known/oauth-protected-resource", scope="mcp"'},
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
async def list_contents(owner: str, repo: str, path: str = "/", ref: str = None) -> str:
    """List files and folders at a path in a GitHub repository."""
    clean_path = path.strip("/")
    data = await github_get(f"/repos/{owner}/{repo}/contents/{clean_path}", params={"ref": ref} if ref else None)
    if isinstance(data, list):
        return "\n".join(f"{item['type']}: {item['name']}" for item in data)
    return f"{data['type']}: {data['name']}"


@mcp.tool()
async def get_file(owner: str, repo: str, path: str, ref: str = None) -> str:
    """Get the decoded content of a file in a GitHub repository."""
    data = await github_get(f"/repos/{owner}/{repo}/contents/{path.lstrip('/')}", params={"ref": ref} if ref else None)
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
        return HTMLResponse("Unknown client_id.", status_code=400)

    def h(v: str) -> str:
        return html.escape(v, quote=True)

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Authorize — MCP GitHub Server</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      background: #0d1117;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
    }}
    .card {{
      background: #161b22;
      border: 1px solid #30363d;
      border-radius: 12px;
      padding: 2rem 2.5rem;
      width: 100%;
      max-width: 380px;
    }}
    .header {{
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 1.75rem;
    }}
    .icon-wrap {{
      width: 36px;
      height: 36px;
      background: #21262d;
      border: 1px solid #30363d;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      flex-shrink: 0;
    }}
    .icon-wrap svg {{ display: block; }}
    .header-title {{
      margin: 0;
      font-size: 15px;
      font-weight: 500;
      color: #e6edf3;
    }}
    .header-sub {{
      margin: 0;
      font-size: 12px;
      color: #8b949e;
    }}
    .info-box {{
      background: #21262d;
      border: 1px solid #30363d;
      border-radius: 8px;
      padding: 12px 14px;
      margin-bottom: 1.5rem;
      display: flex;
      gap: 10px;
      align-items: flex-start;
    }}
    .info-box svg {{ flex-shrink: 0; margin-top: 1px; }}
    .info-box p {{
      margin: 0;
      font-size: 12px;
      color: #8b949e;
      line-height: 1.6;
    }}
    label {{
      display: block;
      font-size: 12px;
      color: #8b949e;
      margin-bottom: 6px;
      letter-spacing: 0.03em;
    }}
    .input-wrap {{
      position: relative;
      margin-bottom: 1.25rem;
    }}
    input[type="password"], input[type="text"] {{
      width: 100%;
      background: #0d1117;
      border: 1px solid #30363d;
      border-radius: 8px;
      color: #e6edf3;
      font-size: 14px;
      padding: 9px 40px 9px 12px;
      outline: none;
      transition: border-color 0.15s, box-shadow 0.15s;
    }}
    input:focus {{
      border-color: #388bfd;
      box-shadow: 0 0 0 3px rgba(56,139,253,0.15);
    }}
    .toggle-btn {{
      position: absolute;
      right: 10px;
      top: 50%;
      transform: translateY(-50%);
      background: none;
      border: none;
      cursor: pointer;
      padding: 0;
      color: #8b949e;
      display: flex;
      align-items: center;
      line-height: 1;
    }}
    .toggle-btn:hover {{ color: #c9d1d9; }}
    .submit-btn {{
      width: 100%;
      background: #238636;
      border: 1px solid #2ea043;
      border-radius: 8px;
      color: #fff;
      font-size: 14px;
      font-weight: 500;
      padding: 9px 16px;
      cursor: pointer;
      letter-spacing: 0.01em;
      transition: background 0.15s;
    }}
    .submit-btn:hover {{ background: #2ea043; }}
    .footer-note {{
      margin: 1.25rem 0 0;
      font-size: 11px;
      color: #6e7681;
      text-align: center;
      line-height: 1.6;
    }}
  </style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div class="icon-wrap">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="#e6edf3" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
          <path d="M9 19c-5 1.5-5-2.5-7-3m14 6v-3.87a3.37 3.37 0 0 0-.94-2.61c3.14-.35 6.44-1.54 6.44-7A5.44 5.44 0 0 0 20 4.77 5.07 5.07 0 0 0 19.91 1S18.73.65 16 2.48a13.38 13.38 0 0 0-7 0C6.27.65 5.09 1 5.09 1A5.07 5.07 0 0 0 5 4.77a5.44 5.44 0 0 0-1.5 3.78c0 5.42 3.3 6.61 6.44 7A3.37 3.37 0 0 0 9 18.13V22"/>
        </svg>
      </div>
      <div>
        <p class="header-title">MCP GitHub Server</p>
        <p class="header-sub">Authorization required</p>
      </div>
    </div>

    <div class="info-box">
      <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#8b949e" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
        <rect x="3" y="11" width="18" height="11" rx="2" ry="2"/>
        <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
      </svg>
      <p>An application is requesting read access to your GitHub repositories through this MCP server.</p>
    </div>

    <form method="POST" action="/authorize">
      <input type="hidden" name="client_id" value="{h(client_id)}">
      <input type="hidden" name="redirect_uri" value="{h(redirect_uri)}">
      <input type="hidden" name="code_challenge" value="{h(code_challenge)}">
      <input type="hidden" name="code_challenge_method" value="{h(code_challenge_method)}">
      <input type="hidden" name="state" value="{h(state)}">

      <label for="passphrase">Passphrase</label>
      <div class="input-wrap">
        <input type="password" id="passphrase" name="passphrase" placeholder="Enter your passphrase" autocomplete="current-password" autofocus>
        <button type="button" class="toggle-btn" onclick="togglePass()" aria-label="Toggle passphrase visibility">
          <svg id="eye-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round">
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
            <circle cx="12" cy="12" r="3"/>
          </svg>
        </button>
      </div>

      <button type="submit" class="submit-btn">Authorize access</button>
    </form>

    <p class="footer-note">Access is limited to repositories you've granted permission to.</p>
  </div>

  <script>
    function togglePass() {{
      var input = document.getElementById('passphrase');
      var icon = document.getElementById('eye-icon');
      if (input.type === 'password') {{
        input.type = 'text';
        icon.innerHTML = '<path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24"/><line x1="1" y1="1" x2="23" y2="23"/>';
      }} else {{
        input.type = 'password';
        icon.innerHTML = '<path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>';
      }}
    }}
  </script>
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
        return HTMLResponse("Incorrect passphrase.", status_code=401)

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
            (access_token, client_id, int(time.time()) + 2592000),
        )
        await db.commit()

    return JSONResponse({
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": 2592000,
        "scope": "mcp",
    })


mcp_asgi = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app):
    await init_db()
    async with mcp_asgi.router.lifespan_context(mcp_asgi):
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
