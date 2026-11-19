
"""
Audio-only music playlist API (NewPipe-style extraction via yt-dlp).
 
Features
  * Search songs by name            GET    /search?q=...
  * Create / list playlists         POST   /playlists, GET /playlists
  * Add song with YouTube copy link POST   /playlists/{id}/items  {"url": "..."}
  * Stream audio-only MP3           GET    /audio/{video_id}
  * M3U playlist of MP3 streams     GET    /playlists/{id}/m3u
  * Tiny web player                 GET    /
 
Requires ffmpeg on PATH (used to convert the audio stream to MP3).
"""
import logging
import os
import re
import shutil
import sqlite3
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlparse
 
import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field
 
BASE_DIR = Path(__file__).parent
# On Render, set DATA_DIR to a persistent disk mount (e.g. /data) so playlists survive redeploys.
DATA_DIR = Path(os.getenv("DATA_DIR", str(BASE_DIR)))
DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR = DATA_DIR / "audio_cache"
CACHE_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "music.db"
 
# Optional: path to a Netscape-format cookies.txt. YouTube often blocks datacenter IPs
# (like Render's) with "Sign in to confirm you're not a bot"; cookies from a logged-in
# browser can get past that. On Render add it as a Secret File and point this at it.
COOKIES_FILE = os.getenv("YTDLP_COOKIES")
 
 
def _writable_cookies() -> str | None:
    """yt-dlp rewrites its cookie file; Render's secret files are read-only, so use a copy."""
    if not COOKIES_FILE or not Path(COOKIES_FILE).exists():
        return None
    dst = Path(tempfile.gettempdir()) / "yt_cookies.txt"
    if not dst.exists() or dst.stat().st_mtime < Path(COOKIES_FILE).stat().st_mtime:
        shutil.copyfile(COOKIES_FILE, dst)
    return str(dst)
 
 
def ydl_extra() -> dict:
    path = _writable_cookies()
    return {"cookiefile": path} if path else {}
 
# Cache limits: when either is exceeded, the least recently played MP3s are deleted.
# Override without editing code, e.g. (PowerShell):  $env:CACHE_MAX_MB = "1000"
CACHE_MAX_MB = int(os.getenv("CACHE_MAX_MB", "500"))
CACHE_MAX_FILES = int(os.getenv("CACHE_MAX_FILES", "50"))
 
