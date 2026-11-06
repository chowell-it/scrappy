"""Discord video-scraper bot.

Watches configured channel(s) for video links from a fixed list of social
sites, downloads with yt-dlp, shrinks with ffmpeg to fit Discord's upload
limit, replies with the file. Posts live status (Downloading -> Processing ->
video) in a single message.

Also exposes slash commands (`/help`, `/scrape`, `/watch ...`) for on-demand
scraping (video/image/gif) and per-server channel configuration.
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.request

# stdout/stderr are fully block-buffered when not attached to a terminal
# (true under systemd/journald) - every print() here would otherwise sit
# invisible in a buffer until the process exits or the buffer fills, making
# journalctl look like nothing happened for whatever triggered it. Force
# line buffering so log lines show up as they're printed.
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)
from collections import OrderedDict
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import discord
from discord import app_commands

# --- config (load .env before importing yt-dlp so AUTO_UPDATE can apply) -----
if os.path.exists(".env"):
    for line in open(".env", encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

TOKEN = os.environ["DISCORD_TOKEN"]
# Comma-separated channel IDs to watch, global fallback used when a guild
# hasn't configured its own list with /watch. Empty = watch nothing until
# /watch add is run (deny by default, not every channel).
WATCH_CHANNELS = {
    int(c) for c in os.environ.get("WATCH_CHANNELS", "").replace(" ", "").split(",") if c
}
# Discord upload limit (MB) for the server's boost tier. Free tier = 20 (raised
# from 10 in Aug 2026); boost L2 = 50, L3 = 100.
MAX_MB = float(os.environ.get("DISCORD_MAX_MB", "20"))
TARGET_BYTES = int(MAX_MB * 1024 * 1024 * 0.95)  # 5% headroom for container overhead
# Skip videos longer than this (minutes) — they download huge and rarely fit.
MAX_MINUTES = float(os.environ.get("MAX_MINUTES", "5"))
# Self-heal: refresh yt-dlp on startup so site changes don't break extraction.
AUTO_UPDATE = os.environ.get("AUTO_UPDATE", "1") == "1"
# Sync slash commands to one guild instantly (for dev); unset = global sync,
# which can take up to an hour to show up everywhere.
DEV_GUILD_ID = os.environ.get("DEV_GUILD_ID") or None

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

# The only sites scrappy will touch. Keeps it from trying (and failing oddly
# on) random video embeds elsewhere, and keeps the attack surface small.
# domain -> display name, used in "unsupported site" messages.
ALLOWED_SITES = {
    "tiktok.com": "TikTok",
    "youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "twitter.com": "Twitter/X",
    "x.com": "Twitter/X",
    "instagram.com": "Instagram",
    "facebook.com": "Facebook",
    "fb.watch": "Facebook",
    "reddit.com": "Reddit",
    "redd.it": "Reddit",
    "twitch.tv": "Twitch",
    "snapchat.com": "Snapchat",
    "pinterest.com": "Pinterest",
    "pin.it": "Pinterest",
    "tumblr.com": "Tumblr",
    "vimeo.com": "Vimeo",
    "linkedin.com": "LinkedIn",
    "threads.net": "Threads",
    "bsky.app": "Bluesky",
    "streamable.com": "Streamable",
    # Not one of the "top 15" social sites, but wants_audio()/download_audio()
    # below auto-grabs audio from these — kept allowed so that feature works.
    "soundcloud.com": "SoundCloud",
    "bandcamp.com": "Bandcamp",
}
SUPPORTED_SITES_TEXT = ", ".join(sorted(set(ALLOWED_SITES.values())))


def site_allowed(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in ALLOWED_SITES)


# Tracking/telemetry query params to strip from shared links before we touch them.
# ponytail: a known-junk denylist covers real-world shares; widen only if a real
# param sneaks through. Path-based IDs (facebook /share/r/…, youtu.be/…) are untouched.
_TRACK_RE = re.compile(r"^(utm_|ga_|_hs|mc_|pk_|hsa_|matomo_)", re.I)
_TRACK_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "twclid",
    "igshid", "igsh", "si", "spm", "scwid", "mibextid",
    "ref", "ref_src", "ref_url", "feature", "share_id",
}


# Twitter/X URLs carry a trailing /photo/N or /video/N selecting one media
# item. yt-dlp's extractor then requires that item's type to be literally
# "video" — but a tweet's GIF is internally typed "animated_gif", so e.g.
# .../status/123/photo/1 on a GIF tweet fails with "Media #1 is not a video"
# even though the same URL with the index dropped extracts it fine (it falls
# back to a lenient path that accepts video *and* animated_gif). Drop it.
_TWITTER_HOSTS = ("twitter.com", "x.com")
# Trailing slash allowed (".../photo/1/") — a bare $ anchor missed that case.
_TWITTER_MEDIA_INDEX_RE = re.compile(r"(/[^/]+/status/\d+)/(?:photo|video)/\d+/?$", re.I)


def _is_twitter_host(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _TWITTER_HOSTS)


def strip_tracking(url: str) -> str:
    parts = urlsplit(url)
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k.lower() not in _TRACK_KEYS and not _TRACK_RE.match(k)]
    path = parts.path
    if _is_twitter_host((parts.hostname or "").lower()):
        path = _TWITTER_MEDIA_INDEX_RE.sub(r"\1", path)
    return urlunsplit(parts._replace(path=path, query=urlencode(kept)))
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


# X's default GraphQL API (yt-dlp's default) has been increasingly incomplete
# for guest/unauthenticated requests - some tweets' animated_gif media come
# back with no video_info at all over GraphQL, even though the same content
# is publicly visible on the logged-out page. The older "legacy" API tends to
# be more complete for exactly this case. Harmless no-op for every other site.
EXTRACTOR_ARGS = {"extractor_args": {"twitter": {"api": ["legacy"]}}}

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

# Matches yt-dlp's "No suitable extractor found for URL {url}" message. Used to
# tell a shortlink whose redirect target has no extractor (e.g. a TikTok
# vm.tiktok.com/t/... link landing on a photo/slideshow post, which yt-dlp
# can't turn into a video) apart from a plain non-video webpage (no redirect,
# same URL, stays silent).
_NO_EXTRACTOR_RE = re.compile(r"no suitable extractor found for url (\S+)")


def classify_download_error(e: Exception, url: str) -> Optional[str]:
    """Turn a DownloadError into a user-facing message, or None to stay silent."""
    err = str(e).lower()
    # Only speak up when the link IS a video but is auth-walled. Everything
    # else (plain webpages, dead links, scrape failures) stays silent.
    auth = ("sign in", "log in", "login", "private", "age-restrict",
            "confirm your age", "not a bot", "members-only", "403")
    if any(a in err for a in auth):
        return ("❌ Can't fetch that — looks age-restricted, private, or it wants "
                "a login. Add a `cookies.txt` on the host to access these.")
    no_extractor = _NO_EXTRACTOR_RE.search(err)
    if no_extractor and no_extractor.group(1).rstrip(".,)") != url.lower():
        # The shortlink's redirect resolved fine, but the landing page (e.g. a
        # TikTok photo/slideshow post) isn't something yt-dlp can pull a video
        # from — say so instead of looking like the redirect itself failed.
        return ("❌ That link redirects to a page with no downloadable video "
                "(likely a photo/slideshow post, not a real video).")
    return None


# --- per-guild channel configuration (persisted across restarts) ------------
WATCH_CONFIG_PATH = "watch_channels.json"


def _load_watch_config() -> dict:
    if not os.path.exists(WATCH_CONFIG_PATH):
        return {}
    try:
        with open(WATCH_CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"watch_channels.json unreadable ({e}), starting fresh")
        return {}


def _save_watch_config(cfg: dict) -> None:
    tmp = WATCH_CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    os.replace(tmp, WATCH_CONFIG_PATH)  # atomic; a crash mid-write can't corrupt the file


_watch_config: dict = _load_watch_config()


def channel_allowed(guild_id: Optional[int], channel_id: int) -> bool:
    if guild_id is None:  # DMs: no per-guild config applies
        return True
    configured = _watch_config.get(str(guild_id))
    if configured:
        return channel_id in configured
    if WATCH_CHANNELS:  # legacy global env fallback
        return channel_id in WATCH_CHANNELS
    # Deny by default: a server that hasn't run /watch add (and has no
    # WATCH_CHANNELS set) gets no auto-watching anywhere, rather than every
    # channel the bot can see.
    return False


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


def _run(cmd: list[str], timeout: int = 600) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        # surface ffmpeg/ffprobe's actual complaint, not a bare exit code
        tail = " / ".join((p.stderr or "").strip().splitlines()[-6:])
        raise RuntimeError(f"{cmd[0]} exit {p.returncode}: {tail or '(no stderr)'}")
    return p.stdout


def ffprobe_duration(path: str) -> float:
    out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", path])
    return float(out.strip() or 0)


def first_entry(info: dict) -> dict:
    """A post holding several videos (e.g. a tweet with 2 clips) comes back as a
    playlist even with noplaylist — that flag only suppresses real playlists.
    Descend to the first entry so the rest of the pipeline sees one video.
    ponytail: first clip only; revisit if anyone asks for the whole set."""
    while info.get("_type") == "playlist":
        entries = list(info.get("entries") or [])
        if not entries:
            raise yt_dlp.utils.DownloadError("Unsupported URL: no video content")
        info = entries[0]
    return info


def probe(url: str) -> dict:
    """Validate the link is a real video without downloading. Raises
    DownloadError if it isn't (used to stay silent on non-video links)."""
    print(f"[probe] {url}")  # ground truth for what yt-dlp actually received
    with yt_dlp.YoutubeDL({"noplaylist": True, "quiet": True, "socket_timeout": 30,
                           "playlist_items": "1",  # multi-video post: only the first
                           # -generic: don't scrape random webpages for og:video etc.
                           # Only sites with a real extractor pass; the rest raise
                           # "Unsupported URL" and stay silent.
                           "allowed_extractors": ["default", "-generic"],
                           **cookie_opt(), **EXTRACTOR_ARGS}) as ydl:
        info = first_entry(ydl.extract_info(url, download=False))
    # belt-and-suspenders: a real video has a duration or a video-bearing format
    if not (info.get("duration") or any(
            f.get("vcodec") not in (None, "none") for f in info.get("formats", []))):
        raise yt_dlp.utils.DownloadError("Unsupported URL: no video content")
    return info


