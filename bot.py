"""Discord video-scraper bot.

Watches configured channel(s) for video links, downloads with yt-dlp,
shrinks with ffmpeg to fit Discord's upload limit, replies with the file.
Posts live status (Downloading -> Processing -> video) in a single message.
"""

import asyncio
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections import OrderedDict
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord

# --- config (load .env before importing yt-dlp so AUTO_UPDATE can apply) -----
if os.path.exists(".env"):
    for line in open(".env", encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TOKEN = os.environ["DISCORD_TOKEN"]
# Comma-separated channel IDs to watch. Empty = every channel the bot can see.
WATCH_CHANNELS = {
    int(c) for c in os.environ.get("WATCH_CHANNELS", "").replace(" ", "").split(",") if c
}
# Discord upload limit (MB) for the server's boost tier. Free tier = 10.
MAX_MB = float(os.environ.get("DISCORD_MAX_MB", "10"))
TARGET_BYTES = int(MAX_MB * 1024 * 1024 * 0.95)  # 5% headroom for container overhead
# Skip videos longer than this (minutes) — they download huge and rarely fit.
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "30"))
# Self-heal: refresh yt-dlp on startup so site changes don't break extraction.
AUTO_UPDATE = os.environ.get("AUTO_UPDATE", "1") == "1"

# Throttle the update to once/day so frequent restarts don't pay the cost each time.
_UPDATE_MARKER = os.path.join(tempfile.gettempdir(), ".scrappy_ytdlp_update")
if AUTO_UPDATE and (not os.path.exists(_UPDATE_MARKER)
                    or time.time() - os.path.getmtime(_UPDATE_MARKER) > 86400):
    try:
        print("Updating yt-dlp…")
        subprocess.run([sys.executable, "-m", "pip", "install", "-qU", "yt-dlp"],
                       timeout=180, check=False)
        open(_UPDATE_MARKER, "w").close()
    except Exception as e:  # noqa: BLE001
        print(f"yt-dlp update skipped: {e}")

import yt_dlp  # noqa: E402  (imported after the optional self-update above)

URL_RE = re.compile(r"https?://\S+")

# Tracking/telemetry query params to strip from shared links before we touch them.
# ponytail: a known-junk denylist covers real-world shares; widen only if a real
# param sneaks through. Path-based IDs (facebook /share/r/…, youtu.be/…) are untouched.
_TRACK_RE = re.compile(r"^(utm_|ga_|_hs|mc_|pk_|hsa_|matomo_)", re.I)
_TRACK_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "twclid",
    "igshid", "igsh", "si", "spm", "scwid", "mibextid",
    "ref", "ref_src", "ref_url", "feature", "share_id",
}


def strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACK_KEYS and not _TRACK_RE.match(k)]
    return urlunsplit(parts._replace(query=urlencode(kept)))
# Format selectors tried in order until one yields a downloadable file. The
# strict mp4 pick is best for Discord; "best" is the catch-all when a site
# offers no mp4. ponytail: two rungs cover ~everything; add more only if a real
# site needs it.
FORMATS = ["bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b", "best"]

# Drop a Netscape-format cookies.txt next to bot.py to unlock age-restricted /
# login-walled / "confirm you're not a bot" videos. Gitignored, optional.
COOKIES = "cookies.txt"


def cookie_opt() -> dict:
    return {"cookiefile": COOKIES} if os.path.exists(COOKIES) else {}

# Recently handled URLs, so a reposted link isn't re-downloaded.
# ponytail: bounded in-memory LRU; resets on restart, which is fine for spam-guard.
_seen: "OrderedDict[str, None]" = OrderedDict()
_SEEN_MAX = 500


def already_seen(url: str) -> bool:
    if url in _seen:
        _seen.move_to_end(url)
        return True
    _seen[url] = None
    if len(_seen) > _SEEN_MAX:
        _seen.popitem(last=False)
    return False

intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        # surface ffmpeg/ffprobe's actual complaint, not a bare exit code
        tail = " / ".join((p.stderr or "").strip().splitlines()[-3:])
        raise RuntimeError(f"{cmd[0]} exit {p.returncode}: {tail or '(no stderr)'}")
    return p.stdout


def ffprobe_duration(path: str) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", path])
    return float(out.strip() or 0)


def probe(url: str) -> dict:
    """Validate the link is a real video without downloading. Raises
    DownloadError if it isn't (used to stay silent on non-video links)."""
    with yt_dlp.YoutubeDL({"noplaylist": True, "quiet": True, **cookie_opt()}) as ydl:
        return ydl.extract_info(url, download=False)