app = FastAPI(title="NewPipe-style Music API", version="1.0.0")
log = logging.getLogger("uvicorn.error")
 
 
# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()
 
 
def init_db() -> None:
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS playlists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id INTEGER NOT NULL REFERENCES playlists(id) ON DELETE CASCADE,
                video_id TEXT NOT NULL,
                title TEXT NOT NULL,
                uploader TEXT,
                duration INTEGER,
                thumbnail TEXT,
                position INTEGER NOT NULL,
                UNIQUE (playlist_id, video_id)
            );
            """
        )
 
 
init_db()
 
 
# --------------------------------------------------------------------------- #
# YouTube helpers (yt-dlp)
# --------------------------------------------------------------------------- #
VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
 
 
def extract_video_id(url: str) -> str:
    """Pull the 11-char video id out of any YouTube 'copy link' format."""
    url = url.strip()
    if VIDEO_ID_RE.match(url):
        return url
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    vid = None
    if host == "youtu.be":
        vid = parsed.path.lstrip("/").split("/")[0]
    elif host in YT_HOSTS:
        if parsed.path == "/watch":
            vid = parse_qs(parsed.query).get("v", [None])[0]
        else:
            m = re.match(r"^/(shorts|embed|live|v)/([\w-]{11})", parsed.path)
            vid = m.group(2) if m else None
    if not vid or not VIDEO_ID_RE.match(vid):
        raise HTTPException(400, "Not a valid YouTube video link")
    return vid
 
 
def watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"
 
 
def fetch_info(video_id: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, **ydl_extra()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(watch_url(video_id), download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(404, f"Could not fetch video: {e}") from e
 
 
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
 
 
def ensure_mp3(video_id: str) -> Path:
    """Download the audio-only stream and convert it to MP3 (cached)."""
    mp3 = CACHE_DIR / f"{video_id}.mp3"
    if mp3.exists():
        return mp3
    missing = [t for t in ("ffmpeg", "ffprobe") if not shutil.which(t)]
    if missing:
        raise HTTPException(
            500,
            f"{' and '.join(missing)} not found on PATH. Install ffmpeg (it includes ffprobe) "
            "and restart the server from a NEW terminal.",
        )
 
    with _locks_guard:
        lock = _locks.setdefault(video_id, threading.Lock())
    with lock:  # avoid downloading the same song twice concurrently
        if mp3.exists():
            return mp3
        opts = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "format": "bestaudio/best",  # audio only, no video stream
            "outtmpl": str(CACHE_DIR / "%(id)s.%(ext)s"),
            **ydl_extra(),
            "postprocessors": [
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
            ],
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([watch_url(video_id)])
        except Exception as e:  # DownloadError, PostProcessingError, etc.
            log.exception("Audio download/convert failed for %s", video_id)
            raise HTTPException(502, f"Audio failed ({type(e).__name__}): {e}") from e
    if not mp3.exists():
        raise HTTPException(500, "MP3 conversion finished but the file was not created")
    return mp3
 
 
# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class PlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
 
 
class ItemAdd(BaseModel):
    url: str = Field(description="YouTube 'copy link' URL, e.g. https://youtu.be/dQw4w9WgXcQ")
 
 
# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
@app.get("/search")
def search(q: str = Query(min_length=1), limit: int = Query(10, ge=1, le=25)):
    """Search YouTube by song name (no API key needed)."""
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True, **ydl_extra()}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(f"ytsearch{limit}:{q}", download=False)
    except yt_dlp.utils.DownloadError as e:
        raise HTTPException(502, f"Search failed: {e}") from e
    results = []
    for e in data.get("entries", []):
        if not e or not e.get("id"):
            continue
        thumbs = e.get("thumbnails") or []
        results.append(
            {
                "video_id": e["id"],
                "title": e.get("title"),
                "uploader": e.get("uploader") or e.get("channel"),
                "duration": e.get("duration"),
                "thumbnail": thumbs[-1]["url"] if thumbs else None,
                "url": watch_url(e["id"]),
            }
        )
    return {"query": q, "results": results}
 
 
# --------------------------------------------------------------------------- #
# Playlists
# --------------------------------------------------------------------------- #
@app.post("/playlists", status_code=201)
def create_playlist(body: PlaylistCreate):
    try:
        with db() as c:
            cur = c.execute("INSERT INTO playlists (name) VALUES (?)", (body.name.strip(),))
            return {"id": cur.lastrowid, "name": body.name.strip()}
    except sqlite3.IntegrityError:
        raise HTTPException(409, "A playlist with that name already exists")
 
 
@app.get("/playlists")
def list_playlists():
    with db() as c:
        rows = c.execute(
            """SELECT p.id, p.name, COUNT(i.id) AS songs
               FROM playlists p LEFT JOIN items i ON i.playlist_id = p.id
               GROUP BY p.id ORDER BY p.id"""
        ).fetchall()
    return [dict(r) for r in rows]
 
 
def _get_playlist(c: sqlite3.Connection, pid: int) -> sqlite3.Row:
    row = c.execute("SELECT * FROM playlists WHERE id = ?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "Playlist not found")
    return row
 
 
@app.get("/playlists/{pid}")
def get_playlist(pid: int, request: Request):
    base = str(request.base_url).rstrip("/")
    with db() as c:
        pl = _get_playlist(c, pid)
        rows = c.execute(
            "SELECT * FROM items WHERE playlist_id = ? ORDER BY position", (pid,)
        ).fetchall()
    items = [{**dict(r), "audio_url": f"{base}/audio/{r['video_id']}"} for r in rows]
    return {"id": pl["id"], "name": pl["name"], "items": items}
 
 
@app.delete("/playlists/{pid}", status_code=204)
def delete_playlist(pid: int):
    with db() as c:
        _get_playlist(c, pid)
        c.execute("DELETE FROM playlists WHERE id = ?", (pid,))
 
 
@app.post("/playlists/{pid}/items", status_code=201)
def add_item(pid: int, body: ItemAdd):
    """Add a song to a playlist using a YouTube 'copy link' URL."""
    video_id = extract_video_id(body.url)
    with db() as c:
        _get_playlist(c, pid)
    info = fetch_info(video_id)
    with db() as c:
        pos = c.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM items WHERE playlist_id = ?", (pid,)
        ).fetchone()[0]
        try:
            cur = c.execute(
                """INSERT INTO items
                   (playlist_id, video_id, title, uploader, duration, thumbnail, position)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    pid,
                    video_id,
                    info.get("title") or video_id,
                    info.get("uploader") or info.get("channel"),
                    int(info["duration"]) if info.get("duration") else None,
                    info.get("thumbnail"),
                    pos,
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(409, "Song is already in this playlist")
    return {"item_id": cur.lastrowid, "video_id": video_id, "title": info.get("title"), "position": pos}
 
 
