#!/usr/bin/env python3
"""Twinarr - seed the same torrent on more trackers, without downloading anything.

Every day Twinarr lists the torrents qBittorrent is seeding from your *source* trackers,
searches your *target* Torznab indexers for the identical release, and adds each exact
match to qBittorrent pointing at the files you already have. qBittorrent verifies the
data before anything seeds. Python standard library only.

By H0m3Lab - MIT license.
"""

import argparse
import hashlib
import http.cookiejar
import json
import logging
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta

VERSION = "0.1.0"
USER_AGENT = f"Twinarr/{VERSION}"
SEEDING_STATES = {"uploading", "stalledUP", "forcedUP", "queuedUP"}
CHECKING_STATES = {"checkingUP", "checkingDL", "checkingResumeData", "moving", "allocating", "metaDL"}
TORZNAB_NS = "{http://torznab.com/schemas/2015/feed}"

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


class Config:
    def __init__(self):
        self.mode = env("MODE", "report").lower()
        if self.mode not in ("report", "inject"):
            raise ValueError("MODE must be 'report' or 'inject'")
        self.qbit_url = env("QBIT_URL", "http://qbittorrent:8080").rstrip("/")
        self.qbit_user = env("QBIT_USER")
        self.qbit_pass = env("QBIT_PASS")
        self.sources = [s.lower() for s in env("SOURCE_TRACKERS").split(",") if s.strip()]
        self.targets = parse_targets(env("TARGETS"))
        self.apikey = env("TORZNAB_APIKEY")
        self.run_at = env("RUN_AT", "03:30")
        if not re.fullmatch(r"\d{1,2}:\d{2}", self.run_at):
            raise ValueError("RUN_AT must be HH:MM")
        self.delay = float(env("SEARCH_DELAY", "30"))
        self.tag = env("TAG", "twinarr")
        self.category = env("INJECT_CATEGORY")  # empty = same category as the source torrent
        self.check_timeout = int(env("CHECK_TIMEOUT", "1800"))
        self.data_dir = env("DATA_DIR", "/data")
        if not self.sources:
            raise ValueError("SOURCE_TRACKERS is empty")
        if not self.targets:
            raise ValueError("TARGETS is empty")


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
        if body.strip() not in (b"Ok.", b""):
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


# --------------------------------------------------------------------------- state

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS searches (
        source_hash TEXT, target TEXT, searched_at TEXT, results INTEGER, outcome TEXT,
        PRIMARY KEY (source_hash, target))""",
    """CREATE TABLE IF NOT EXISTS twins (
        twin_hash TEXT PRIMARY KEY, source_hash TEXT, target TEXT, name TEXT,
        added_at TEXT, status TEXT)""",
]


def open_db(path):
    db = sqlite3.connect(path)
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
    found = added = 0
    first = True
    for src in sources:
        files = qb.files(src["hash"])
        total = sum(files.values())
        query = search_query(src["name"])
        for target, url in cfg.targets:
            # already twinned on this target (by Twinarr or by hand): nothing left to find
            if db.execute("SELECT 1 FROM twins WHERE source_hash=? AND target=? AND status IN ('seeding','checking')",
                          (src["hash"], target)).fetchone() or \
                    db.execute("SELECT 1 FROM searches WHERE source_hash=? AND target=? AND outcome='already-in-client'",
                               (src["hash"], target)).fetchone():
                continue
            if not first:
                time.sleep(cfg.delay)
            first = False
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
    print("Searches by outcome:")
    for o, n in db.execute("SELECT outcome, COUNT(*) FROM searches GROUP BY outcome"):
        print(f"  {o:20} {n}")
    print("Twins by status:")
    for s, n in db.execute("SELECT status, COUNT(*) FROM twins GROUP BY status"):
        print(f"  {s:20} {n}")
    print("Last 10 twins:")
    for row in db.execute("SELECT added_at, target, status, name FROM twins ORDER BY added_at DESC LIMIT 10"):
        print("  " + "  ".join(row))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Twinarr: seed the same torrent on more trackers.")
    ap.add_argument("command", nargs="?", default="daemon", choices=["daemon", "once", "status"])
    ap.add_argument("--version", action="version", version=VERSION)
    args = ap.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = Config()
    os.makedirs(cfg.data_dir, exist_ok=True)
    db = open_db(os.path.join(cfg.data_dir, "twinarr.db"))
    if args.command == "status":
        status(db)
        return 0
    log.info("Twinarr %s", VERSION)
    while True:
        if args.command == "daemon":
            nxt = next_run(cfg.run_at)
            log.info("next run %s", nxt.strftime("%Y-%m-%d %H:%M"))
            settle_wait = (nxt - datetime.now()).total_seconds()
            time.sleep(max(0, settle_wait))
        try:
            qb = QBit(cfg.qbit_url, cfg.qbit_user, cfg.qbit_pass)
            settle_checks(cfg, qb, db)  # twins left checking by an earlier run
            run_once(cfg, qb, db)
        except Exception as e:
            log.error("run failed: %s", redact(str(e)))
            if args.command == "once":
                return 1
        if args.command == "once":
            return 0


if __name__ == "__main__":
    sys.exit(main())
