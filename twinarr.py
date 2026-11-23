#!/usr/bin/env python3
"""Twinarr - seed the same torrent on more trackers, without downloading anything.

Every day Twinarr lists the torrents qBittorrent is seeding from your *source* trackers,
searches your *target* Torznab indexers for the identical release, and adds each exact
match to qBittorrent pointing at the files you already have. qBittorrent verifies the
data before anything seeds. Python standard library only.

By H0m3Lab - MIT license.
"""

import argparse
import base64
import hashlib
import hmac
import html
import http.cookiejar
import json
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "0.3.1"
USER_AGENT = f"Twinarr/{VERSION}"
SEEDING_STATES = {"uploading", "stalledUP", "forcedUP", "queuedUP"}
CHECKING_STATES = {"checkingUP", "checkingDL", "checkingResumeData", "moving", "allocating", "metaDL"}
TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"
VIDEO_EXT = re.compile(r"\.(mkv|mp4|m4v|avi|ts|m2ts|wmv|mov|mpe?g)$", re.I)

log = logging.getLogger("twinarr")


# --------------------------------------------------------------------------- config

def env(name, default=""):
    return os.environ.get(name, default).strip()


def parse_targets(raw):
    """TARGETS = "name|torznab_url, name|torznab_url" -> [(name, url)]."""
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "|" not in part:
            raise ValueError(f"TARGETS entry needs 'name|url': {part!r}")
        name, url = part.split("|", 1)
        name, url = name.strip(), url.strip()
        if not name or not url.startswith(("http://", "https://")):
            raise ValueError(f"bad TARGETS entry: {part!r}")
        out.append((name, url))
    return out


def valid_time(s):
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", s or "")
    return bool(m) and int(m.group(1)) < 24 and int(m.group(2)) < 60


class Config:
    """Environment first; values saved on the web settings page (``saved``) win."""

    def __init__(self, saved=None):
        saved = saved or {}
        self.mode = (saved.get("mode") or env("MODE", "report")).lower()
        if self.mode not in ("report", "inject"):
            raise ValueError("MODE must be 'report' or 'inject'")
        self.qbit_url = env("QBIT_URL", "http://qbittorrent:8080").rstrip("/")
        self.qbit_user = env("QBIT_USER")
        self.qbit_pass = env("QBIT_PASS")
        self.prowlarr_url = env("PROWLARR_URL").rstrip("/")
        self.prowlarr_key = env("PROWLARR_APIKEY")
        src = saved["sources"] if "sources" in saved else env("SOURCE_TRACKERS")
        self.sources = [s.strip().lower() for s in src.split(",") if s.strip()]
        self.apikey = env("TORZNAB_APIKEY")
        if "targets" in saved and self.prowlarr_url:
            # [{"id": 2, "name": "..."}] picked from Prowlarr on the settings page
            self.targets = [(t["name"], f"{self.prowlarr_url}/{int(t['id'])}/api")
                            for t in json.loads(saved["targets"])]
            self.apikey = self.prowlarr_key  # guard:allow (attribute, no value)
        else:
            self.targets = parse_targets(env("TARGETS"))
            if not self.apikey and self.prowlarr_url and self.prowlarr_key:
                self.apikey = self.prowlarr_key  # guard:allow (attribute, no value)
        self.run_at = saved.get("run_at") or env("RUN_AT", "03:30")
        if not valid_time(self.run_at):
            raise ValueError("RUN_AT must be HH:MM")
        self.delay = float(env("SEARCH_DELAY", "30"))
        self.tag = env("TAG", "twinarr")
        self.category = env("INJECT_CATEGORY")  # empty = same category as the source torrent
        self.check_timeout = int(env("CHECK_TIMEOUT", "1800"))
        self.data_dir = env("DATA_DIR", "/data")
        self.ui_port = int(env("UI_PORT", "8383"))
        self.ui_password = env("UI_PASSWORD")

    def problems(self):
        out = []
        if not self.sources:
            out.append("no main (source) tracker picked")
        if not self.targets:
            out.append("no reseed-to (target) indexer picked")
        return out


# --------------------------------------------------------------------------- bencode

def bdecode(data):
    """Decode bencoded bytes. Returns (value, info_bytes) where info_bytes is the raw
    top-level 'info' dict (needed for the info-hash), or None."""
    info_span = []

    def dec(i, depth):
        c = data[i:i + 1]
        if c == b"i":
            end = data.index(b"e", i)
            return int(data[i + 1:end]), end + 1
        if c == b"l":
            i += 1
            out = []
            while data[i:i + 1] != b"e":
                v, i = dec(i, depth + 1)
                out.append(v)
            return out, i + 1
        if c == b"d":
            i += 1
            out = {}
            while data[i:i + 1] != b"e":
                k, i = dec(i, depth + 1)
                start = i
                v, i = dec(i, depth + 1)
                if depth == 0 and k == b"info":
                    info_span.append((start, i))
                out[k] = v
            return out, i + 1
        if c.isdigit():
            colon = data.index(b":", i)
            n = int(data[i:colon])
            start = colon + 1
            if start + n > len(data):
                raise ValueError("truncated string")
            return data[start:start + n], start + n
        raise ValueError(f"bad bencode at {i}")

    value, end = dec(0, 0)
    if end != len(data):
        raise ValueError("trailing data after bencode")
    info = data[info_span[0][0]:info_span[0][1]] if info_span else None
    return value, info