def download(url: str, workdir: str) -> str:
    """Download url, retrying each format selector until one works."""
    last_err: Exception | None = None
    for fmt in FORMATS:
        opts = {
            "format": fmt,
            "outtmpl": os.path.join(workdir, "raw.%(ext)s"),
            "noplaylist": True,
            "playlist_items": "1",  # multi-video post: only the first
            "quiet": True,
            "socket_timeout": 30,  # fail a stalled connection instead of hanging
            "merge_output_format": "mp4",
            **cookie_opt(), **EXTRACTOR_ARGS,
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = first_entry(ydl.extract_info(url, download=True))
                raw = ydl.prepare_filename(info)
            if not os.path.exists(raw):  # merged/remuxed extension may differ
                raw = next((os.path.join(workdir, f) for f in os.listdir(workdir)), raw)
            return raw
        except yt_dlp.utils.DownloadError as e:
            last_err = e
            for f in os.listdir(workdir):  # clear partials before next attempt
                os.remove(os.path.join(workdir, f))
    raise last_err  # type: ignore[misc]


# Hosts that are audio-first — auto-download as mp3 unless a tag says otherwise.
_AUDIO_HOSTS = re.compile(r"soundcloud\.com|bandcamp\.com|music\.youtube\.com", re.I)
# Explicit override in the message: $audio / $video / !audio / !video (tag wins).
_MODE_TAG = re.compile(r"[!$](audio|video)\b", re.I)


def wants_audio(content: str, url: str) -> bool:
    tag = _MODE_TAG.search(content)
    if tag:
        return tag.group(1).lower() == "audio"
    return bool(_AUDIO_HOSTS.search(url))


def download_audio(url: str, workdir: str) -> str:
    """Grab bestaudio as mp3 with embedded metadata + cover art."""
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(workdir, "audio.%(ext)s"),
        "noplaylist": True,
        "playlist_items": "1",  # multi-track post: only the first
        "quiet": True,
        "socket_timeout": 30,
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"},
            {"key": "FFmpegMetadata"},
            {"key": "EmbedThumbnail"},
        ],
        **cookie_opt(), **EXTRACTOR_ARGS,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.extract_info(url, download=True)
    mp3 = next((os.path.join(workdir, f) for f in os.listdir(workdir)
                if f.lower().endswith(".mp3")), None)
    if not mp3:
        raise RuntimeError("audio extraction produced no mp3")
    return mp3