@app.delete("/playlists/{pid}/items/{item_id}", status_code=204)
def remove_item(pid: int, item_id: int):
    with db() as c:
        cur = c.execute("DELETE FROM items WHERE id = ? AND playlist_id = ?", (item_id, pid))
        if cur.rowcount == 0:
            raise HTTPException(404, "Item not found")
 
 
@app.get("/playlists/{pid}/m3u", response_class=PlainTextResponse)
def playlist_m3u(pid: int, request: Request):
    """M3U file - open it in VLC or any audio player to play the whole playlist."""
    base = str(request.base_url).rstrip("/")
    with db() as c:
        pl = _get_playlist(c, pid)
        rows = c.execute(
            "SELECT * FROM items WHERE playlist_id = ? ORDER BY position", (pid,)
        ).fetchall()
    lines = ["#EXTM3U", f"#PLAYLIST:{pl['name']}"]
    for r in rows:
        lines.append(f"#EXTINF:{r['duration'] or -1},{r['title']}")
        lines.append(f"{base}/audio/{r['video_id']}")
    return PlainTextResponse("\n".join(lines) + "\n", media_type="audio/x-mpegurl")
 
 
# --------------------------------------------------------------------------- #
# Audio streaming (MP3 only)
# --------------------------------------------------------------------------- #
_prune_lock = threading.Lock()
 
 
def prune_cache(keep=None) -> None:
    """Delete least-recently-played MP3s until the cache is within its limits.
 
    `keep` is a file that must never be deleted (the one being played right now).
    Recency is tracked through file modification time, which stream_audio refreshes
    on every play.
    """
    with _prune_lock:
        entries = []
        for p in CACHE_DIR.glob("*.mp3"):
            try:
                st = p.stat()
            except OSError:
                continue
            entries.append((st.st_mtime, st.st_size, p))
        entries.sort(key=lambda e: e[0])  # oldest first
 
        total = sum(e[1] for e in entries)
        count = len(entries)
        max_bytes = CACHE_MAX_MB * 1024 * 1024
 
        for _, size, p in entries:
            if count <= CACHE_MAX_FILES and total <= max_bytes:
                break
            if keep is not None and p == keep:
                continue
            try:
                p.unlink()
            except OSError:  # e.g. file currently being streamed on Windows - skip it
                continue
            total -= size
            count -= 1
            log.info("Cache cleanup: removed %s", p.name)
 
 
prune_cache()  # also tidy up at startup
 
 
@app.get("/audio/{video_id}")
def stream_audio(video_id: str):
    """Returns the audio-only MP3 (first request downloads + converts, then cached)."""
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(400, "Invalid video id")
    mp3 = ensure_mp3(video_id)
    try:
        os.utime(mp3)  # mark as recently played
    except OSError:
        pass
    prune_cache(keep=mp3)
    # No filename= so it plays inline in <audio> instead of being sent as a download.
    return FileResponse(mp3, media_type="audio/mpeg")
 
 
