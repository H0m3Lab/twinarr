# CLAUDE.md: Twinarr

Guide for AI coding agents (and humans) working on this repository. Organization rules:
https://github.com/H0m3Lab/.github/blob/main/STANDARDS.md

## Purpose

Twinarr seeds torrents a user already has on more trackers. Once a day it searches Torznab indexers
for exact twins of every torrent qBittorrent seeds from the configured source trackers, and adds each
match to qBittorrent pointing at the existing files. Nothing is ever downloaded.

## Architecture

One file, `twinarr.py`:

- `Config`: reads environment variables (see README settings table).
- `bdecode` / `torrent_files` / `infohash`: minimal bencode reader for `.torrent` files.
- `torznab_search` / `parse_torznab`: Torznab `t=search` requests and RSS parsing.
- `QBit`: qBittorrent WebUI API v2 client (login, list, files, trackers, add, recheck, start, tags).
- `run_once`: pick sources (`pick_sources`) → query (`search_query`) → size pre-filter (`size_close`) →
  fetch `.torrent` → strict file compare (`files_match`) → add stopped + recheck (inject mode).
- `settle_checks`: starts twins that verified 100%, tags the rest `<TAG>-mismatch` and leaves them stopped.
- `main`: `daemon` (sleep until `RUN_AT`, run, repeat), `once`, `status`.
- SQLite `twinarr.db` in `DATA_DIR`: `searches` (last outcome per source+target) and `twins`.

## Stack

- Python 3.12, standard library only (no pip dependencies)
- Storage: SQLite
- Packaging: Docker (`Dockerfile`), Compose example in `docker-compose.example.yml`

## Repository layout

```
twinarr.py                  the whole app
tests/test_twinarr.py       unit tests (unittest, no network)
Dockerfile                  python:3.12-slim image, entrypoint twinarr.py
docker-compose.example.yml  copy to docker-compose.yml (git-ignored)
.env.example                copy to .env (git-ignored): trackers, indexers, logins
```

## Commands

| Task | Command |
|---|---|
| Test | `python -m unittest discover -s tests -v` |
| Lint | `ruff check --select E9,F63,F7,F82,F401,F841 --target-version py312 .` |
| Run once | `python twinarr.py once` (needs the env vars) |
| Status | `python twinarr.py status` |
| Docker image | `docker build -t twinarr:local .` |

CI (`.github/workflows/ci.yml`) runs compile, ruff, unit tests, docker build and a gitleaks scan. Keep it green.

## Configuration

Environment variables are documented in the README settings table and `.env.example`. When you add
or change one: update both, give it a safe default, and add a CHANGELOG line.

## Conventions

- Standard library only; ask before adding a dependency.
- Log every match and every failure; never log URLs without `redact()` (Torznab URLs carry API keys).
- Default to the safe side: `MODE=report` by default; anything unexpected skips the item.

## Data safety (do not break)

- Never add a torrent that is not a strict file-list match.
- Always add **stopped** with `skip_checking=false` and force a recheck; start only at 100% progress.
  A twin that is not 100% must stay stopped, otherwise qBittorrent would download into the user's files.
- Never delete, move, rename or recheck the **source** torrent, and never touch files on disk.
- `twinarr.db` is history only; schema changes are new `CREATE TABLE IF NOT EXISTS` / migration steps.

## Security

- Never hardcode credentials or private values (real IPs, host names, domains, keys, names, tracker
  names). Use placeholders like `tracker-a.example` / `192.0.2.10` in code, tests and docs.
- Twinarr exposes no port. It needs outbound access to qBittorrent and the Torznab URLs only.

## Releases

Semantic versioning. Version lives in `VERSION` in `twinarr.py`. Steps: CHANGELOG `[Unreleased]` → new
version section, bump `VERSION`, commit, tag `vX.Y.Z`, GitHub release with the changelog section as notes.
