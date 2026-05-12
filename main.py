from fastapi import FastAPI, Request, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from dotenv import load_dotenv
import json
import os
import secrets
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env", override=True)

app = FastAPI()

DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
IMAGES_DIR = Path(os.getenv("IMAGES_DIR", DATA_DIR / "images"))
NOTES_FILE = DATA_DIR / "notes.json"
TOKENS_FILE = DATA_DIR / "integration_tokens.json"
IMAGE_EXTS = [".png", ".jpg", ".jpeg", ".gif", ".webp"]
SECTIONS = ["about", "goals", "coach_needs", "races", "weaknesses", "archive"]
AUTH_REALM = "coach-site"

DATA_DIR.mkdir(exist_ok=True)
IMAGES_DIR.mkdir(parents=True, exist_ok=True)
(BASE_DIR / "static/css").mkdir(parents=True, exist_ok=True)

app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=IMAGES_DIR), name="uploads")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    password = os.getenv("COACH_SITE_PASSWORD")
    if not password or request.url.path == "/health":
        return await call_next(request)

    authorization = request.headers.get("authorization", "")
    scheme, _, credentials = authorization.partition(" ")
    authenticated = False

    if scheme.lower() == "basic" and credentials:
        try:
            import base64

            decoded = base64.b64decode(credentials).decode("utf-8")
            username, _, supplied_password = decoded.partition(":")
            authenticated = (
                secrets.compare_digest(username, os.getenv("COACH_SITE_USERNAME", "coach"))
                and secrets.compare_digest(supplied_password, password)
            )
        except Exception:
            authenticated = False

    if authenticated:
        return await call_next(request)

    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": f'Basic realm="{AUTH_REALM}"'},
    )


def load_notes() -> dict:
    if NOTES_FILE.exists():
        return json.loads(NOTES_FILE.read_text(encoding="utf-8"))
    return {}


def save_notes(notes: dict) -> None:
    NOTES_FILE.write_text(json.dumps(notes, indent=2), encoding="utf-8")


def load_token_cache() -> dict:
    if TOKENS_FILE.exists():
        return json.loads(TOKENS_FILE.read_text(encoding="utf-8"))
    return {}


def save_token_cache(tokens: dict) -> None:
    TOKENS_FILE.write_text(json.dumps(tokens, indent=2), encoding="utf-8")


def get_image_url(section: str) -> str:
    for ext in IMAGE_EXTS:
        if (IMAGES_DIR / f"{section}{ext}").exists():
            return f"/uploads/{section}{ext}"
    return ""


class IntegrationError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.status_code = status_code


def request_json(
    url: str,
    method: str = "GET",
    headers: dict | None = None,
    data: dict | None = None,
) -> dict:
    body = None
    req_headers = headers or {}
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req_headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            **req_headers,
        }

    request = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=12) as response:
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise IntegrationError(f"API request failed ({exc.code}): {raw}", exc.code)
    except urllib.error.URLError as exc:
        raise IntegrationError(f"Could not reach API: {exc.reason}")


def image_from(images: list[dict]) -> str:
    return images[0]["url"] if images else ""


def seconds_to_label(seconds: int | float | None) -> str:
    if not seconds:
        return ""
    minutes = int(seconds) // 60
    hours = minutes // 60
    minutes = minutes % 60
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def meters_to_miles(meters: int | float | None) -> str:
    if meters is None:
        return ""
    return f"{meters / 1609.344:.2f} mi"


def meters_to_feet(meters: int | float | None) -> str:
    if meters is None:
        return ""
    return f"{meters * 3.28084:.0f} ft"


def get_cached_access_token(provider: str) -> str:
    cache = load_token_cache().get(provider, {})
    if cache.get("access_token") and cache.get("expires_at", 0) > time.time() + 60:
        return cache["access_token"]
    return ""


def get_spotify_access_token() -> str:
    cached = get_cached_access_token("spotify")
    if cached:
        return cached

    client_id = os.getenv("SPOTIFY_CLIENT_ID")
    client_secret = os.getenv("SPOTIFY_CLIENT_SECRET")
    refresh_token = os.getenv("SPOTIFY_REFRESH_TOKEN") or load_token_cache().get("spotify", {}).get("refresh_token")
    if not all([client_id, client_secret, refresh_token]):
        raise IntegrationError("Spotify is not configured.", 503)

    token = request_json(
        "https://accounts.spotify.com/api/token",
        method="POST",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    tokens = load_token_cache()
    tokens["spotify"] = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token", refresh_token),
        "expires_at": time.time() + token.get("expires_in", 3600),
    }
    save_token_cache(tokens)
    return token["access_token"]