def download(url: str, workdir: str) -> str:
    """Download url, retrying each format selector until one works."""
    last_err: Exception | None = None
    for fmt in FORMATS:
        opts = {
            "format": fmt,
            "outtmpl": os.path.join(workdir, "raw.%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "merge_output_format": "mp4",
            **cookie_opt(),
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                raw = ydl.prepare_filename(info)
            if not os.path.exists(raw):  # merged/remuxed extension may differ
                raw = next((os.path.join(workdir, f) for f in os.listdir(workdir)), raw)
            return raw
        except yt_dlp.utils.DownloadError as e:
            last_err = e
            for f in os.listdir(workdir):  # clear partials before next attempt
                os.remove(os.path.join(workdir, f))
    raise last_err  # type: ignore[misc]


def shrink(src: str, dst: str, scale: float = 1.0) -> None:
    """Re-encode src toward TARGET_BYTES. scale<1 forces a lower bitrate."""
    duration = ffprobe_duration(src) or 1
    audio_kbps = 96
    # total_bitrate(bits/s) = TARGET_BYTES*8 / duration ; reserve audio, rest for video
    base = int((TARGET_BYTES * 8 / duration) / 1000) - audio_kbps
    video_kbps = max(150, int(base * scale))
    _run([
        "ffmpeg", "-y", "-i", src,
        # 0:V (capital) = 1st real video, skipping attached thumbnails that
        # sites like Facebook embed as a still video stream (encoding the
        # thumbnail yields frame=0 -> "Conversion failed"). audio optional.
        "-map", "0:V:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps*2}k",
        # downscale to 720p max; keeps aspect, never upscales
        "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease",
        "-c:a", "aac", "-b:a", f"{audio_kbps}k",
        "-movflags", "+faststart",
        dst,
    ])


def process(raw: str, workdir: str) -> str | None:
    """Shrink raw to fit; retry at lower bitrate if the estimate overshoots.
    Returns final path, or None if it still can't fit."""
    out = os.path.join(workdir, "small.mp4")
    scale = 1.0
    for _ in range(3):
        shrink(raw, out, scale)
        if os.path.getsize(out) <= TARGET_BYTES:
            return out
        scale *= 0.8  # VBR/container overshot the target; drop ~20% and retry
    return None


@client.event
async def on_ready():
    print(f"Logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if WATCH_CHANNELS and message.channel.id not in WATCH_CHANNELS:
        return
    m = URL_RE.search(message.content)
    if not m:
        return
    url = strip_tracking(m.group(0))
    # ponytail: skip GIFs (tenor/giphy/.gif) — not videos worth scraping/re-encoding
    if re.search(r"\.gif($|\?)|tenor\.com|giphy\.com", url, re.I):
        return
    if already_seen(url):  # reposted link, don't re-download
        return

    # Check the link first. Truly unsupported links stay silent; a video that
    # exists but is blocked (age/login/private/bot-check) gets an explanation.
    try:
        info = await asyncio.to_thread(probe, url)
    except yt_dlp.utils.DownloadError as e:
        err = str(e).lower()
        if "unsupported url" in err or "is not a valid url" in err:
            return  # not a video link, ignore quietly
        await message.reply(
            "❌ Can't fetch that — looks age-restricted, private, or it wants "
            "a login. Add a `cookies.txt` on the host to access these.",
            mention_author=False,
        )
        return
    title = (info.get("title") or "video")[:80]

    # Bail before downloading a multi-GB stream.
    duration = info.get("duration") or 0
    if duration and duration > MAX_MINUTES * 60:
        await message.reply(
            f"❌ **{title}** is {duration / 60:.0f} min — over the {MAX_MINUTES:g} min limit.",
            mention_author=False,
        )
        return

    status = await message.reply(f"⏳ Downloading **{title}**…", mention_author=False)
    workdir = tempfile.mkdtemp(prefix="scrappy_")
    try:
        raw = await asyncio.to_thread(download, url, workdir)
        if os.path.getsize(raw) <= TARGET_BYTES:
            final: str | None = raw
        else:
            await status.edit(content=f"🔧 Processing **{title}** (shrinking to fit)…")
            final = await asyncio.to_thread(process, raw, workdir)

        if final:
            # editing the status message with the file shows the "uploaded" result
            await status.edit(content="", attachments=[discord.File(final)])
        else:
            await status.edit(content=f"❌ Couldn't get **{title}** under {MAX_MB:g} MB.")
    except yt_dlp.utils.DownloadError:
        await status.edit(content="❌ Download failed (link may need login or be unavailable).")
    except Exception as e:  # noqa: BLE001
        await status.edit(content=f"❌ Failed: {e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    assert shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not on PATH"
    client.run(TOKEN)
