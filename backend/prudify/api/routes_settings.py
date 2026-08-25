"""Settings, libraries and wordlist management."""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Body, Depends, HTTPException, Request

from ..config import Config, LibrarySettings, save_config, writable_dir_error
from ..core import matcher as matcher_mod
from ..core.transcribe import Word
from ..db import session_scope
from ..schemas import (
    LibraryIn,
    MatchTestIn,
    MatchTestOut,
    SettingsIn,
    SettingsOut,
    WordlistOut,
)
from ..services import library as library_service
from ..services.events import bus
from .deps import get_config

router = APIRouter(tags=["settings"])

# Bundled lists ship with the package and are read-only; user lists live in the
# data directory so an upgrade never overwrites someone's curated list.
_USER_LIST_SUBDIR = "wordlists"


def _user_wordlist_dir(config: Config) -> Path:
    directory = config.resolved_data_dir() / _USER_LIST_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    return directory


@router.get("/settings", response_model=SettingsOut)
def get_settings(config: Config = Depends(get_config)) -> SettingsOut:
    return SettingsOut(**config.model_dump())


@router.put("/settings", response_model=SettingsOut)
def update_settings(
    request: Request,
    payload: SettingsIn = Body(...),
    config: Config = Depends(get_config),
) -> SettingsOut:
    data = payload.model_dump(exclude_none=True)
    for section, values in data.items():
        if section == "log_level":
            config.log_level = values
            continue
        current = getattr(config, section)
        setattr(config, section, type(current).model_validate(values))

    save_config(config)
    request.app.state.config = config
    bus.publish("settings.updated", {"sections": list(data.keys())})
    return SettingsOut(**config.model_dump())


@router.post("/settings/regenerate-api-key")
def regenerate_api_key(request: Request, config: Config = Depends(get_config)) -> dict:
    config.server.api_key = secrets.token_hex(16)
    save_config(config)
    request.app.state.config = config
    return {"api_key": config.server.api_key}


# --------------------------------------------------------------------------
# Libraries
# --------------------------------------------------------------------------


@router.get("/libraries")
def list_libraries(config: Config = Depends(get_config)) -> list[dict]:
    output = []
    for library in config.libraries:
        source = Path(library.source_path).expanduser()
        destination = Path(library.output_path).expanduser()
        output.append(
            {
                **library.model_dump(),
                "source_exists": source.is_dir(),
                "output_exists": destination.is_dir(),
                "output_writable": destination.is_dir()
                and writable_dir_error(destination) is None,
            }
        )
    return output


def _validate_paths(payload: LibraryIn) -> None:
    """Reject a library whose paths cannot do the job, before it is saved."""
    source = Path(payload.source_path).expanduser()
    if not source.is_dir():
        raise HTTPException(status_code=400, detail=f"Source path not found: {source}")

    output = Path(payload.output_path).expanduser()
    problem = writable_dir_error(output)
    if problem:
        raise HTTPException(status_code=400, detail=f"Output path is unusable: {problem}")

    if source.resolve() == output.resolve():
        raise HTTPException(
            status_code=400, detail="Output path must differ from the source path"
        )


@router.post("/libraries")
def create_library(
    request: Request, payload: LibraryIn, config: Config = Depends(get_config)
) -> dict:
    _validate_paths(payload)
    library = LibrarySettings(**payload.model_dump())
    config.libraries.append(library)
    save_config(config)
    request.app.state.config = config
    bus.publish("library.created", {"id": library.id, "name": library.name})
    return library.model_dump()


@router.put("/libraries/{library_id}")
def update_library(
    request: Request,
    library_id: str,
    payload: LibraryIn,
    config: Config = Depends(get_config),
) -> dict:
    library = config.library_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    # Same checks as creation. Without them a library could be created with a
    # good output path and later edited to point inside a read-only mount,
    # which is not discovered until a book finishes transcribing.
    _validate_paths(payload)
    updated = LibrarySettings(id=library_id, **payload.model_dump())
    config.libraries = [updated if lib.id == library_id else lib for lib in config.libraries]
    save_config(config)
    request.app.state.config = config
    return updated.model_dump()


