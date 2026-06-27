# scrappy

Discord bot: drop a video link (YouTube, Instagram, TikTok, X, Reddit, …) in a
watched channel, it downloads with yt-dlp, shrinks with ffmpeg to fit the
server's upload limit, and replies with the video file.

One file: [bot.py](bot.py). ~120 lines.

## How it works

1. `on_message` finds the first URL in messages posted to a watched channel.
2. **Probes** the link first — if it isn't a real video, the bot stays silent
   (no reply on random article links).
3. Replies "⏳ Downloading…", grabs the video with yt-dlp (tries strict mp4,
   then falls back to `best` if a site offers no mp4).
4. If over the limit, edits to "🔧 Processing…" and ffmpeg re-encodes (H.264,
   ≤720p, bitrate computed from duration to land just under the cap).
5. Edits that same message to attach the finished video. One tidy thread.

Also: skips reposted links (in-memory dedupe), refuses videos over
`MAX_MINUTES` before downloading them, and retries the shrink at a lower
bitrate if the first encode overshoots.

**Self-healing:** yt-dlp breaks when sites change their internals. The bot
runs `pip install -U yt-dlp` on startup (toggle with `AUTO_UPDATE`, throttled
to once/day), so a VPS restart picks up the fix automatically.

## Local setup

Needs **Python 3.10+** and **ffmpeg** (provides `ffmpeg` + `ffprobe`).

```bash
# 1. ffmpeg
#   Windows:  winget install Gyan.FFmpeg     (or: choco install ffmpeg)
#   macOS:    brew install ffmpeg
#   Debian:   sudo apt install ffmpeg

# 2. deps
pip install -r requirements.txt

# 3. config
cp .env.example .env        # then edit .env, paste your bot token

# 4. run
python bot.py
```

### Creating the bot token

1. https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Reset Token** → copy into `.env` as `DISCORD_TOKEN`.
3. Same tab → enable **MESSAGE CONTENT INTENT** (required, off by default).
4. **OAuth2 → URL Generator**: scope `bot`, permissions
   *Send Messages*, *Read Message History*, *Attach Files*. Open the URL to
   invite the bot to your server.
5. To restrict it to certain channels: Discord **Settings → Advanced →
   Developer Mode** on, right-click a channel → **Copy Channel ID**, put the
   IDs (comma-separated) in `WATCH_CHANNELS`. Blank = all channels.

## Free / cheap VPS hosting

Any $4–6/mo Linux box works (Hetzner CX22 ~€4, Oracle Cloud free tier, a
Raspberry Pi). Steps on a fresh Ubuntu/Debian VPS:

```bash
sudo apt update && sudo apt install -y python3-venv ffmpeg git
git clone <your-repo> scrappy && cd scrappy   # or scp the folder up
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env             # paste token, set MAX_MB
```

Keep it running with systemd so it survives reboots/crashes:

```bash
sudo tee /etc/systemd/system/scrappy.service >/dev/null <<EOF
[Unit]
Description=scrappy discord bot
After=network.target

[Service]
WorkingDirectory=$HOME/scrappy
ExecStart=$HOME/scrappy/.venv/bin/python bot.py
Restart=always
User=$USER

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl enable --now scrappy
journalctl -u scrappy -f    # view logs
```

Update later: `git pull && sudo systemctl restart scrappy`.

## Cookies (age-restricted / login-walled / "confirm you're not a bot")

YouTube age-gated videos, private/login-walled Instagram & TikTok posts, and
the "confirm you're not a bot" wall (common on datacenter/VPS IPs) all need a
logged-in session. Drop a `cookies.txt` next to `bot.py` and yt-dlp uses it
automatically — no code change. Without it, the bot replies explaining the link
is blocked.

1. On your PC, install the browser extension **"Get cookies.txt LOCALLY"**
   (Chrome/Firefox).
2. Log into the site (**use a throwaway account** — the file grants full session
   access to it).
3. On the site, click the extension → **Export** → save `cookies.txt`.
4. Put it in the bot folder on the host (e.g. GCP browser SSH → ⚙ → Upload):
   ```bash
   mv ~/cookies.txt ~/scripts/scrappy/cookies.txt
   sudo systemctl restart scrappy
   ```

`cookies.txt` is **gitignored** — it's a credential, never commit it. Cookies
expire every few weeks; re-export when videos start failing again. One file
covers YouTube, Instagram, and TikTok.

## Notes

- Free-tier servers cap uploads at **10 MB** — set `DISCORD_MAX_MB` to match
  your boost tier (L2 = 50, L3 = 100). Long 4K videos may not fit even
  re-encoded; the bot replies saying so.
- yt-dlp breaks when sites change. `AUTO_UPDATE=1` (default) refreshes it on
  each startup; otherwise `pip install -U yt-dlp` periodically.
