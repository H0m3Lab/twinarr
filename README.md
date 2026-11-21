# Twinarr

**Seed the torrents you already have on more trackers, without downloading anything.**

You grab a release from one tracker. Often the exact same release, with the same files, is also on
your other trackers. Seeding it there too earns upload credit for data you already have on disk.

Every day Twinarr:

1. lists the completed torrents qBittorrent is **seeding** from your **source** trackers,
2. searches your **target** indexers (any Torznab feed, e.g. through Prowlarr) for each one,
3. downloads only the `.torrent` file of results with the same size and compares the file list:
   **same file names, same sizes**, or it is not a match,
4. adds each match to qBittorrent **stopped**, pointing at the files you already have, and forces a
   data check,
5. starts it only if the check says 100%. Anything else stays stopped and gets the tag
   `twinarr-mismatch`, so qBittorrent never downloads a single byte.

Every seeding torrent is searched again every day, however old it is. If a twin is uploaded weeks
after your grab, the next daily run finds it. Torrents already in qBittorrent (added by Twinarr, by
another tool or by hand) are recognized by their info-hash and skipped from then on.

Twinarr is one Python file, standard library only, with a small SQLite history and a built-in
web page (port 8383):

- **Status:** next and last run, a **Run now** button, every twin it added (site, status) and the run
  history.
- **Settings:** the daily run time, report or inject mode, and the sites. **Main** lists the trackers
  your qBittorrent torrents come from (with counts): tick the one(s) you download from. **Reseed to**
  lists your Prowlarr indexers: tick the sites to look for twins on.

## Install

```bash
git clone https://github.com/H0m3Lab/twinarr.git && cd twinarr
cp docker-compose.example.yml docker-compose.yml   # adjust paths, network, TZ
cp .env.example .env                               # trackers, indexers, qBittorrent login
docker compose up -d --build
docker compose logs -f twinarr
```

Open `http://<server>:8383`, pick your sites on **Settings**, and press **Run now** on the status
page. It starts in **report mode**: it searches and logs `MATCH ...` lines but adds nothing. From the
command line:

```bash
docker compose run --rm twinarr once
docker compose run --rm twinarr status   # searches by outcome, twins by status
```

When the matches look right, switch the mode to **inject** on the settings page.

Values saved on the settings page (run time, mode, main trackers, reseed-to sites) are stored in
`twinarr.db` and win over the environment variables below, which are the starting defaults.

### Requirements

- qBittorrent 4.4 or newer with the WebUI on (tested on 5.x).
- Twinarr reaches qBittorrent and your indexers over the network. It does **not** need your media
  mounted: the new torrent uses the source torrent's save path, and qBittorrent reads the files.
- Your qBittorrent default content layout should be **Original** (the default). Twinarr compares
  paths the way qBittorrent lists them.

## Settings

| Variable | Default | Meaning |
|---|---|---|
| `PROWLARR_URL` / `PROWLARR_APIKEY` | empty | Lets the settings page list your Prowlarr indexers to reseed to |
| `UI_PORT` | `8383` | Web page port |
| `UI_PASSWORD` | empty | If set, the web page asks for it (any user name). Without it, keep the port on your home network |
| `MODE` | `report` | `report` = log matches only; `inject` = add them to qBittorrent |
| `SOURCE_TRACKERS` | empty | Parts of announce host names, comma separated. Seeding torrents from these get searched |
| `TARGETS` | empty | `name\|torznab_url`, comma separated. The indexers to search, if you don't pick them from Prowlarr |
| `TORZNAB_APIKEY` | empty | Added as `apikey=` to every Torznab request (e.g. your Prowlarr API key) |
| `QBIT_URL` | `http://qbittorrent:8080` | qBittorrent WebUI |
| `QBIT_USER` / `QBIT_PASS` | empty | WebUI login. Leave empty if the WebUI skips auth for Twinarr's network |
| `RUN_AT` | `03:30` | Daily run time, container local time (`TZ`) |
| `SEARCH_DELAY` | `30` | Seconds between searches |
| `TAG` | `twinarr` | Tag on every added torrent; failed checks get `<TAG>-mismatch` |
| `INJECT_CATEGORY` | empty | Category for added torrents. Empty = same category as the source torrent |
| `CHECK_TIMEOUT` | `1800` | Seconds to wait for data checks after a run; leftovers are settled on the next run |
| `DATA_DIR` | `/data` | Where `twinarr.db` lives |
| `LOG_LEVEL` | `INFO` | Python log level |

Example `.env` with two targets through Prowlarr:

```ini
SOURCE_TRACKERS=tracker-a.example
TARGETS=tracker-b|http://prowlarr:9696/2/api,tracker-c|http://prowlarr:9696/3/api
TORZNAB_APIKEY=your-prowlarr-api-key
```

## How matching works

- The search query is the release name cut down to `Title S01E02`, `Title S01` or `Title 2024`,
  because indexers match words, not full release names.
- Results whose size is more than 1% off are skipped without downloading anything.
- For the rest, the `.torrent` is fetched and its files (BEP 47 padding files ignored) must equal the
  source torrent's files exactly: same relative paths, same sizes.
- qBittorrent's own data check is the final word: it must reach 100% before the twin starts.

## Be a good citizen

Searching every seeding torrent daily is more API traffic than once-and-done tools make. Keep
`SEARCH_DELAY` at 30 seconds or more, and check that your trackers allow automated searches and
cross-seeding. Most do. Some limit API calls per day.

## Troubleshooting

- **The page says "Not set up"**: no main tracker or no reseed-to site is picked yet. Open Settings.
- **Settings shows "Could not read Prowlarr"**: check `PROWLARR_URL` (reachable from the container)
  and `PROWLARR_APIKEY` (Prowlarr → Settings → General).
- **`qBittorrent login failed`**: check `QBIT_USER` / `QBIT_PASS`, and that `QBIT_URL` is reachable
  from the container's network.
- **`0 torrents seeding from source trackers`**: `SOURCE_TRACKERS` is matched against the announce
  **host name** only (never the path or passkey). Look at a torrent's Trackers tab in qBittorrent and
  use part of that host.
- **`search failed ... torznab error 100`**: wrong API key. Put it in `TORZNAB_APIKEY` or in the
  `TARGETS` URLs, not both.
- **A twin stays stopped with tag `twinarr-mismatch`**: the data check did not reach 100%, so the files
  on disk differ from that torrent. Remove it in qBittorrent **without deleting files**. Twinarr
  remembers it and never adds that torrent again.

## Releases and contributing

Changes are listed in [CHANGELOG.md](CHANGELOG.md); releases are tagged `vX.Y.Z`. Issues and pull
requests are welcome; see the
[contribution guide](https://github.com/H0m3Lab/.github/blob/main/CONTRIBUTING.md). Report security
problems privately through the **Security** tab.

## License

MIT. See [LICENSE](LICENSE).
