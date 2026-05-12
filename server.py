import os
import base64
from contextlib import asynccontextmanager
import httpx
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.requests import Request
from starlette.routing import Mount, Route
import uvicorn

load_dotenv()

GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
PORT = int(os.getenv("PORT", "8000"))

mcp = FastMCP("github-server", host="0.0.0.0", stateless_http=True, json_response=True)

GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


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


mcp_asgi = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app):
    yield


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/health", health),
        Mount("/", app=mcp_asgi),
    ],
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
