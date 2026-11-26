# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.4] - 2026-11-26

### Fixed

- qBittorrent can stop answering its API for minutes while it hash-checks a big file, so every run failed with "timed out" part-way and later torrents were never searched. Twinarr now waits up to `QBIT_TIMEOUT` (300 s) per call and asks again (3 attempts) when qBittorrent is busy.
- A torrent add that timed out could still have been accepted, leaving a twin stopped at 0% with no seed limits, no data check and no record in Twinarr. Twinarr now waits for the twin to appear and finishes it (seed limits, recheck, record); an add is never sent twice.
- Seed limits sent while qBittorrent was busy could be lost. They are now read back and sent once more if they did not stick.

### Added

- `QBIT_TIMEOUT` setting (seconds per qBittorrent call, default 300).

## [0.3.3] - 2026-11-24

### Fixed

- A run that failed part-way (for example a request that timed out) skipped the step that starts verified twins, so twins it had already added sat stopped ("Completed") until the next day's run. The settle step now also runs after a failed run.

## [0.3.2] - 2026-11-23

### Fixed

- A twin whose data check never ran (the recheck request was lost when qBittorrent restarted or was too busy to answer) showed 0% and was marked a mismatch for good, so it was never seeded or added again. A twin below 100% now gets one more recheck before it counts as a mismatch; a real mismatch still stays stopped.

## [0.3.1] - 2026-11-23

### Fixed

- qBittorrent 5.1+ answers a torrent add with JSON (`success_count` ...) instead of `Ok.`; Twinarr took that as a refusal and stopped the run, leaving the new twin stopped and unchecked.

## [0.3.0] - 2026-11-21

### Added
- New twins get the seed limits of the Prowlarr indexer they came from (seed ratio, seed time, and
  pack seed time when the twin has more than one video file), so qBittorrent stops them at that
  site's rule instead of the global limit. Values left empty in Prowlarr use qBittorrent's global
  limit. Targets that are not Prowlarr indexers keep the global limits.

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

[Unreleased]: https://github.com/H0m3Lab/twinarr/compare/v0.3.2...HEAD
[0.3.2]: https://github.com/H0m3Lab/twinarr/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/H0m3Lab/twinarr/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/H0m3Lab/twinarr/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/H0m3Lab/twinarr/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/H0m3Lab/twinarr/releases/tag/v0.1.0
