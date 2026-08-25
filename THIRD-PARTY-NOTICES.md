# Third-party notices

Prudify itself is MIT licensed (see `LICENSE`). It is distributed as a
container image that also contains other people's software. This file records
what is in there and under what terms, because "it's all open source" is not
an answer anyone should have to take on trust.

## Python dependencies

Every runtime dependency is under a permissive licence -- MIT, BSD-3-Clause,
or Apache-2.0. None of them is copyleft, and none of them places any condition
on Prudify's own licence.

| Package | Licence |
| --- | --- |
| fastapi, starlette, uvicorn | MIT / BSD-3-Clause |
| pydantic, pydantic-settings | MIT |
| SQLAlchemy | MIT |
| PyYAML | MIT |
| watchdog | Apache-2.0 |
| python-multipart | Apache-2.0 |
| typer, rich | MIT |
| faster-whisper | MIT |
| ctranslate2 | MIT |
| onnxruntime | MIT |
| tokenizers, huggingface_hub | Apache-2.0 |
| av (PyAV) | BSD-3-Clause |
| numpy | BSD-3-Clause |

To regenerate this list from an actual install:

```
pip install pip-licenses && pip-licenses --format=markdown
```

## Front end

React and React DOM are MIT. Vite and `@vitejs/plugin-react` are MIT and are
build-time only -- they are not shipped in the image.

## FFmpeg -- the one that needs care

The image installs Debian's `ffmpeg` package, which is built with
`--enable-gpl`. The FFmpeg **binary is therefore GPL-licensed**, not LGPL.

This does not affect Prudify's own licence. Prudify runs `ffmpeg` as a
separate process over a command line; it does not link against libavcodec.
That is aggregation, not derivation, and the GPL does not reach across it.
MIT code calling a GPL program stays MIT.

What it does mean is that anyone **redistributing the image** is redistributing
a GPL binary, and owes its recipients the corresponding source. In practice
Debian already publishes that source, and pointing at it is the accepted way to
discharge the obligation:

> FFmpeg is included under the GNU General Public License v2 or later.
> Source: https://sources.debian.org/src/ffmpeg/

To confirm what a given image actually shipped:

```
docker run --rm ghcr.io/camwise1/prudify:latest ffmpeg -version
```

An LGPL FFmpeg build is possible (drop `--enable-gpl`, lose x264/x265, which
Prudify never uses since it only touches audio) but Debian does not package
one, so it would mean building FFmpeg in the Dockerfile.

## Whisper models

Models are **not** bundled in the image. They are downloaded at first use from
the Hugging Face Hub, from the `Systran/faster-whisper-*` repositories, which
are MIT licensed -- as is OpenAI's original Whisper that they are converted
from. They are public: no account, token or licence acceptance is required.

## Wordlists

The bundled wordlists in `backend/prudify/wordlists/` are part of Prudify and
carry the same MIT licence.