def _text(b):
    return b.decode("utf-8", "replace") if isinstance(b, bytes) else str(b)


def torrent_files(meta):
    """Files of a decoded .torrent as {relative/path: size}, the same way qBittorrent
    lists them with the 'Original' content layout."""
    info = meta[b"info"]
    name = _text(info.get(b"name.utf-8", info[b"name"]))
    if b"files" not in info:
        return {name: int(info[b"length"])}
    out = {}
    for f in info[b"files"]:
        parts = f.get(b"path.utf-8", f[b"path"])
        # BEP 47 padding files are not real data
        if f.get(b"attr", b"").find(b"p") >= 0 or (parts and _text(parts[-1]).startswith(".pad")):
            continue
        out["/".join([name] + [_text(p) for p in parts])] = int(f[b"length"])
    return out


def infohash(info_bytes):
    return hashlib.sha1(info_bytes).hexdigest()


# --------------------------------------------------------------------------- matching

def norm_path(p):
    return p.replace("\\", "/").strip("/")


def files_match(source_files, candidate_files):
    """Strict: identical set of relative paths with identical sizes."""
    a = {norm_path(k): v for k, v in source_files.items()}
    b = {norm_path(k): v for k, v in candidate_files.items()}
    return bool(a) and a == b


def search_query(name):
    """Indexers match words, not full release names. Cut the name down to
    'Title S01E02' / 'Title S01' for TV and 'Title 2024' for movies; the strict file
    check does the real matching."""
    s = re.sub(r"\.(mkv|mp4|avi|m4v|ts|wmv|iso)$", "", name, flags=re.I)
    s = re.sub(r"[._]+", " ", s).strip()
    m = re.search(r"\b(S\d{1,2}(?:E\d{1,3})?)\b", s, flags=re.I)
    if m:
        return (s[:m.start()].strip() + " " + m.group(1).upper()).strip()
    for m in re.finditer(r"\b(19\d{2}|20\d{2})\b", s):
        if m.start() > 0:  # a year at the very start is part of the title
            return s[:m.end()].strip()
    return " ".join(s.split()[:6])


def tracker_matches(tracker_urls, sources):
    hosts = []
    for u in tracker_urls:
        try:
            hosts.append((urllib.parse.urlsplit(u).hostname or "").lower())
        except ValueError:
            continue
    return any(s in h for h in hosts for s in sources)


def size_close(a, b, tolerance=0.01):
    return a > 0 and b > 0 and abs(a - b) <= a * tolerance


# --------------------------------------------------------------------------- HTTP

def http_get(url, timeout=60, max_bytes=20 * 1024 * 1024):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(max_bytes + 1)[:max_bytes + 1]


class MagnetRedirect(Exception):
    pass