def get_strava_access_token() -> str:
    client_id = os.getenv("STRAVA_CLIENT_ID")
    client_secret = os.getenv("STRAVA_CLIENT_SECRET")
    refresh_token = os.getenv("STRAVA_REFRESH_TOKEN") or load_token_cache().get("strava", {}).get("refresh_token")
    if not all([client_id, client_secret, refresh_token]):
        cached = get_cached_access_token("strava")
        if cached:
            return cached
        access_token = os.getenv("STRAVA_ACCESS_TOKEN")
        if access_token:
            return access_token
        raise IntegrationError("Strava is not configured.", 503)

    token = request_json(
        "https://www.strava.com/oauth/token",
        method="POST",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    tokens = load_token_cache()
    tokens["strava"] = {
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token", refresh_token),
        "expires_at": token.get("expires_at", time.time() + 21600),
    }
    save_token_cache(tokens)
    return token["access_token"]


def spotify_headers() -> dict:
    return {"Authorization": f"Bearer {get_spotify_access_token()}"}


def strava_headers() -> dict:
    return {"Authorization": f"Bearer {get_strava_access_token()}"}


def get_spotify_recent_tracks() -> list[dict]:
    data = request_json(
        "https://api.spotify.com/v1/me/player/recently-played?limit=5",
        headers=spotify_headers(),
    )
    tracks = []
    for item in data.get("items", []):
        track = item.get("track") or {}
        album = track.get("album") or {}
        tracks.append(
            {
                "name": track.get("name", ""),
                "artists": ", ".join(artist.get("name", "") for artist in track.get("artists", [])),
                "album": album.get("name", ""),
                "image": image_from(album.get("images", [])),
                "url": (track.get("external_urls") or {}).get("spotify", ""),
                "played_at": item.get("played_at", ""),
            }
        )
    return tracks


def get_spotify_playlists() -> list[dict]:
    data = request_json(
        "https://api.spotify.com/v1/me/playlists?limit=20",
        headers=spotify_headers(),
    )
    owner_id = os.getenv("SPOTIFY_USER_ID")
    playlists = []
    for item in data.get("items", []):
        owner = item.get("owner") or {}
        if item.get("public") is not True:
            continue
        if owner_id and owner.get("id") != owner_id:
            continue
        playlists.append(
            {
                "name": item.get("name", ""),
                "description": item.get("description", ""),
                "tracks": (item.get("tracks") or {}).get("total", 0),
                "image": image_from(item.get("images", [])),
                "url": (item.get("external_urls") or {}).get("spotify", ""),
            }
        )
        if len(playlists) == 6:
            break
    return playlists


def get_strava_overview() -> dict:
    athlete = request_json("https://www.strava.com/api/v3/athlete", headers=strava_headers())
    activities = request_json(
        "https://www.strava.com/api/v3/athlete/activities?per_page=5",
        headers=strava_headers(),
    )
    normalized_activities = []
    for activity in activities:
        normalized_activities.append(
            {
                "name": activity.get("name", ""),
                "sport": activity.get("sport_type") or activity.get("type", ""),
                "date": activity.get("start_date_local", ""),
                "distance": meters_to_miles(activity.get("distance")),
                "moving_time": seconds_to_label(activity.get("moving_time")),
                "elevation": meters_to_feet(activity.get("total_elevation_gain")),
                "url": f"https://www.strava.com/activities/{activity.get('id')}" if activity.get("id") else "",
            }
        )
    return {
        "athlete": {
            "name": " ".join(part for part in [athlete.get("firstname"), athlete.get("lastname")] if part),
            "city": athlete.get("city", ""),
            "state": athlete.get("state", ""),
            "country": athlete.get("country", ""),
            "image": athlete.get("profile") or athlete.get("profile_medium") or "",
            "followers": athlete.get("follower_count"),
            "friends": athlete.get("friend_count"),
            "url": f"https://www.strava.com/athletes/{athlete.get('id')}" if athlete.get("id") else "",
        },
        "latest_activity": normalized_activities[0] if normalized_activities else {},
        "activities": normalized_activities,
    }


class NoteBody(BaseModel):
    text: str


@app.get("/", response_class=RedirectResponse)
async def root():
    return RedirectResponse(url="/about")


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/{section}", response_class=HTMLResponse)
async def section_page(request: Request, section: str):
    if section not in SECTIONS:
        raise HTTPException(status_code=404, detail="Section not found")
    notes = load_notes()
    return templates.TemplateResponse(
        f"{section}.html",
        {
            "request": request,
            "active": section,
            "note_text": notes.get(section, ""),
            "image_url": get_image_url(section),
        },
    )


@app.post("/api/notes/{section}")
async def save_note(section: str, body: NoteBody):
    if section not in SECTIONS:
        raise HTTPException(status_code=404)
    notes = load_notes()
    notes[section] = body.text
    save_notes(notes)
    return {"ok": True}


@app.post("/api/images/{section}")
async def upload_image(section: str, file: UploadFile = File(...)):
    if section not in SECTIONS:
        raise HTTPException(status_code=404)
    ext = Path(file.filename or "image.png").suffix.lower()
    if ext not in IMAGE_EXTS:
        ext = ".png"
    for old_ext in IMAGE_EXTS:
        old = IMAGES_DIR / f"{section}{old_ext}"
        if old.exists():
            old.unlink()
    dest = IMAGES_DIR / f"{section}{ext}"
    with dest.open("wb") as f:
        shutil.copyfileobj(file.file, f)
    return {"ok": True, "url": f"/uploads/{section}{ext}"}


@app.delete("/api/images/{section}")
async def delete_image(section: str):
    if section not in SECTIONS:
        raise HTTPException(status_code=404)
    for ext in IMAGE_EXTS:
        path = IMAGES_DIR / f"{section}{ext}"
        if path.exists():
            path.unlink()
    return {"ok": True}


@app.get("/api/spotify")
async def spotify_overview():
    try:
        return {
            "ok": True,
            "recent_tracks": get_spotify_recent_tracks(),
            "playlists": get_spotify_playlists(),
        }
    except IntegrationError as exc:
        return {"ok": False, "message": str(exc)}


@app.get("/api/strava")
async def strava_overview():
    try:
        return {"ok": True, **get_strava_overview()}
    except IntegrationError as exc:
        return {"ok": False, "message": str(exc)}
