# Installation and service setup

Prudify needs three things: Python 3.10+, `ffmpeg` on the `PATH`, and a folder
it can write to. Everything else is optional.

---

## Docker

The image bundles ffmpeg and `faster-whisper`, so nothing else is required.

```bash
docker run -d \
  --name prudify \
  -p 8317:8317 \
  -e OMP_NUM_THREADS=4 \
  -v $(pwd)/config:/config \
  -v /path/to/audiobooks:/audiobooks:ro \
  -v /path/to/audiobooks-clean:/audiobooks-clean \
  --restart unless-stopped \
  ghcr.io/camwise1/prudify:latest
```

The API key is printed to the container log on first start:

```bash
docker logs prudify 2>&1 | grep "API key"
```

### Volumes

| Path | Purpose |
| --- | --- |
| `/config` | Config file, SQLite database, logs, transcripts, downloaded models |
| `/audiobooks` | Your library. Mount read-only. |
| `/audiobooks-clean` | Where cleaned copies land |

Models download into `/config/models` on first use — a few hundred megabytes
for `base.en`. Keeping that on the volume means rebuilding the container does
not re-download them.

### Synology

Under Container Manager, mount your media share read-only and create the clean
folder as a separate shared folder. NFS and SMB mounts rarely deliver
filesystem events, so leave the scheduled rescan enabled, and set
`PRUDIFY_POLLING_WATCHER=1` if you want immediate pickup.

### Unraid

Set `PUID`/`PGID` to match your media share owner (usually `99`/`100`) so the
cleaned files are readable by your other containers.

---

## Native install

```bash
pip install "prudify[whisper]"
```

**On Windows**, PowerShell blocks unsigned scripts by default, so activating a
virtual environment fails with "running scripts is disabled on this system".
Allow your own local scripts once, per user, no administrator rights needed:

```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

A script that arrived over the network also needs `Unblock-File <path>`, since
Windows marks downloaded files and `RemoteSigned` refuses them.

Without the `[whisper]` extra you get the server and the UI but no
transcription backend — useful for trying it out, not for actually cleaning
anything.

### ffmpeg

Any version from 4.x onwards works. Prudify passes its filter graph in a file
to stay under the Windows command-line length limit, and the option for that
was renamed in ffmpeg 7.0 and removed in 9.0 -- the correct spelling is chosen
at runtime from the binary you have, so old and new both work.

| Platform | Command |
| --- | --- |
| macOS | `brew install ffmpeg` |
| Windows | `winget install Gyan.FFmpeg` |
| Debian/Ubuntu | `sudo apt install ffmpeg` |
| Fedora | `sudo dnf install ffmpeg` |

If ffmpeg lives somewhere unusual, point at it explicitly:

```bash
export PRUDIFY_FFMPEG=/opt/ffmpeg/bin/ffmpeg
export PRUDIFY_FFPROBE=/opt/ffmpeg/bin/ffprobe
```

### Where things live

| Platform | Data directory |
| --- | --- |
| Linux | `~/.config/prudify` |
| macOS | `~/Library/Application Support/Prudify` |
| Windows | `%APPDATA%\Prudify` |

Override with `PRUDIFY_DATA_DIR`. `prudify config` prints the resolved paths.

---

## Running as a service

### Linux (systemd)

`/etc/systemd/system/prudify.service`:

```ini
[Unit]
Description=Prudify
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=prudify
Group=prudify
Environment="PRUDIFY_DATA_DIR=/var/lib/prudify"
Environment="OMP_NUM_THREADS=4"
ExecStart=/usr/local/bin/prudify serve
Restart=on-failure
RestartSec=10

# Transcription is a long, low-priority background job. Being nice about it
# keeps the machine usable for everything else.
Nice=10
IOSchedulingClass=idle

# Hardening: Prudify only needs its data directory and your media paths.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/prudify /path/to/audiobooks-clean

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now prudify
journalctl -u prudify -f
```

### macOS (launchd)

`~/Library/LaunchAgents/com.prudify.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.prudify</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/prudify</string>
    <string>serve</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>OMP_NUM_THREADS</key>
    <string>4</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/prudify.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/prudify.err</string>
  <!-- Low priority: transcription should not fight the foreground. -->
  <key>ProcessType</key>
  <string>Background</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.prudify.plist
```

Note that `PATH` must include the directory containing `ffmpeg` — launchd does
not inherit your shell environment.

### Windows (service)

Using [NSSM](https://nssm.cc/):

```powershell
nssm install Prudify "C:\Python311\Scripts\prudify.exe" serve
nssm set Prudify AppEnvironmentExtra PRUDIFY_DATA_DIR=E:\Prudify OMP_NUM_THREADS=4
nssm set Prudify AppDirectory E:\Prudify
nssm set Prudify Start SERVICE_AUTO_START
nssm set Prudify AppPriority BELOW_NORMAL_PRIORITY_CLASS
nssm start Prudify
```

Or with the built-in `sc.exe` plus a scheduled task at boot, if you would
rather not add a dependency.

**A note for low-memory Windows boxes**: `faster-whisper` streams the file
instead of allocating one tensor for the whole book, so the classic
`DefaultCPUAllocator: not enough memory` failure from `openai-whisper` does not
apply. If you are still tight, set *Chunk length* to 30 minutes in
Settings → Transcription and keep the page file on a drive with room.

---

## Reverse proxy

Set **Settings → Security → URL base** to your sub-path (for example
`/prudify`) and restart. Server-Sent Events need buffering disabled:

### nginx

```nginx
location /prudify/ {
    proxy_pass http://127.0.0.1:8317;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;

    # Required for the live queue updates.
    proxy_buffering off;
    proxy_cache off;
    proxy_read_timeout 3600s;
}
```

### Caddy

```
handle_path /prudify/* {
    reverse_proxy 127.0.0.1:8317 {
        flush_interval -1
    }
}
```

If your proxy already authenticates, you can turn off *Require the API key* in
Settings → Security. Do not do that on anything reachable from the internet.

---

## Upgrading

```bash
# Docker
docker compose pull && docker compose up -d

# pip
pip install --upgrade "prudify[whisper]"
```

Your config, database and transcripts live in the data directory and are not
touched by an upgrade. Bundled wordlists may change between releases; any list
you have edited is stored separately in `<data dir>/wordlists/` and shadows the
bundled one, so your edits survive.