class _NoMagnet(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if newurl.startswith("magnet:"):
            raise MagnetRedirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_torrent(url, timeout=60):
    opener = urllib.request.build_opener(_NoMagnet)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with opener.open(req, timeout=timeout) as r:
        data = r.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024 or not data.startswith(b"d"):
        raise ValueError("not a .torrent file")
    return data


def redact(url):
    return re.sub(r"(apikey|passkey|key|token)=[^&]+", r"\1=***", url, flags=re.I)


# --------------------------------------------------------------------------- Torznab

def parse_torznab(xml_bytes):
    """-> [{'title', 'size', 'link'}]"""
    root = ET.fromstring(xml_bytes)
    err = root if root.tag == "error" else root.find("error")
    if err is not None:
        raise RuntimeError(f"torznab error {err.get('code')}: {err.get('description')}")
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        size = 0
        enc = item.find("enclosure")
        link = (item.findtext("link") or "").strip()
        if enc is not None:
            link = enc.get("url") or link
            size = int(enc.get("length") or 0)
        for attr in item.findall(f"{TORZNAB_NS}attr"):
            if attr.get("name") == "size":
                size = int(attr.get("value") or 0)
        size = size or int(item.findtext("size") or 0)
        if link:
            out.append({"title": title, "size": size, "link": link})
    return out


def torznab_search(base_url, apikey, query):
    params = {"t": "search", "q": query}
    if apikey:
        params["apikey"] = apikey
    sep = "&" if "?" in base_url else "?"
    return parse_torznab(http_get(base_url + sep + urllib.parse.urlencode(params)))


# --------------------------------------------------------------------------- qBittorrent

def add_accepted(body):
    """True when qBittorrent accepted /torrents/add.

    4.x answers "Ok." (older 5.x an empty body); newer 5.x answers JSON like
    {"success_count":1,"failure_count":0,"pending_count":0,"added_torrent_ids":[...]}.
    """
    body = (body or b"").strip()
    if body in (b"Ok.", b""):
        return True
    try:
        res = json.loads(body)
    except ValueError:
        return False
    return (isinstance(res, dict) and not res.get("failure_count")
            and (res.get("success_count", 0) + res.get("pending_count", 0)) > 0)


class QBit:
    def __init__(self, url, user, password):
        self.url = url
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.user, self.password = user, password
        if user:
            self.login()

    def login(self):
        body = urllib.parse.urlencode({"username": self.user, "password": self.password}).encode()
        req = urllib.request.Request(self.url + "/api/v2/auth/login", data=body,
                                     headers={"Referer": self.url, "User-Agent": USER_AGENT})
        with self.opener.open(req, timeout=30) as r:
            body = r.read().strip()
        # qBittorrent 4.x answers "Ok.", 5.x answers 204 with an empty body; both set the SID cookie
        if body not in (b"Ok.", b"") or not any(c.name.endswith("SID") or "SID_" in c.name for c in self.jar):
            raise RuntimeError("qBittorrent login failed (check QBIT_USER / QBIT_PASS)")

    def _req(self, path, data=None, headers=None, raw=False):
        h = {"Referer": self.url, "User-Agent": USER_AGENT}
        h.update(headers or {})
        if isinstance(data, dict):
            data = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.url + path, data=data, headers=h)
        with self.opener.open(req, timeout=60) as r:
            body = r.read()
        return body if raw else (json.loads(body) if body.strip() else None)

    def torrents(self, hashes=None):
        q = "?" + urllib.parse.urlencode({"hashes": hashes}) if hashes else ""
        return self._req("/api/v2/torrents/info" + q)

    def trackers(self, h):
        return [t["url"] for t in self._req("/api/v2/torrents/trackers?hash=" + h)
                if t.get("url", "").startswith(("http", "udp"))]

    def files(self, h):
        return {f["name"]: int(f["size"]) for f in self._req("/api/v2/torrents/files?hash=" + h)}

    def add_stopped(self, torrent_bytes, savepath, category, tags):
        boundary = uuid.uuid4().hex
        fields = {"savepath": savepath, "category": category, "tags": tags,
                  "paused": "true", "stopped": "true", "skip_checking": "false",
                  "autoTMM": "false", "contentLayout": "Original", "root_folder": "true"}
        parts = []
        for k, v in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="torrents"; '
                     f'filename="twin.torrent"\r\nContent-Type: application/x-bittorrent\r\n\r\n'.encode()
                     + torrent_bytes + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = self._req("/api/v2/torrents/add", data=b"".join(parts), raw=True,
                         headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if not add_accepted(body):
            raise RuntimeError(f"qBittorrent refused the torrent: {body[:200]!r}")

    def recheck(self, h):
        self._req("/api/v2/torrents/recheck", data={"hashes": h}, raw=True)

    def start(self, h):
        try:
            self._req("/api/v2/torrents/start", data={"hashes": h}, raw=True)
        except urllib.error.HTTPError as e:  # qBittorrent 4.x
            if e.code != 404:
                raise
            self._req("/api/v2/torrents/resume", data={"hashes": h}, raw=True)

    def add_tags(self, h, tags):
        self._req("/api/v2/torrents/addTags", data={"hashes": h, "tags": tags}, raw=True)

    def set_share_limits(self, h, ratio, minutes):
        """Per-torrent seed limits; -2 = use qBittorrent's global limit. The stop/remove action
        stays the global one (qBittorrent 5.x needs the last two fields)."""
        self._req("/api/v2/torrents/setShareLimits", raw=True, data={
            "hashes": h, "ratioLimit": ratio, "seedingTimeLimit": minutes, "inactiveSeedingTimeLimit": -2,
            "shareLimitAction": "Default", "shareLimitsMode": "Default"})


# --------------------------------------------------------------------------- seed limits

def seed_limits_from_fields(fields):
    """Prowlarr indexer fields -> (ratio, minutes, pack_minutes); None = not set.
    An empty pack time falls back to the normal seed time, as Sonarr does."""
    f = {x.get("name"): x.get("value") for x in fields}
    ratio = f.get("torrentBaseSettings.seedRatio")
    minutes = f.get("torrentBaseSettings.seedTime")
    pack = f.get("torrentBaseSettings.packSeedTime")
    return ratio, minutes, pack if pack is not None else minutes


def share_limits_for(limits, files):
    """(ratio, minutes) for qBittorrent, -2 for 'global'; None when nothing is set."""
    if not limits:
        return None
    ratio, minutes, pack = limits
    videos = [n for n in files if VIDEO_EXT.search(n)]
    minutes = pack if len(videos) > 1 else minutes
    out = (-2 if ratio is None else float(ratio), -2 if minutes is None else int(minutes))
    return None if out == (-2, -2) else out


def prowlarr_seed_limits(cfg, target_url):
    """Seed limits of the Prowlarr indexer behind a target URL, or None (not a Prowlarr target)."""
    if not (cfg.prowlarr_url and cfg.prowlarr_key):
        return None
    m = re.fullmatch(re.escape(cfg.prowlarr_url) + r"/(\d+)/api", target_url)
    if not m:
        return None
    req = urllib.request.Request(f"{cfg.prowlarr_url}/api/v1/indexer/{m.group(1)}",
                                 headers={"X-Api-Key": cfg.prowlarr_key, "User-Agent": USER_AGENT})  # guard:allow
    with urllib.request.urlopen(req, timeout=20) as r:
        return seed_limits_from_fields(json.loads(r.read()).get("fields", []))


# --------------------------------------------------------------------------- state

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS searches (
        source_hash TEXT, target TEXT, searched_at TEXT, results INTEGER, outcome TEXT,
        PRIMARY KEY (source_hash, target))""",
    """CREATE TABLE IF NOT EXISTS twins (
        twin_hash TEXT PRIMARY KEY, source_hash TEXT, target TEXT, name TEXT,
        added_at TEXT, status TEXT)""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
    """CREATE TABLE IF NOT EXISTS runs (
        id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, trigger TEXT, mode TEXT,
        sources INTEGER, searches INTEGER, matches INTEGER, added INTEGER, error TEXT)""",
]


