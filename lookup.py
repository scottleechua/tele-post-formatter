import httpx
import os
from urllib.parse import quote


BLUESKY_API = "https://public.api.bsky.app/xrpc/app.bsky.actor.searchActors"
SERPER_API = "https://google.serper.dev/search"


async def search_bluesky(name: str, limit: int = 3) -> list[dict]:
    """Search Bluesky globally for actors matching name. Returns list of {handle, display_name}."""
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                BLUESKY_API,
                params={"q": name, "limit": limit},
                timeout=10,
            )
            r.raise_for_status()
            actors = r.json().get("actors", [])
            return [
                {
                    "handle": a["handle"],
                    "display_name": a.get("displayName") or a["handle"],
                    "url": f"https://bsky.app/profile/{a['handle']}",
                }
                for a in actors[:limit]
            ]
        except Exception:
            return []


async def search_serper(query: str) -> str | None:
    """Run a Google search via Serper and return the top organic URL."""
    api_key = os.environ["SERPER_API_KEY"]
    async with httpx.AsyncClient() as client:
        try:
            r = await client.post(
                SERPER_API,
                headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                json={"q": query, "num": 3},
                timeout=10,
            )
            r.raise_for_status()
            results = r.json().get("organic", [])
            if results:
                return results[0].get("link")
        except Exception:
            pass
    return None


async def search_twitter(name: str) -> str | None:
    """Search Google for a Twitter/X profile."""
    url = await search_serper(f'"{name}" site:x.com OR site:twitter.com')
    return url


async def search_instagram(name: str) -> str | None:
    """Search Google for an Instagram profile."""
    url = await search_serper(f'"{name}" site:instagram.com')
    return url


async def lookup_all(name: str) -> dict:
    """Look up a name on all platforms concurrently."""
    import asyncio
    bsky_task = asyncio.create_task(search_bluesky(name))
    twitter_task = asyncio.create_task(search_twitter(name))
    instagram_task = asyncio.create_task(search_instagram(name))

    bluesky, twitter, instagram = await asyncio.gather(
        bsky_task, twitter_task, instagram_task
    )
    return {
        "name": name,
        "bluesky": bluesky,       # list of {handle, display_name, url}
        "twitter": twitter,       # url string or None
        "instagram": instagram,   # url string or None
    }


def twitter_search_url(name: str) -> str:
    return f"https://x.com/search?q={quote(name)}"


def instagram_search_url(name: str) -> str:
    return f"https://www.instagram.com/explore/search/?q={quote(name)}"