@router.delete("/libraries/{library_id}")
def delete_library(
    request: Request, library_id: str, config: Config = Depends(get_config)
) -> dict:
    if config.library_by_id(library_id) is None:
        raise HTTPException(status_code=404, detail="Library not found")
    config.libraries = [lib for lib in config.libraries if lib.id != library_id]
    save_config(config)
    request.app.state.config = config
    return {"deleted": library_id}


@router.post("/libraries/{library_id}/scan")
def scan_library(library_id: str, config: Config = Depends(get_config)) -> dict:
    library = config.library_by_id(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="Library not found")
    with session_scope() as session:
        return library_service.scan_library(session, config, library)


# --------------------------------------------------------------------------
# Wordlists
# --------------------------------------------------------------------------


@router.get("/wordlists")
def list_wordlists(config: Config = Depends(get_config)) -> list[dict]:
    entries = []
    for path in sorted(matcher_mod.bundled_wordlist_dir().glob("*.txt")):
        entries.append({"name": path.stem, "builtin": True})
    for path in sorted(_user_wordlist_dir(config).glob("*.txt")):
        entries.append({"name": path.stem, "builtin": False})
    return entries


@router.get("/wordlists/{name}", response_model=WordlistOut)
def get_wordlist(name: str, config: Config = Depends(get_config)) -> WordlistOut:
    name = _safe_name(name)
    user_path = _user_wordlist_dir(config) / f"{name}.txt"
    builtin_path = matcher_mod.bundled_wordlist_dir() / f"{name}.txt"
    path = user_path if user_path.exists() else builtin_path
    if not path.exists():
        raise HTTPException(status_code=404, detail="Wordlist not found")
    content = path.read_text(encoding="utf-8")
    return WordlistOut(
        name=name,
        builtin=path == builtin_path,
        content=content,
        rule_count=len(matcher_mod.parse_wordlist(content.splitlines())),
    )


@router.put("/wordlists/{name}", response_model=WordlistOut)
def save_wordlist(
    name: str, content: str = Body(..., embed=True), config: Config = Depends(get_config)
) -> WordlistOut:
    """Saving a bundled list writes a user copy that shadows it."""
    name = _safe_name(name)
    path = _user_wordlist_dir(config) / f"{name}.txt"
    path.write_text(content, encoding="utf-8")
    bus.publish("wordlist.updated", {"name": name})
    return WordlistOut(
        name=name,
        builtin=False,
        content=content,
        rule_count=len(matcher_mod.parse_wordlist(content.splitlines())),
    )


@router.delete("/wordlists/{name}")
def delete_wordlist(name: str, config: Config = Depends(get_config)) -> dict:
    name = _safe_name(name)
    path = _user_wordlist_dir(config) / f"{name}.txt"
    if not path.exists():
        raise HTTPException(status_code=404, detail="No user wordlist by that name")
    path.unlink()
    return {"deleted": name}


@router.post("/wordlists/test", response_model=MatchTestOut)
def test_matching(payload: MatchTestIn, config: Config = Depends(get_config)) -> MatchTestOut:
    """Run a sentence through the matcher so users can see what would be cut."""
    filtering = config.filtering.model_copy(deep=True)
    if payload.wordlist:
        filtering.wordlist = payload.wordlist
    if payload.custom_words:
        filtering.custom_words = list(filtering.custom_words) + payload.custom_words
    if payload.match_mode:
        filtering.match_mode = payload.match_mode  # type: ignore[assignment]

    matcher = matcher_mod.build_matcher_from_settings(
        filtering, user_dir=_user_wordlist_dir(config)
    )
    tokens = payload.text.split()
    words = [
        Word(start=float(i), end=float(i) + 0.5, text=token, probability=1.0)
        for i, token in enumerate(tokens)
    ]
    matches = matcher.find(words)
    hit_indices = set()
    rules: dict[int, str] = {}
    for match in matches:
        span = len(match.text.split())
        for offset in range(span):
            hit_indices.add(match.word_index + offset)
            rules[match.word_index + offset] = match.rule

    return MatchTestOut(
        tokens=[
            {"text": token, "matched": index in hit_indices, "rule": rules.get(index, "")}
            for index, token in enumerate(tokens)
        ],
        match_count=len(matches),
    )


def _safe_name(name: str) -> str:
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in "-_")
    if not cleaned:
        raise HTTPException(status_code=400, detail="Invalid wordlist name")
    return cleaned