def open_db(path):
    db = sqlite3.connect(path, timeout=30)
    for s in SCHEMA:
        db.execute(s)
    db.commit()
    return db


def now():
    return datetime.now().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- run

def pick_sources(qb, cfg):
    out = []
    for t in qb.torrents():
        if t.get("progress", 0) < 1 or t.get("state") not in SEEDING_STATES:
            continue
        if cfg.tag in [x.strip() for x in (t.get("tags") or "").split(",")]:
            continue
        # 'tracker' is the working announce URL; it is empty while no tracker answers
        if not tracker_matches([t.get("tracker", "")], cfg.sources) and \
                not tracker_matches(qb.trackers(t["hash"]), cfg.sources):
            continue
        out.append(t)
    return out


def run_once(cfg, qb, db):
    known = {t["hash"].lower() for t in qb.torrents()}
    sources = pick_sources(qb, cfg)
    log.info("%d torrents seeding from source trackers; searching %d target(s); mode=%s",
             len(sources), len(cfg.targets), cfg.mode)
    found = added = searches = 0
    first = True
    seed_limits = {}  # target -> Prowlarr seed limits, read once per run
    for src in sources:
        files = qb.files(src["hash"])
        total = sum(files.values())
        query = search_query(src["name"])
        for target, url in cfg.targets:
            # already twinned on this target (by Twinarr or by hand): nothing left to find
            if db.execute("SELECT 1 FROM twins WHERE source_hash=? AND target=? COLLATE NOCASE AND status IN ('seeding','checking')",
                          (src["hash"], target)).fetchone() or \
                    db.execute("SELECT 1 FROM searches WHERE source_hash=? AND target=? COLLATE NOCASE AND outcome='already-in-client'",
                               (src["hash"], target)).fetchone():
                continue
            if not first:
                time.sleep(cfg.delay)
            first = False
            searches += 1
            try:
                results = torznab_search(url, cfg.apikey, query)
            except Exception as e:
                log.warning("[%s] search failed for %s: %s", target, query, redact(str(e)))
                continue
            outcome = "none"
            for res in results:
                if not size_close(total, res["size"]):
                    continue
                try:
                    data = fetch_torrent(res["link"])
                    meta, info = bdecode(data)
                except MagnetRedirect:
                    continue
                except Exception as e:
                    log.warning("[%s] could not read %s: %s", target, res["title"], redact(str(e)))
                    continue
                if info is None or not files_match(files, torrent_files(meta)):
                    continue
                h = infohash(info)
                if h in known:
                    outcome = "already-in-client"
                    break
                if db.execute("SELECT 1 FROM twins WHERE twin_hash=? AND status='mismatch'", (h,)).fetchone():
                    continue  # failed its data check before: never add it again
                found += 1
                outcome = "match"
                log.info("[%s] MATCH %s", target, src["name"])
                if cfg.mode == "inject":
                    category = cfg.category or src.get("category", "")
                    qb.add_stopped(data, src["save_path"], category, cfg.tag)
                    time.sleep(2)
                    apply_seed_limits(cfg, qb, seed_limits, target, url, h, files)
                    qb.recheck(h)
                    known.add(h)
                    db.execute("INSERT OR REPLACE INTO twins VALUES (?,?,?,?,?,?)",
                               (h, src["hash"], target, src["name"], now(), "checking"))
                    added += 1
                break
            db.execute("INSERT OR REPLACE INTO searches VALUES (?,?,?,?,?)",
                       (src["hash"], target, now(), len(results), outcome))
            db.commit()
    log.info("run done: %d match(es), %d added%s", found, added,
             "" if cfg.mode == "inject" else " (report mode: nothing added)")
    if added:
        settle_checks(cfg, qb, db)
    return {"sources": len(sources), "searches": searches, "matches": found, "added": added}


