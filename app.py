import re
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, Form, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="YT to WAV (mono 22050Hz)")

# Accept youtube.com, music.youtube.com, youtu.be, shorts, etc.
YT_RE = re.compile(
    r"^https?://(www\.|music\.|m\.)?(youtube\.com/(watch|shorts|embed|live)|youtu\.be/)",
    re.IGNORECASE,
)

MAX_DURATION_SEC = 15 * 60  # 15 min cap to stay within Render free timeouts


class ConvertRequest(BaseModel):
    url: str


def is_valid_yt_url(url: str) -> bool:
    return bool(YT_RE.match(url.strip()))


def cleanup_path(p: Path):
    shutil.rmtree(p, ignore_errors=True)


@app.get("/health"):
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse):
    return """
    <html><head><title>YT to WAV</title></head>
    <body style="font-family:sans-serif;max-width:600px;margin:40px auto">
      <h2>YouTube to WAV (mono, 22050 Hz)</h2>
      <form method="post" action="/convert-form">
        <input name="url" placeholder="https://www.youtube.com/watch?v=..." style="width:100%;padding:8px" />
        <br/><br/>
        <button type="submit">Convert & Download</button>
      </form>
      <p>API usage:</p>
      <pre>POST /convert\n{"url": "https://www.youtube.com/watch?v=..."}</pre>
      <p>Output: 16-bit PCM WAV, 1 channel, 22050 Hz.</p>
    </body></html>
    """


async def convert_youtube_to_wav(url: str, workdir: Path) -> Path:
    # 1. Download best audio with yt-dlp
    download_tpl = str(workdir / "input.%(ext)s")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": download_tpl,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        # Bypass some bot checks; user-agent helps on Render
        "user_agent": "Mozilla/5.0",
        "retries": 3,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"yt-dlp download failed: {e}")

    if not info:
        raise HTTPException(status_code=400, detail="Could not fetch video info")

    duration = info.get("duration") or 0
    if duration > MAX_DURATION_SEC:
        raise HTTPException(
            status_code=400,
            detail=f"Video too long ({duration}s). Max is {MAX_DURATION_SEC}s.",
        )

    # Find downloaded file
    inputs = list(workdir.glob("input.*"))
    if not inputs:
        raise HTTPException(status_code=500, detail="Download succeeded but no file found")
    input_file = inputs[0]

    # Sanitize title for output name
    title = re.sub(r"[^a-zA-Z0-9-_ ]", "", info.get("title", "audio"))[:50].strip() or "audio"
    output_file = workdir / f"{title}-mono-22050.wav"

    # 2. Convert with ffmpeg: mono, 22050 Hz, 16-bit PCM (good quality WAV)
    cmd = [
        "ffmpeg",
        "-y",
        "-i", str(input_file),
        "-ac", "1",          # mono
        "-ar", "22050",      # 22050 Hz
        "-c:a", "pcm_s16le", # 16-bit PCM, lossless = "good quality" for WAV
        str(output_file),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        raise HTTPException(status_code=500, detail=f"ffmpeg failed: {e.stderr[-2000:]}")
    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="ffmpeg not found in PATH")

    if not output_file.exists():
        raise HTTPException(status_code=500, detail="Conversion failed, no output file")

    # Remove original download to save space
    try:
        input_file.unlink()
    except Exception:
        pass

    return output_file


@app.post("/convert")
async def convert(req: ConvertRequest, background_tasks: BackgroundTasks):
    url = req.url.strip()
    if not is_valid_yt_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    workdir = Path(tempfile.gettempdir()) / f"ytwav-{uuid.uuid4().hex}"
    workdir.mkdir(parents=True, exist_ok=True)
    output_file = await convert_youtube_to_wav(url, workdir)
    background_tasks.add_task(cleanup_path, workdir)
    return FileResponse(
        path=str(output_file),
        media_type="audio/wav",
        filename=output_file.name,
    )


# Simple HTML form support (no JS needed)
@app.post("/convert-form")
async def convert_form(background_tasks: BackgroundTasks, url: str = Form(...)):
    url = url.strip()
    if not is_valid_yt_url(url):
        raise HTTPException(status_code=400, detail="Invalid YouTube URL")
    workdir = Path(tempfile.gettempdir()) / f"ytwav-{uuid.uuid4().hex}"
    workdir.mkdir(parents=True, exist_ok=True)
    output_file = await convert_youtube_to_wav(url, workdir)
    background_tasks.add_task(cleanup_path, workdir)
    return FileResponse(
        path=str(output_file),
        media_type="audio/wav",
        filename=output_file.name,
    )
