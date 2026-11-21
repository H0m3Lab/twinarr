# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - 2026-11-21

### Added
- Web page on port 8383 (`UI_PORT`): next/last run, **Run now**, added twins with status, run history.
- Settings page: daily run time, report/inject mode, pick the main tracker(s) from the trackers in
  qBittorrent and the reseed-to sites from your Prowlarr indexers (`PROWLARR_URL`, `PROWLARR_APIKEY`).
  Saved values are stored in `twinarr.db` and override the environment.
- Optional `UI_PASSWORD` (HTTP basic auth); form posts from other sites are refused.
- `runs` table: every run with its counts or error.

### Changed
- `SOURCE_TRACKERS` and `TARGETS` are no longer required at start-up; an unconfigured Twinarr shows
  "Not set up" instead of exiting.
- Target names are compared without regard to case.

## [0.1.0] - 2026-11-21

### Added
- First public release: daily search of every torrent seeding from your source trackers on your
  Torznab target indexers, strict file-by-file matching, report and inject modes, data check before
  seeding (mismatches stay stopped), SQLite search history, `status` command.

[Unreleased]: https://github.com/H0m3Lab/twinarr/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/H0m3Lab/twinarr/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/H0m3Lab/twinarr/releases/tag/v0.1.0
