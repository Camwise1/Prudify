# Configuration

Everything lives in one `config.yaml` inside the data directory. The UI writes
the same file, so you can edit it by hand, use the Settings page, or both.
`prudify config` prints where it is.

## Environment variables

Environment variables only decide *where* things live and how the process
binds — the settings themselves are not duplicated there.

| Variable | Purpose |
| --- | --- |
| `PRUDIFY_DATA_DIR` | Config, database, logs, transcripts (Docker: `/config`) |
| `PRUDIFY_CONFIG` | Path to `config.yaml`, if not in the data directory |
| `PRUDIFY_WORK_DIR` | Scratch space for renders (defaults to `<data>/work`) |
| `PRUDIFY_HOST` / `PRUDIFY_PORT` | Bind address and port |
| `PRUDIFY_API_KEY` | Sets the key on first run instead of generating one |
| `PRUDIFY_LOG_LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `PRUDIFY_FFMPEG` / `PRUDIFY_FFPROBE` | Explicit binary paths |
| `PRUDIFY_POLLING_WATCHER` | `1` forces polling instead of filesystem events |
| `OMP_NUM_THREADS` | Threads available to the transcription backend |

## config.yaml

```yaml
server:
  host: 0.0.0.0
  port: 8317
  url_base: ""              # "/prudify" behind a reverse proxy
  api_key: <generated>
  require_api_key: true

auth:
  # none | apikey | basic | forms | external
  method: forms
  # always | disabled_for_local
  required: always
  username: you
  password_hash: scrypt$...      # never a plaintext password
  session_lifetime_hours: 720
  # method: external only. The identity header is ignored from anywhere else.
  trusted_proxies: []
  proxy_user_header: X-Forwarded-User

libraries:
  - id: 3f2a19bc
    name: Audiobooks
    source_path: /audiobooks
    output_path: /audiobooks-clean
    enabled: true
    auto_process: true       # queue new books automatically
    layout: books            # books | episodes -- see below
    extensions: []           # [] means every supported format
    exclude_patterns: []     # globs relative to source_path

transcription:
  engine: faster-whisper     # faster-whisper | whisper-cpp | openai-whisper
  model: base.en
  device: auto               # auto | cpu | cuda
  compute_type: auto         # auto | int8 | int8_float16 | float16 | float32
  language: en
  beam_size: 5
  vad_filter: true
  cpu_threads: 4
  chunk_minutes: 0           # 0 = auto; long files are chunked
  chunk_overlap_seconds: 2
  model_dir: ""

filtering:
  wordlist: strict
  custom_words: []
  custom_allowlist: []
  match_mode: exact          # exact | prefix | fuzzy
  fuzzy_max_distance: 1
  pad_before_ms: 0
  pad_after_ms: 100
  merge_gap_ms: 250
  min_confidence: 0.0

output:
  mode: mute                 # mute | beep | cut
  beep_frequency: 1000
  beep_volume: 0.15
  container: same            # same | m4b | m4a | mp3 | opus
  audio_codec: auto
  bitrate: auto              # "auto" matches the source
  preserve_chapters: true
  preserve_cover: true
  preserve_metadata: true
  tag_cleaned: true
  copy_when_clean: true
  overwrite_existing: false

processing:
  max_concurrent_jobs: 1
  cooldown_seconds: 0
  scan_interval_minutes: 60
  stability_seconds: 30
  skip_if_output_exists: true
  keep_transcripts: true
  keep_work_files: false
  dry_run: false
  min_free_space_mb: 2048
  duration_tolerance_seconds: 1.0