# GIF distribution services — explicitly blocked (they embed natively; no reupload).
_GIF_SERVICES = re.compile(r"tenor\.com|giphy\.com|klipy\.", re.I)


def is_direct_gif(url: str) -> bool:
    """True for a direct .gif file link (e.g. a Twitter/social CDN), not a gif-service page."""
    return urlsplit(url).path.lower().endswith(".gif")


def fetch_gif(url: str, workdir: str) -> str:
    """Download the raw .gif so we can reupload it into the chat as an animated GIF."""
    dst = os.path.join(workdir, "out.gif")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r, open(dst, "wb") as f:
        shutil.copyfileobj(r, f)
    return dst


def shrink(src: str, dst: str, scale: float = 1.0) -> None:
    """Re-encode src toward TARGET_BYTES. scale<1 forces a lower bitrate."""
    duration = ffprobe_duration(src) or 1
    audio_kbps = 96
    # total_bitrate(bits/s) = TARGET_BYTES*8 / duration ; reserve audio, rest for video
    base = int((TARGET_BYTES * 8 / duration) / 1000) - audio_kbps
    video_kbps = max(150, int(base * scale))
    _run([
        "ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error", "-i", src,
        # 0:V (capital) = 1st real video, skipping attached thumbnails that
        # sites like Facebook embed as a still video stream (encoding the
        # thumbnail yields frame=0 -> "Conversion failed"). audio optional.
        "-map", "0:V:0", "-map", "0:a:0?",
        "-c:v", "libx264", "-preset", "superfast",
        "-b:v", f"{video_kbps}k", "-maxrate", f"{video_kbps}k", "-bufsize", f"{video_kbps*2}k",
        # downscale to 720p max; keeps aspect, never upscales
        # downscale to 720p max (keeps aspect, never upscales), then force both
        # dims even — libx264 rejects odd width/height (e.g. 405x720)
        "-vf", "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease,"
               "scale=trunc(iw/2)*2:trunc(ih/2)*2",
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


async def grab_video(url: str, workdir: str, on_processing=None) -> str | None:
    """Download then shrink-if-needed. Returns the final playable path, or
    None if it still doesn't fit under MAX_MB after shrinking."""
    raw = await asyncio.to_thread(download, url, workdir)
    if os.path.getsize(raw) <= TARGET_BYTES:
        return raw
    if on_processing:
        await on_processing()
    return await asyncio.to_thread(process, raw, workdir)


# --- image / gif grabbing ----------------------------------------------------
# Deliberately simple: direct image/gif links download as-is; anything else
# goes through yt-dlp and we pull whatever thumbnail/image URLs it surfaced.
# This covers the common cases (a direct media link, or a post yt-dlp already
# has thumbnail metadata for) — it isn't a full gallery scraper.
_IMG_EXT_RE = re.compile(r"\.(jpe?g|png|webp|gif)(?:$|\?)", re.I)
MAX_IMAGES = 10


def _fetch_bytes(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (fixed http(s) scheme from yt-dlp/our own regex)
        return resp.read()


def fetch_images(url: str, workdir: str, want_gif: bool) -> list[str]:
    """Grab image(s)/gif(s) referenced by url. Returns saved file paths."""
    candidates: list[str] = []
    if _IMG_EXT_RE.search(url):
        candidates = [url]
    else:
        with yt_dlp.YoutubeDL({"quiet": True, "socket_timeout": 30,
                               "allowed_extractors": ["default", "-generic"],
                               **cookie_opt(), **EXTRACTOR_ARGS}) as ydl:
            info = ydl.extract_info(url, download=False)
        for entry in info.get("entries") or [info]:
            thumbs = entry.get("thumbnails") or []
            if thumbs:
                candidates.append(thumbs[-1]["url"])  # last = highest-res
            elif entry.get("url") and _IMG_EXT_RE.search(entry["url"]):
                candidates.append(entry["url"])

    if want_gif:
        candidates = [u for u in candidates if u.lower().split("?")[0].endswith(".gif")]

    paths = []
    for i, u in enumerate(candidates[:MAX_IMAGES]):
        m = _IMG_EXT_RE.search(u)
        ext = m.group(1).lower() if m else "jpg"
        try:
            data = _fetch_bytes(u)
        except Exception as e:  # noqa: BLE001 — one bad image shouldn't sink the rest
            print(f"[fetch_images] skipping {u}: {e}")
            continue
        path = os.path.join(workdir, f"image_{i}.{ext}")
        with open(path, "wb") as f:
            f.write(data)
        paths.append(path)
    return paths


@client.event
async def on_ready():
    try:
        if DEV_GUILD_ID:
            guild = discord.Object(id=int(DEV_GUILD_ID))
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        else:
            await tree.sync()
    except Exception as e:  # noqa: BLE001 — don't let a sync hiccup kill startup
        print(f"Slash command sync failed: {e}")
    print(f"Logged in as {client.user}")


@client.event
async def on_error(event: str, *args, **kwargs):
    # discord.py's default handler already logs to stderr; this just makes
    # sure a bug in one handler can't take the whole bot down silently.
    print(f"[on_error] unhandled exception in {event}:")
    traceback.print_exc()


@client.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not channel_allowed(message.guild.id if message.guild else None, message.channel.id):
        return
    m = URL_RE.search(message.content)
    if not m:
        return
    url = strip_tracking(m.group(0))
    # GIF-hosting services (Tenor/Giphy/Klipy) embed natively in Discord — leave
    # them. A *direct* .gif file is handled below instead; it's a raw media
    # file (identified by extension), not a site to run through the allowlist
    # — the CDN host serving it (e.g. Twitter's pbs.twimg.com) usually isn't
    # one of ALLOWED_SITES anyway.
    if _GIF_SERVICES.search(url):
        return
    is_gif = is_direct_gif(url)
    if not is_gif and not site_allowed(url):
        return
    if already_seen(url):  # reposted link, don't re-download
        return

    title = "gif"
    if not is_gif:
        # Check the link first. Truly unsupported links stay silent; a video that
        # exists but is blocked (age/login/private/bot-check) gets an explanation.
        try:
            info = await asyncio.to_thread(probe, url)
        except yt_dlp.utils.DownloadError as e:
            msg = classify_download_error(e, url)
            if msg:
                await message.reply(msg, mention_author=False)
            return
        except Exception as e:  # noqa: BLE001 — surface real bugs instead of going silent
            print(f"[probe] unexpected error on {url}: {e}")
            await message.reply(f"❌ Something went wrong checking that link: {e}",
                                 mention_author=False)
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

    # plain send, not a reply — original message gets deleted on success and a
    # reply would leave a broken "original message deleted" reference
    status = await message.channel.send(f"⏳ Downloading **{title}**…")
    workdir = tempfile.mkdtemp(prefix="scrappy_")
    try:
        t = time.time()
        final: str | None
        if is_gif:
            raw = await asyncio.to_thread(fetch_gif, url, workdir)
            sz = os.path.getsize(raw)
            print(f"[timing] gif {time.time()-t:.1f}s, {sz/1e6:.1f}MB")
            # keep it a .gif; can't shrink without a gif encoder, so fits or it doesn't
            final = raw if sz <= TARGET_BYTES else None
        elif wants_audio(message.content, url):
            raw = await asyncio.to_thread(download_audio, url, workdir)
            sz = os.path.getsize(raw)
            print(f"[timing] audio {time.time()-t:.1f}s, {sz/1e6:.1f}MB ({title})")
            # mp3 can't be shrunk by the video re-encoder; fits or it doesn't
            final = raw if sz <= TARGET_BYTES else None
        else:
            async def mark_processing():
                await status.edit(content=f"🔧 Processing **{title}** (shrinking to fit)…")

            final = await grab_video(url, workdir, mark_processing)
            print(f"[timing] video {time.time()-t:.1f}s ({title})")

        if final:
            # editing the status message with the file shows the "uploaded" result
            # -# = small grey subtext; backticks make the link plain/copyable, not a hyperlink
            await status.edit(content=f"-# 📎 Sent from {message.author.mention} `{url}`",
                              attachments=[discord.File(final)])
            # ponytail: needs Manage Messages perm; ignore if we can't delete
            try:
                await message.delete()
            except discord.Forbidden:
                pass
        else:
            await status.edit(content=f"❌ Couldn't get **{title}** under {MAX_MB:g} MB.")
    except yt_dlp.utils.DownloadError:
        await status.edit(content="❌ Download failed (link may need login or be unavailable).")
    except discord.HTTPException as e:
        await status.edit(content=f"❌ Discord rejected the upload: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"[on_message] unexpected error on {url}: {e}")
        await status.edit(content=f"❌ Failed: {e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --- slash commands -----------------------------------------------------------

@tree.command(name="help", description="Show what scrappy can do")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**scrappy** grabs media from links so you don't have to.\n\n"
        "- Drop a link in a watched channel and it auto-downloads the video.\n"
        f"- `/scrape url format:` grab a video, image, or gif on demand.\n"
        f"- `/watch add|remove|list` configure which channels it watches "
        "(needs Manage Channels).\n\n"
        f"Supported sites: {SUPPORTED_SITES_TEXT}.\n"
        f"Videos longer than {MAX_MINUTES:g} min are skipped.",
        ephemeral=True,
    )


@tree.command(name="scrape", description="Grab a video, image, or gif from a link")
@app_commands.describe(url="Link to a post on a supported site",
                        format="What to grab (default: video)")
@app_commands.choices(format=[
    app_commands.Choice(name="video", value="video"),
    app_commands.Choice(name="image", value="image"),
    app_commands.Choice(name="gif", value="gif"),
])
async def scrape_cmd(interaction: discord.Interaction, url: str,
                      format: Optional[app_commands.Choice[str]] = None):
    # No channel_allowed() gate here on purpose: /watch controls passive
    # auto-watching of posted links, not this explicit on-demand command —
    # it works in any channel the bot can see and post in.
    url = strip_tracking(url.strip())
    fmt = format.value if format else "video"
    # A direct image/gif file (e.g. a CDN link) is identified by extension, not
    # by site — same reasoning as the passive-watching gif bypass above.
    is_direct_media = fmt != "video" and _IMG_EXT_RE.search(url)
    if not is_direct_media and not site_allowed(url):
        await interaction.response.send_message(
            f"❌ Unsupported site. Supported: {SUPPORTED_SITES_TEXT}.", ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    workdir = tempfile.mkdtemp(prefix="scrappy_")
    try:
        if fmt == "video":
            try:
                info = await asyncio.to_thread(probe, url)
            except yt_dlp.utils.DownloadError as e:
                msg = classify_download_error(e, url)
                if not msg:
                    print(f"[/scrape video] probe failed for {url}: {e}")
                    msg = "❌ Couldn't find a video at that link."
                await interaction.followup.send(msg)
                return
            title = (info.get("title") or "video")[:80]
            duration = info.get("duration") or 0
            if duration and duration > MAX_MINUTES * 60:
                await interaction.followup.send(
                    f"❌ **{title}** is {duration / 60:.0f} min — over the "
                    f"{MAX_MINUTES:g} min limit.")
                return
            final = await grab_video(url, workdir)
            if not final:
                await interaction.followup.send(f"❌ Couldn't get **{title}** under {MAX_MB:g} MB.")
                return
            await interaction.followup.send(content=f"**{title}**", file=discord.File(final))
        elif fmt == "image":
            paths = await asyncio.to_thread(fetch_images, url, workdir, want_gif=False)
            if not paths:
                await interaction.followup.send("❌ Couldn't find any images at that link.")
                return
            await interaction.followup.send(files=[discord.File(p) for p in paths])
        else:  # gif
            paths = await asyncio.to_thread(fetch_images, url, workdir, want_gif=True)
            if paths:
                await interaction.followup.send(files=[discord.File(p) for p in paths])
                return
            # No literal .gif file/thumbnail found — on most sites (Twitter,
            # Reddit, Tumblr...) what displays as a "gif" is actually encoded
            # as a short looping mp4 with no .gif anywhere. Fall back to
            # grabbing it as video; Discord renders a short looping mp4 the
            # same way it renders a gif.
            try:
                info = await asyncio.to_thread(probe, url)
            except yt_dlp.utils.DownloadError as e:
                msg = classify_download_error(e, url)
                if msg:
                    await interaction.followup.send(msg)
                    return
                print(f"[/scrape gif-fallback] probe failed for {url}: {e}")
                # Truly no video/gif data at all (e.g. the post is just a
                # static photo despite looking animated in the site's UI) —
                # last resort: send the image instead of failing outright.
                img_paths = await asyncio.to_thread(fetch_images, url, workdir, want_gif=False)
                if not img_paths:
                    await interaction.followup.send("❌ Couldn't find a gif at that link.")
                    return
                await interaction.followup.send(
                    content="-# Not actually animated — sending the image instead.",
                    files=[discord.File(p) for p in img_paths])
                return
            duration = info.get("duration") or 0
            if duration and duration > MAX_MINUTES * 60:
                await interaction.followup.send(
                    f"❌ That's {duration / 60:.0f} min — not really a gif, and over the "
                    f"{MAX_MINUTES:g} min limit.")
                return
            final = await grab_video(url, workdir)
            if not final:
                await interaction.followup.send(f"❌ Couldn't get that under {MAX_MB:g} MB.")
                return
            await interaction.followup.send(file=discord.File(final))
    except yt_dlp.utils.DownloadError as e:
        msg = classify_download_error(e, url) or f"❌ Couldn't fetch that: {e}"
        await interaction.followup.send(msg)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Discord rejected the upload: {e}")
    except Exception as e:  # noqa: BLE001
        print(f"[/scrape] unexpected error on {url}: {e}")
        await interaction.followup.send(f"❌ Failed: {e}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


watch_group = app_commands.Group(
    name="watch", description="Configure which channels scrappy watches for links")


@watch_group.command(name="add", description="Make scrappy watch a channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def watch_add(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    cfg = _watch_config.setdefault(guild_id, [])
    if channel.id in cfg:
        await interaction.response.send_message(
            f"{channel.mention} is already watched.", ephemeral=True)
        return
    cfg.append(channel.id)
    _save_watch_config(_watch_config)
    await interaction.response.send_message(f"✅ Now watching {channel.mention}.", ephemeral=True)


@watch_group.command(name="remove", description="Stop scrappy watching a channel")
@app_commands.checks.has_permissions(manage_channels=True)
async def watch_remove(interaction: discord.Interaction, channel: discord.TextChannel):
    guild_id = str(interaction.guild_id)
    cfg = _watch_config.get(guild_id, [])
    if channel.id not in cfg:
        await interaction.response.send_message(
            f"{channel.mention} isn't on the watch list.", ephemeral=True)
        return
    cfg.remove(channel.id)
    _save_watch_config(_watch_config)
    await interaction.response.send_message(f"✅ Stopped watching {channel.mention}.",
                                              ephemeral=True)


@watch_group.command(name="list", description="Show which channels scrappy is watching")
async def watch_list(interaction: discord.Interaction):
    cfg = _watch_config.get(str(interaction.guild_id), [])
    if cfg:
        mentions = ", ".join(f"<#{c}>" for c in cfg)
        await interaction.response.send_message(f"Watching: {mentions}", ephemeral=True)
    elif WATCH_CHANNELS:
        await interaction.response.send_message(
            "No per-server config; falling back to the bot's global "
            "`WATCH_CHANNELS` env setting.", ephemeral=True)
    else:
        await interaction.response.send_message(
            "No channels configured — not auto-watching anywhere in this server. "
            "Run `/watch add` on a channel to turn it on there.", ephemeral=True)


tree.add_command(watch_group)


@tree.error
async def on_app_command_error(interaction: discord.Interaction,
                                error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        msg = "❌ You need the **Manage Channels** permission to do that."
    else:
        print(f"[slash command error] {error}")
        msg = f"❌ Something went wrong: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    assert shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg/ffprobe not on PATH"
    client.run(TOKEN)