def apply_seed_limits(cfg, qb, cache, target, url, h, files):
    """Give a new twin the seed ratio/time its Prowlarr indexer has; on any error the twin
    keeps qBittorrent's global limits (logged, never fatal)."""
    try:
        if target not in cache:
            cache[target] = prowlarr_seed_limits(cfg, url)
        limits = share_limits_for(cache[target], files)
        if limits:
            qb.set_share_limits(h, *limits)
            log.info("[%s] seed limits ratio %s / %s min", target, *limits)
    except Exception as e:
        log.warning("[%s] could not set seed limits (global limits apply): %s", target, redact(str(e)))


def settle_checks(cfg, qb, db):
    """Start twins that verified 100%; leave the rest stopped and tag them."""
    deadline = time.time() + cfg.check_timeout
    while True:
        pending = [r[0] for r in db.execute("SELECT twin_hash FROM twins WHERE status='checking'")]
        if not pending:
            return
        info = {t["hash"].lower(): t for t in qb.torrents("|".join(pending))}
        for h in pending:
            t = info.get(h)
            if t is None:
                db.execute("UPDATE twins SET status='gone' WHERE twin_hash=?", (h,))
            elif t["state"] in CHECKING_STATES:
                continue
            elif t.get("progress", 0) >= 1:
                qb.start(h)
                db.execute("UPDATE twins SET status='seeding' WHERE twin_hash=?", (h,))
                log.info("verified 100%%, seeding: %s", t["name"])
            else:
                qb.add_tags(h, cfg.tag + "-mismatch")
                db.execute("UPDATE twins SET status='mismatch' WHERE twin_hash=?", (h,))
                log.warning("data check %.1f%%, left STOPPED (tag %s-mismatch): %s",
                            t.get("progress", 0) * 100, cfg.tag, t["name"])
        db.commit()
        if time.time() > deadline:
            log.warning("check timeout; %d twin(s) still checking, next run settles them", len(pending))
            return
        time.sleep(15)


def next_run(run_at, now_dt=None):
    now_dt = now_dt or datetime.now()
    hh, mm = (int(x) for x in run_at.split(":"))
    t = now_dt.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return t if t > now_dt else t + timedelta(days=1)


def status(db):
    for r in db.execute("SELECT started_at, trigger, sources, searches, matches, added, error "
                        "FROM runs ORDER BY id DESC LIMIT 1"):
        print("Last run: %s (%s) sources=%s searches=%s matches=%s added=%s %s" % tuple(x or "" for x in r))
    print("Searches by outcome:")
    for o, n in db.execute("SELECT outcome, COUNT(*) FROM searches GROUP BY outcome"):
        print(f"  {o:20} {n}")
    print("Twins by status:")
    for s, n in db.execute("SELECT status, COUNT(*) FROM twins GROUP BY status"):
        print(f"  {s:20} {n}")
    print("Last 10 twins:")
    for row in db.execute("SELECT added_at, target, status, name FROM twins ORDER BY added_at DESC LIMIT 10"):
        print("  " + "  ".join(row))


# --------------------------------------------------------------------------- runner + scheduler

DB_PATH = None
RUN_LOCK = threading.Lock()
RUNNING = {"since": None}


def db_conn():
    return open_db(DB_PATH)


def load_saved(c):
    return dict(c.execute("SELECT key, value FROM settings"))


def current_config(c=None):
    c = c or db_conn()
    return Config(load_saved(c))


