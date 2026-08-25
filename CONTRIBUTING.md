# Contributing

Thanks for looking. Bug reports, wordlists, format support and UI polish are all
welcome.

## Getting set up

```bash
git clone https://github.com/Camwise1/Prudify.git prudify && cd prudify
python -m venv .venv && source .venv/bin/activate
pip install -e ".[whisper,dev]"

cd frontend && npm install && cd ..
```

Two terminals for development:

```bash
prudify serve                  # backend on :8317
cd frontend && npm run dev      # UI on :5173, proxying /api to :8317
```

## Tests

```bash
pytest                  # everything
pytest tests/test_matcher.py -v
ruff check backend tests
```

Audio tests build their own fixtures with ffmpeg rather than committing binary
files, and skip automatically if ffmpeg is absent. Pipeline tests seed the
transcript cache so they run in CI without downloading a Whisper model — every
other stage still runs for real, including the ffmpeg render and validation.

If you change anything about rendering, the tests that matter are the ones in
`tests/test_pipeline.py` that measure the actual audio level inside and outside
a muted interval. A render that produces a file of the right length but the
wrong contents is exactly the bug worth catching.

## Architecture

```
backend/prudify/
  core/       probe, transcribe, match, render
  services/   queue, watcher, event bus, library reconciliation
  api/        FastAPI routers
```

`core/` must not import from `services/` or `api/`, and must not know the
database exists. That constraint is what lets `prudify clean` and the job queue
be two thin wrappers around the same pipeline. Please keep it.

The database is a cache and a journal, never the source of truth for audio.
Deleting it and rescanning must always be a safe recovery.

## Adding a transcription backend

Subclass `Transcriber` in `core/transcribe.py`, implement `transcribe()` to
return a `Transcript` of `Word(start, end, text, probability)`, and register it
in `ENGINES`. Everything downstream is engine-agnostic.

## Adding a wordlist

Drop a `.txt` file in `backend/prudify/wordlists/`. The syntax is documented in
[docs/wordlists.md](docs/wordlists.md). Please keep lists focused and say in the
PR what tier the list is aiming at — a list is much more useful when its scope
is predictable.

## Code style

- Python: ruff, 100 columns, type hints on public functions.
- JavaScript: two-space indent, no semicolons, functional components.
- Comments should explain *why*, especially where the code works around an
  ffmpeg quirk. There are several, and each one cost somebody an afternoon.

## Pull requests

Small and focused beats large and sweeping. Include a test for behaviour
changes. If you found an ffmpeg incompatibility, please note the version and
platform — behaviour genuinely varies between 4.x, 6.x and 7.x.
