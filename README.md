# scrappy

Discord bot: drop a video link from a supported social site in a watched
channel, it downloads with yt-dlp, shrinks with ffmpeg to fit the server's
upload limit, and replies with the video file. Also works on demand via slash
commands, including grabbing images/gifs instead of video.

One file: [bot.py](bot.py).

## Supported sites

TikTok, YouTube, Twitter/X, Instagram, Facebook, Reddit, Twitch, Snapchat,
Pinterest, Tumblr, Vimeo, LinkedIn, Threads, Bluesky, Streamable. Links to
anything else are ignored. (Edit `ALLOWED_SITES` in `bot.py` to change the list.)

SoundCloud and Bandcamp are also allowed — not "top 15 social" sites, but
needed for the audio auto-download behavior below.

## How it works

1. `on_message` finds the first URL in messages posted to a watched channel,
   and checks it's one of the supported sites above (direct image/gif file
   links are the one exception — see below).
2. **Probes** the link first — if it isn't a real video, the bot stays silent
   (no reply on random article links); if it's a shortlink that redirects to
   something with no video (e.g. a TikTok photo post), it says so. A post
   with multiple videos/clips (e.g. a tweet with 2 clips) is treated as its
   first clip only.
3. Refuses videos over `MAX_MINUTES` (default 5) before downloading them.
4. Replies "⏳ Downloading…", grabs the video with yt-dlp (tries strict mp4,
   then falls back to `best` if a site offers no mp4).
5. If over the limit, edits to "🔧 Processing…" and ffmpeg re-encodes (H.264,
   ≤720p, bitrate computed from duration to land just under the cap), retrying
   at a lower bitrate if the first encode overshoots.
6. Edits that same message to attach the finished video. One tidy thread.

**Audio-only:** SoundCloud, Bandcamp, and `music.youtube.com` links are
auto-downloaded as mp3 (with embedded cover art/metadata) instead of video.
Override either way on any link by tagging the message with `$audio`/`$video`
(or `!audio`/`!video`).

**GIFs:** a link to a GIF-*hosting service* (Tenor, Giphy, Klipy) is left
alone — Discord already embeds those natively. A *direct* `.gif` file link
(e.g. from a tweet's media CDN) is reposted as-is instead of going through
yt-dlp.

Also skips reposted links (in-memory dedupe). Unexpected failures (network
errors, ffmpeg bugs, Discord upload rejections) are caught and reported in
the channel instead of failing silently.

**Self-healing:** yt-dlp breaks when sites change their internals. The bot
runs `pip install -U yt-dlp` on startup (toggle with `AUTO_UPDATE`, throttled
to once/day), so a VPS restart picks up the fix automatically.

## Slash commands

- `/help` — what the bot does, supported sites, current limits.
- `/scrape url format:` — grab a video (default), image, or gif from a
  supported link on demand, regardless of the dedupe/auto-watch flow.
- `/watch add|remove|list` — configure which channels scrappy auto-watches
  (posted links trigger a download) in this server (needs **Manage
  Channels**). Settings persist in `watch_channels.json` next to `bot.py`
  and survive restarts. **Nothing is watched by default** — a server with no
  configuration falls back to the global `WATCH_CHANNELS` env var, or watches
  no channels at all if that's blank too; run `/watch add` to turn it on
  somewhere. `/scrape` isn't affected by this — it works in any channel
  regardless of `/watch` config, since it's an explicit on-demand pull, not
  passive watching.

Slash commands need the `applications.commands` OAuth2 scope (see invite URL
below) and sync globally on startup, which can take up to an hour to appear
the first time. Set `DEV_GUILD_ID` to a test server's ID to sync instantly
there while developing.

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
4. **OAuth2 → URL Generator**: scopes `bot` and `applications.commands`,
   bot permissions *Send Messages*, *Read Message History*, *Attach Files*.
   Open the URL to invite the bot to your server.
5. **Nothing is auto-watched until you turn it on.** Once the bot's in your
   server, run `/watch add #channel` for each channel it should watch (needs
   Manage Channels) — links posted elsewhere are ignored. Or, for a single
   global default across every server, enable Developer Mode (**Settings →
   Advanced**), right-click a channel → **Copy Channel ID**, put the IDs
   (comma-separated) in `WATCH_CHANNELS`. `/scrape` works everywhere
   regardless, since you're invoking it on purpose.

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
is blocked for those sites.

**Twitter/X is a special case:** instead of an explicit "log in" error, X's
own UI sometimes labels a post as a GIF/video when the underlying media is
actually just a static photo with no motion data at all (confirmed by
`/scrape format:image` succeeding on the same link while `format:video`/
`format:gif` both report "couldn't find" one) — in that case there's
nothing more to get, cookies or not. It's also possible for an
unauthenticated request to come back with real media missing that cookies
*would* fix; if `format:image` also fails on a link that clearly has visible
content, that's the more likely explanation, so add X cookies below and
retry.

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
can cover every site at once — log into each site (YouTube, Instagram,
TikTok, X, ...) in the same browser before exporting, or export separately
per site and append the files together.

## Notes

- Free-tier servers cap uploads at **20 MB** (Discord raised it from 10 MB in
  Aug 2026) — set `DISCORD_MAX_MB` to match your boost tier (L2 = 50,
  L3 = 100). Long 4K videos may not fit even re-encoded; the bot replies
  saying so.
- Videos over `MAX_MINUTES` (default **5**) are skipped before downloading,
  to avoid pulling multi-GB streams that won't fit anyway. Raise it if your
  server has more upload headroom.
- yt-dlp breaks when sites change. `AUTO_UPDATE=1` (default) refreshes it on
  each startup; otherwise `pip install -U yt-dlp` periodically.
- TikTok in particular walls off requests that don't look like a real
  browser — no login needed, `curl_cffi` (in `requirements.txt`) lets yt-dlp
  impersonate one. If a TikTok link fails with "Unexpected response from
  webpage request" and `curl_cffi` isn't installed, that's almost always why.
- `/scrape format:image` pulls whatever image/thumbnail URLs yt-dlp surfaces
  for the post (or a direct image link as-is). It's not a full gallery
  scraper — multi-photo carousels on some sites may only yield one image.
- `/scrape format:gif` tries, in order: a direct `.gif` file, sent as-is; a
  short looping mp4 (the common case on Twitter/Reddit/Tumblr, where a "gif"
  is actually encoded as video with no `.gif` anywhere) — **converted to an
  actual `.gif` with ffmpeg** (capped at `GIF_MAX_SECONDS`, 15s by default,
  since gifs are far less space-efficient than mp4 for the same length; falls
  back to sending the mp4 itself if the conversion fails or still won't fit);
  and finally, if there's truly no video/animation data at all (some posts
  just look animated in the site's own UI but are static photos), the plain
  image, with a note that it isn't actually moving.
