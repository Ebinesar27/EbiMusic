# NewPipe-style Music API (FastAPI)

Search songs by name, add them to playlists from a YouTube "copy link", and play
**audio-only MP3** from the playlist. Extraction uses `yt-dlp` (the Python
counterpart of NewPipe's extractor) - no API key needed.

## Setup

```bash
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
# ffmpeg is required for MP3 conversion:
#   Ubuntu: sudo apt install ffmpeg | macOS: brew install ffmpeg | Windows: winget install ffmpeg
uvicorn main:app --reload
```

- Web player: http://127.0.0.1:8000/
- Swagger docs: http://127.0.0.1:8000/docs

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/search?q=song name&limit=10` | Search by song name |
| POST | `/playlists` `{"name": "Favs"}` | Create playlist |
| GET | `/playlists` | List playlists |
| GET | `/playlists/{id}` | Playlist with songs + `audio_url` |
| DELETE | `/playlists/{id}` | Delete playlist |
| POST | `/playlists/{id}/items` `{"url": "https://youtu.be/..."}` | Add song via copy link |
| DELETE | `/playlists/{id}/items/{item_id}` | Remove song |
| GET | `/audio/{video_id}` | Stream MP3 (audio only, cached in `audio_cache/`) |
| GET | `/playlists/{id}/m3u` | M3U file - open in VLC to play whole playlist |

## Example

```bash
curl -X POST localhost:8000/playlists -H 'Content-Type: application/json' -d '{"name":"Favs"}'
curl -X POST localhost:8000/playlists/1/items -H 'Content-Type: application/json' \
     -d '{"url":"https://youtu.be/dQw4w9WgXcQ"}'
curl localhost:8000/search?q=kadhal+rojave
```

Accepted link formats: `youtu.be/ID`, `youtube.com/watch?v=ID`, `music.youtube.com/watch?v=ID`, `/shorts/ID`, `/embed/ID`.

## Notes

- The first play of a song downloads and converts it (a few seconds); later plays are instant from cache.
- The cache auto-cleans: when it exceeds 50 songs or 500 MB, the least recently played MP3s are deleted
  (the song being played is never deleted). Change the limits with environment variables, e.g.
  PowerShell: `$env:CACHE_MAX_MB = "1000"; $env:CACHE_MAX_FILES = "100"` before starting uvicorn.
  Check usage at `/health`.
- Keep yt-dlp updated (`pip install -U yt-dlp`) - YouTube changes often.
- For personal/educational use; respect YouTube's Terms of Service and copyright.

## Deploy on Render (Docker + ffmpeg)

1. Push these files to a GitHub repo: `main.py`, `requirements.txt`, `Dockerfile`, `.dockerignore`, `render.yaml`.
2. Render dashboard -> **New** -> **Blueprint** -> pick the repo (it reads `render.yaml`).
   Or **New -> Web Service** -> Runtime **Docker**, and set the env vars below manually.
3. Open `https://<your-app>.onrender.com/health` - `ready_for_mp3` should be `true`.

Environment variables: `DATA_DIR` (persistent disk path, `/data`), `CACHE_MAX_MB`, `CACHE_MAX_FILES`,
`YTDLP_COOKIES` (optional path to a cookies file).

Notes:
- **Free plan:** no persistent disk, so playlists (`music.db`) and cached MP3s reset on every redeploy/restart
  and the service sleeps when idle. Remove the `disk:` block and `DATA_DIR` if you use it. A paid plan with
  a disk keeps playlists.
- **YouTube may block Render's IPs** ("Sign in to confirm you're not a bot"). If `/search` or `/audio/...` fails
  like that, export a `cookies.txt` from a logged-in browser (a "Get cookies.txt" extension), add it in Render
  -> Environment -> **Secret Files** as `cookies.txt`, and set `YTDLP_COOKIES=/etc/secrets/cookies.txt`.
  Use a throwaway Google account, since accounts used this way can get flagged.
- Test locally first: `docker build -t music-api . && docker run -p 8000:8000 music-api`.
- The app has no login, so anyone with the URL can use it. Keep the URL private or add authentication before sharing.
#