@app.get("/health")
def health():
    """Quick environment check: open http://127.0.0.1:8000/health"""
    files = list(CACHE_DIR.glob("*.mp3"))
    size = 0
    for f in files:
        try:
            size += f.stat().st_size
        except OSError:
            pass
    return {
        "ffmpeg": shutil.which("ffmpeg"),
        "ffprobe": shutil.which("ffprobe"),
        "yt_dlp_version": yt_dlp.version.__version__,
        "ready_for_mp3": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        "cache": {
            "files": len(files),
            "size_mb": round(size / 1024 / 1024, 1),
            "max_files": CACHE_MAX_FILES,
            "max_mb": CACHE_MAX_MB,
        },
    }
 
 
# --------------------------------------------------------------------------- #
# Minimal web player
# --------------------------------------------------------------------------- #
PLAYER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Music Player</title>
<style>
 body{font-family:system-ui,sans-serif;max-width:720px;margin:24px auto;padding:0 16px}
 input,select,button{padding:8px;margin:2px 0;font-size:14px}
 .row{display:flex;gap:6px;flex-wrap:wrap} .row input{flex:1;min-width:160px}
 li{padding:6px 0;border-bottom:1px solid #8884;display:flex;justify-content:space-between;gap:8px}
 li span{cursor:pointer} li.playing span{font-weight:700}
 audio{width:100%;margin:12px 0}
</style></head><body>
<h2>Music Player</h2>
<div class="row"><select id="pl"></select>
 <input id="newpl" placeholder="New playlist name"><button onclick="newPl()">Create</button></div>
<div class="row"><input id="link" placeholder="Paste YouTube link"><button onclick="addLink()">Add link</button></div>
<div class="row"><input id="q" placeholder="Search song name" onkeydown="if(event.key=='Enter')search()">
 <button onclick="search()">Search</button></div>
<ul id="results"></ul>
<audio id="audio" controls></audio>
<h3>Playlist</h3><ul id="queue"></ul>
<script>
let items=[], cur=-1;
const $=id=>document.getElementById(id);
async function api(path,opt){const r=await fetch(path,opt);if(!r.ok){alert((await r.json()).detail||r.statusText);throw 0}
 return r.status==204?null:r.json()}
const post=(p,b)=>api(p,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(b)});
async function loadPls(sel){const p=await api('/playlists');$('pl').innerHTML=p.map(x=>
 `<option value="${x.id}">${x.name} (${x.songs})</option>`).join('');
 if(sel)$('pl').value=sel; loadQueue()}
async function newPl(){const n=$('newpl').value.trim();if(!n)return;const p=await post('/playlists',{name:n});$('newpl').value='';loadPls(p.id)}
async function loadQueue(){if(!$('pl').value){items=[];render();return}
 items=(await api('/playlists/'+$('pl').value)).items;render()}
function render(){$('queue').innerHTML=items.map((s,i)=>`<li class="${i==cur?'playing':''}">
 <span onclick="play(${i})">${i+1}. ${s.title}</span><button onclick="del(${s.id})">x</button></li>`).join('')}
function play(i){cur=i;$('audio').src=items[i].audio_url;$('audio').play();render()}
$('audio').onended=()=>{if(cur+1<items.length)play(cur+1)};
async function addUrl(u){const id=$('pl').value;if(!id)return alert('Create a playlist first');
 await post(`/playlists/${id}/items`,{url:u});loadPls(id)}
function addLink(){const u=$('link').value.trim();if(u){addUrl(u).then(()=>$('link').value='')}}
async function del(id){await api(`/playlists/${$('pl').value}/items/${id}`,{method:'DELETE'});loadPls($('pl').value)}
async function search(){const q=$('q').value.trim();if(!q)return;$('results').innerHTML='<li>Searching...</li>';
 const r=await api('/search?q='+encodeURIComponent(q));
 $('results').innerHTML=r.results.map(s=>`<li><span>${s.title} <small>- ${s.uploader||''}</small></span>
 <button onclick="addUrl('${s.url}')">Add</button></li>`).join('')}
$('pl').onchange=loadQueue; loadPls();
</script></body></html>"""
 
 
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def player():
    return PLAYER_HTML
 