def do_run(trigger):
    """One full pass. Returns False if a run is already going."""
    if not RUN_LOCK.acquire(blocking=False):
        return False
    RUNNING["since"] = now()
    c = db_conn()
    rid = None
    try:
        cfg = current_config(c)
        rid = c.execute("INSERT INTO runs (started_at, trigger, mode) VALUES (?,?,?)",
                        (now(), trigger, cfg.mode)).lastrowid
        c.commit()
        probs = cfg.problems()
        if probs:
            raise RuntimeError("not set up: " + "; ".join(probs))
        qb = QBit(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
        settle_checks(cfg, qb, c)  # twins left checking by an earlier run
        st = run_once(cfg, qb, c)
        c.execute("UPDATE runs SET finished_at=?, sources=?, searches=?, matches=?, added=? WHERE id=?",
                  (now(), st["sources"], st["searches"], st["matches"], st["added"], rid))
        c.commit()
        return True
    except Exception as e:
        msg = redact(str(e))
        log.error("run failed: %s", msg)
        if rid:
            c.execute("UPDATE runs SET finished_at=?, error=? WHERE id=?", (now(), msg[:500], rid))
            c.commit()
        return True
    finally:
        RUNNING["since"] = None
        c.close()
        RUN_LOCK.release()


def scheduler():
    last_fired = None
    while True:
        try:
            cfg = current_config()
            nxt = next_run(cfg.run_at, datetime.now() - timedelta(seconds=59))
            slot = nxt.strftime("%Y-%m-%d %H:%M")
            if datetime.now() >= nxt and slot != last_fired:
                last_fired = slot
                do_run("schedule")
        except Exception as e:
            log.error("scheduler: %s", redact(str(e)))
        time.sleep(20)


# --------------------------------------------------------------------------- lookups for the web page

def prowlarr_indexers(cfg):
    """[{'id', 'name'}] of enabled torrent indexers, or None when Prowlarr is not configured."""
    if not (cfg.prowlarr_url and cfg.prowlarr_key):
        return None
    req = urllib.request.Request(cfg.prowlarr_url + "/api/v1/indexer",
                                 headers={"X-Api-Key": cfg.prowlarr_key, "User-Agent": USER_AGENT})  # guard:allow
    with urllib.request.urlopen(req, timeout=20) as r:
        rows = json.loads(r.read())
    return sorted(({"id": int(x["id"]), "name": str(x["name"])} for x in rows
                   if x.get("protocol") == "torrent" and x.get("enable", True)), key=lambda x: x["name"].lower())


def tracker_hosts(cfg):
    """{host: number of completed torrents} from qBittorrent's working tracker per torrent."""
    qb = QBit(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
    counts = {}
    for t in qb.torrents():
        host = (urllib.parse.urlsplit(t.get("tracker") or "").hostname or "").lower()
        if host and t.get("progress", 0) >= 1:
            counts[host] = counts.get(host, 0) + 1
    return counts


# --------------------------------------------------------------------------- web UI

CSS = """:root{--bg:#f6f7f9;--fg:#1d2433;--card:#fff;--mut:#667085;--acc:#0e7c66;--line:#e4e7ec;--warn:#b42318;--ok:#067647}
@media (prefers-color-scheme:dark){:root{--bg:#12151c;--fg:#e6e9ef;--card:#1b2029;--mut:#98a2b3;--acc:#3fc9a7;--line:#2a3140;--warn:#f97066;--ok:#47cd89}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}main{max-width:1100px;margin:auto;padding:16px}
a{color:var(--acc)}h1{margin:.2em 0}h1 small{color:var(--mut);font-size:.5em;font-weight:400}nav{margin:4px 0 16px}nav a{margin-right:14px}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.big{font-size:1.4em;font-weight:700;color:var(--acc)}
table{width:100%;border-collapse:collapse;background:var(--card);border-radius:10px;overflow:hidden;margin:8px 0 20px}
td,th{padding:6px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}th{color:var(--mut);font-weight:600}
.mut{color:var(--mut)}.wrap{word-break:break-all}.scroll{overflow-x:auto}.warn{background:var(--warn);color:#fff;padding:10px;border-radius:8px;margin:10px 0}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--warn);font-weight:600}.note{background:var(--card);border:1px solid var(--acc);padding:10px;border-radius:8px;margin:10px 0}
form .row{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:8px 0}
input[type=text],input[type=time],select{padding:6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);max-width:100%}
button{background:var(--acc);color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:15px;cursor:pointer}button:disabled{opacity:.5;cursor:default}"""

STATUS_LABEL = {"seeding": ("ok", "seeding"), "checking": ("mut", "checking data"),
                "mismatch": ("bad", "data check failed, stopped"), "gone": ("mut", "removed from client")}


def esc(v):
    return html.escape("" if v is None else str(v))


def page(title, body, refresh=False):
    meta = "<meta http-equiv='refresh' content='15'>" if refresh else ""
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>{meta}<title>{esc(title)}</title>"
            f"<style>{CSS}</style></head><body><main>"
            f"<nav><a href='/'>Status</a><a href='/settings'>Settings</a></nav>{body}"
            f"<p class='mut'>Twinarr {VERSION} · by H0m3Lab</p></main></body></html>")


def fmt_time(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%b %d %H:%M")
    except ValueError:
        return iso


def status_page(msg=""):
    c = db_conn()
    try:
        cfg = current_config(c)
        last = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        cols = [d[0] for d in c.execute("SELECT * FROM runs LIMIT 0").description]
        runs = [dict(zip(cols, r)) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 14")]
        twins = c.execute("SELECT added_at, target, status, name FROM twins ORDER BY added_at DESC LIMIT 200").fetchall()
        seeding = c.execute("SELECT COUNT(*) FROM twins WHERE status='seeding'").fetchone()[0]
    finally:
        c.close()
    last = dict(zip(cols, last)) if last else None
    running = RUNNING["since"]
    nxt = next_run(cfg.run_at)
    if running:
        last_txt = f"running since {fmt_time(running)}"
    elif last and last["error"]:
        last_txt = f"<span class='bad'>failed</span> {fmt_time(last['started_at'])}"
    elif last:
        last_txt = (f"{fmt_time(last['started_at'])}<div class='mut'>{last['sources'] or 0} torrents, "
                    f"{last['matches'] or 0} matches, {last['added'] or 0} added</div>")
    else:
        last_txt = "never"
    cards = [("Next run", f"{nxt:%a %b %d %H:%M}"), ("Last run", last_txt), ("Twins seeding", str(seeding)),
             ("Mode", "inject (adds matches)" if cfg.mode == "inject" else "report (only lists matches)")]
    cards_html = "".join(f"<div class='card'><div class='mut'>{k}</div><div class='big'>{v}</div></div>" for k, v in cards)
    warn = "".join(f"<div class='warn'>Not set up: {esc(p)}. Open <a style='color:#fff' href='/settings'>Settings</a>.</div>"
                   for p in cfg.problems())
    note = f"<div class='note'>{esc(msg)}</div>" if msg else ""
    run_btn = (f"<form method='post' action='/run'><button{' disabled' if running else ''}>"
               f"{'Running…' if running else 'Run now'}</button></form>")
    sites = (f"<p>Main: <b>{esc(', '.join(cfg.sources) or 'none')}</b> → reseed to: "
             f"<b>{esc(', '.join(n for n, _ in cfg.targets) or 'none')}</b></p>")
    trows = "".join(
        f"<tr><td>{fmt_time(a)}</td><td>{esc(t)}</td><td class='{STATUS_LABEL.get(s, ('mut', s))[0]}'>"
        f"{esc(STATUS_LABEL.get(s, ('mut', s))[1])}</td><td class='wrap'>{esc(n)}</td></tr>" for a, t, s, n in twins) \
        or "<tr><td class='mut' colspan='4'>No twins added yet.</td></tr>"
    rrows = "".join(
        f"<tr><td>{fmt_time(r['started_at'])}</td><td>{esc(r['trigger'])}</td><td>{esc(r['mode'])}</td>"
        + (f"<td colspan='4' class='bad wrap'>{esc(r['error'])}</td>" if r["error"] else
           f"<td>{r['sources'] if r['sources'] is not None else '…'}</td><td>{r['searches'] or 0}</td>"
           f"<td>{r['matches'] or 0}</td><td>{r['added'] or 0}</td>") + "</tr>" for r in runs) \
        or "<tr><td class='mut' colspan='7'>No runs yet.</td></tr>"
    body = (f"<h1>Twinarr <small>seed what you have on more trackers</small></h1>{warn}{note}"
            f"<div class='cards'>{cards_html}</div>{sites}{run_btn}"
            f"<h2>Added twins</h2><div class='scroll'><table><tr><th>Added</th><th>Site</th><th>Status</th>"
            f"<th>Torrent</th></tr>{trows}</table></div>"
            f"<h2>Runs</h2><div class='scroll'><table><tr><th>Started</th><th>How</th><th>Mode</th>"
            f"<th>Torrents</th><th>Searches</th><th>Matches</th><th>Added</th></tr>{rrows}</table></div>")
    return page("Twinarr", body, refresh=bool(running))


def settings_page(msg=""):
    cfg = current_config()
    errors = []
    try:
        hosts = tracker_hosts(cfg)
    except Exception as e:
        hosts = {}
        errors.append(f"Could not read qBittorrent: {redact(str(e))}")
    try:
        idx = prowlarr_indexers(cfg)
    except Exception as e:
        idx = []
        errors.append(f"Could not read Prowlarr: {redact(str(e))}")
    for s in cfg.sources:  # keep picked hosts visible even with no torrent right now
        if not any(s in h for h in hosts):
            hosts[s] = 0
    src_rows = "".join(
        f"<tr><td><input type='checkbox' name='src' value='{esc(h)}' id='s{i}'"
        f"{' checked' if any(s in h for s in cfg.sources) else ''}></td>"
        f"<td><label for='s{i}'>{esc(h)}</label></td><td>{n}</td></tr>"
        for i, (h, n) in enumerate(sorted(hosts.items(), key=lambda x: -x[1]))) \
        or "<tr><td class='mut' colspan='3'>No torrents in qBittorrent.</td></tr>"
    picked = {n.lower() for n, _ in cfg.targets}
    if idx is None:
        tgt = ("<p class='mut'>Set <code>PROWLARR_URL</code> and <code>PROWLARR_APIKEY</code> to pick sites here. "
               f"Now using TARGETS from the environment: <b>{esc(', '.join(picked) or 'none')}</b>.</p>")
    else:
        tgt = "<table><tr><th></th><th>Indexer (Prowlarr)</th></tr>" + ("".join(
            f"<tr><td><input type='checkbox' name='tgt' value='{x['id']}' id='t{x['id']}'"
            f"{' checked' if x['name'].lower() in picked else ''}></td><td><label for='t{x['id']}'>{esc(x['name'])}</label></td></tr>"
            for x in idx) or "<tr><td></td><td class='mut'>No torrent indexers in Prowlarr.</td></tr>") + "</table>"
    note = f"<div class='note'>{esc(msg)}</div>" if msg else ""
    errs = "".join(f"<div class='warn'>{esc(e)}</div>" for e in errors)
    sel = lambda v: " selected" if cfg.mode == v else ""  # noqa: E731
    body = (f"<h1>Settings</h1>{note}{errs}<form method='post' action='/settings'>"
            f"<div class='row'><b>Daily run time</b><br><input type='time' name='run_at' value='{esc(cfg.run_at)}' required>"
            f"<div class='mut'>Container time zone. Every seeding torrent from the main tracker(s) is searched once per run.</div></div>"
            f"<div class='row'><b>Mode</b><br><select name='mode'><option value='report'{sel('report')}>report: only list matches</option>"
            f"<option value='inject'{sel('inject')}>inject: add matches to qBittorrent</option></select>"
            f"<div class='mut'>Added twins start only after qBittorrent's data check reaches 100%.</div></div>"
            f"<div class='row'><b>Main tracker(s)</b> <span class='mut'>where your torrents come from</span>"
            f"<div class='scroll'><table><tr><th></th><th>Tracker (from qBittorrent)</th><th>Torrents</th></tr>{src_rows}</table></div>"
            f"Other (part of a tracker host name): <input type='text' name='src_extra' placeholder='tracker.example'></div>"
            f"<div class='row'><b>Reseed to</b> <span class='mut'>sites to search for twins</span>{tgt}</div>"
            f"<p><button type='submit'>Save</button></p></form>")
    return page("Twinarr settings", body)


def save_settings(form):
    c = db_conn()
    try:
        run_at = (form.get("run_at") or [""])[0].strip()
        if not valid_time(run_at):
            return "Not saved: the run time must be HH:MM."
        mode = (form.get("mode") or ["report"])[0]
        if mode not in ("report", "inject"):
            return "Not saved: unknown mode."
        raw = form.get("src", []) + (form.get("src_extra") or [""])[0].split(",")
        src = [s for s in (x.strip().lower() for x in raw) if re.fullmatch(r"[a-z0-9.-]+", s)]
        rows = [("run_at", run_at), ("mode", mode), ("sources", ",".join(dict.fromkeys(src)))]
        cfg = current_config(c)
        idx = prowlarr_indexers(cfg)
        if idx is not None:
            want = {int(x) for x in form.get("tgt", []) if x.isdigit()}
            rows.append(("targets", json.dumps([x for x in idx if x["id"] in want])))
        c.executemany("INSERT OR REPLACE INTO settings VALUES (?,?)", rows)
        c.commit()
        Config(load_saved(c))  # validate
        log.info("settings saved: run_at=%s mode=%s sources=%s", run_at, mode, ",".join(src))
        return "Saved."
    except Exception as e:
        return f"Not saved: {redact(str(e))}"
    finally:
        c.close()


class Web(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, body="", ctype="text/html; charset=utf-8", headers=None):
        body = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        pw = current_config().ui_password
        if not pw:
            return True
        got = self.headers.get("Authorization", "")
        if got.startswith("Basic "):
            try:
                user_pw = base64.b64decode(got[6:]).decode("utf-8", "replace")
                if hmac.compare_digest(user_pw.split(":", 1)[-1], pw):
                    return True
            except ValueError:
                pass
        self.send(401, "login required", "text/plain", {"WWW-Authenticate": 'Basic realm="Twinarr"'})
        return False

    def same_origin(self):
        """Refuse form posts that come from another site."""
        host = self.headers.get("Host", "")
        src = self.headers.get("Origin") or self.headers.get("Referer") or ""
        return bool(src) and urllib.parse.urlsplit(src).netloc == host

    def do_GET(self):
        if self.path == "/health":
            return self.send(200, "ok", "text/plain")
        if not self.authed():
            return
        try:
            if self.path in ("/", "/index.html"):
                return self.send(200, status_page())
            if self.path == "/settings":
                return self.send(200, settings_page())
            return self.send(404, "not found", "text/plain")
        except Exception as e:
            log.error("web: %s", redact(str(e)))
            return self.send(500, "error, see the container log", "text/plain")

    def do_POST(self):
        if not self.authed():
            return
        if not self.same_origin():
            return self.send(403, "cross-site request refused", "text/plain")
        n = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
        form = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace"), keep_blank_values=True)
        if self.path == "/settings":
            return self.send(200, settings_page(save_settings(form)))
        if self.path == "/run":
            if RUNNING["since"]:
                return self.send(200, status_page("A run is already going."))
            threading.Thread(target=do_run, args=("manual",), daemon=True).start()
            time.sleep(0.5)
            return self.send(303, "", headers={"Location": "/"})
        return self.send(404, "not found", "text/plain")


def main(argv=None):
    global DB_PATH
    ap = argparse.ArgumentParser(description="Twinarr: seed the same torrent on more trackers.")
    ap.add_argument("command", nargs="?", default="daemon", choices=["daemon", "once", "status"])
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    data_dir = env("DATA_DIR", "/data")
    os.makedirs(data_dir, exist_ok=True)
    DB_PATH = os.path.join(data_dir, "twinarr.db")
    c = db_conn()
    if args.command == "status":
        status(c)
        return 0
    cfg = current_config(c)
    c.close()
    log.info("Twinarr %s", VERSION)
    if args.command == "once":
        do_run("manual")
        last = db_conn().execute("SELECT error FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return 1 if last and last[0] else 0
    threading.Thread(target=scheduler, daemon=True).start()
    log.info("web UI on port %d%s, daily run at %s", cfg.ui_port,
             "" if cfg.ui_password else " (no UI_PASSWORD: keep it on your home network)", cfg.run_at)
    ThreadingHTTPServer(("0.0.0.0", cfg.ui_port), Web).serve_forever()


if __name__ == "__main__":
    sys.exit(main())