log_level: INFO
```

## Authentication

`forms` is the default: a login page and a session cookie, the same shape the
*arr applications use. `basic` uses the browser's own password prompt.
`external` takes the username from a reverse-proxy header, which is how you
put Authelia, Authentik or Keycloak in front of Prudify without Prudify
needing to understand OIDC — note the header is only trusted from the
networks listed in `trusted_proxies`, because otherwise anyone could send it.

Whichever you choose, **the API key keeps working** for scripts, the CLI and
Home Assistant. It travels in the `X-Api-Key` header.

Upgrading an existing install does not change anything: a config file written
before login existed keeps API-key authentication until you choose otherwise.

Locked out? There is no email reset, because Prudify runs on your hardware and
has no mail server. Shell access is the recovery path:

```bash
prudify auth set-password --username you
prudify auth status
prudify auth method none          # last resort on a trusted network
```

## The settings that actually matter

**`transcription.model`** dominates both quality and runtime. `base.en` finds
essentially all clearly-enunciated profanity. `small.en` is meaningfully better
on mumbled or heavily-accented narration and roughly three times slower.

**`transcription.cpu_threads`** is the main speed control on CPU. Set it to
your physical core count minus one or two — Whisper will happily take the whole
machine otherwise.

**`processing.max_concurrent_jobs`** should stay at 1 unless you have cores to
spare. Two jobs on four cores is slower than one job on four cores.

**`processing.stability_seconds`** is how long a newly-appeared file must stop
changing size before it is queued. Raise it if books arrive over a slow network
share — transcribing a half-copied file wastes an hour.

**`processing.scan_interval_minutes`** is the safety net. Filesystem events do
not reliably cross NFS or SMB, so on a NAS this is usually the mechanism that
actually notices new books. Do not set it to 0 unless your library is local.

**`output.container: same`** keeps M4B as M4B and MP3 as MP3. Forcing `m4b` on
a folder of MP3s produces one M4B per MP3, which is probably not what you want.

## Adding a library

Source and output must be different directories. If the output folder sits
inside the source folder, Prudify skips it during scans so cleaned files are
never re-cleaned — but keeping them separate is tidier.

The output path has to be writable, and Prudify checks by writing a file there
rather than by trying to create the directory. Creating a directory that
already exists succeeds even on a read-only mount, which is exactly how an
output path aimed inside a `:ro` media mount used to pass validation and then
fail hours later, after the book had been transcribed.

### Layout: books or episodes

`layout: books` is the default and assumes the audiobook convention — a folder
is one work, and the files inside it are its parts. Twelve MP3s in
`Author/Title/` are twelve chapters of one book.

`layout: episodes` says the folder is a *show* and every file in it is a
separate thing someone listens to on its own. Use it for podcasts. Without it
a show reaches the scanner as a pile of differently-titled MP3s, which is the
one case the grouping heuristics treat as a single multi-part work — so three
hundred episodes become one job that succeeds or fails as a unit, and one bad
file takes the lot with it.

In episodic mode the episode title comes from the filename and the show from
its folder, with no tag lookup. Reading tags costs one `ffprobe` per file, and
for a show with hundreds of episodes on a network share that is the entire
scan, spent to learn a title the filename already carries.

Podcasts and audiobooks want separate library entries even when they live
under the same mount. If the podcast folder sits inside your audiobook tree,
add it to that library's `exclude_patterns` so it is not catalogued twice.

Point a second Audiobookshelf (or Plex, or Jellyfin) library at the output
folder. Because the folder structure is mirrored, your metadata agents will
match the same books.

## The API

The full REST API is documented at `/api/docs` on a running instance. Every
endpoint takes an `X-Api-Key` header.

```bash
KEY=$(grep api_key ~/.config/prudify/config.yaml | awk '{print $2}')

curl -H "X-Api-Key: $KEY" localhost:8317/api/v1/system/status
curl -H "X-Api-Key: $KEY" localhost:8317/api/v1/books?status=new
curl -H "X-Api-Key: $KEY" -X POST localhost:8317/api/v1/books/queue-all
```

Live progress is a Server-Sent Events stream at `/api/v1/queue/events`, which
accepts the key as an `apikey` query parameter since `EventSource` cannot set
headers.

## Troubleshooting

**Books are found but never queue.** Check *auto-process* on the library, and
that the book is monitored. Books you have ignored show as `Ignored`.

**"ffmpeg was not found".** Install it, or set `PRUDIFY_FFMPEG`. On launchd
and Windows services the process environment does not inherit your shell
`PATH`.

**Output rejected for duration drift.** The render did not match the source
length. Look at Logs for the ffmpeg error; a truncated or corrupt source file
is the usual cause. Prudify deletes the bad output rather than publishing it.

**Everything is very slow.** Confirm `cpu_threads` and `OMP_NUM_THREADS` are
set, check `compute_type` is `int8` on CPU, and consider a smaller model. The
Dashboard shows which stage a job is in — if it is stuck at `transcribing`,
that is Whisper, not Prudify.

**The percentage has not moved in twenty minutes.** Probably nothing is wrong.
Encoding is a single ffmpeg run over the whole book, and publishing the result
to a network share is a multi-gigabyte copy; both can hold one number for a
long time. The queue reports how long the current stage has been running, so
check that the elapsed time is still climbing. If it has frozen too, the job
really is stuck — Logs will have the last thing ffmpeg said.

**The scratch volume keeps growing.** A container killed mid-render cannot
clean up after itself. Prudify sweeps abandoned job directories at startup,
keeping only those belonging to jobs still queued, so a restart reclaims them.
On Docker Desktop for Windows note that the volume lives inside a WSL2 disk
image which grows to its high-water mark and never shrinks on its own.

**Chapters lost on MP3 output.** MP3 has no standard chapter container the way
M4B does. Keep `container: same` for M4B sources.
