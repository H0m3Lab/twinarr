import hashlib
import os
import sys
import tempfile
import unittest
from contextlib import nullcontext
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import twinarr as t  # noqa: E402


def benc(v):
    if isinstance(v, int):
        return b"i%de" % v
    if isinstance(v, str):
        v = v.encode()
    if isinstance(v, bytes):
        return b"%d:%s" % (len(v), v)
    if isinstance(v, list):
        return b"l" + b"".join(benc(x) for x in v) + b"e"
    if isinstance(v, dict):
        items = sorted((k.encode() if isinstance(k, str) else k, x) for k, x in v.items())
        return b"d" + b"".join(benc(k) + benc(x) for k, x in items) + b"e"
    raise TypeError(v)


MULTI = {"announce": "https://tracker.example.com/a",
         "info": {"name": "Show.S01.1080p.WEB-GRP", "piece length": 16384, "pieces": b"x" * 20,
                  "files": [{"length": 100, "path": ["Show.S01E01.mkv"]},
                            {"length": 7, "path": [".pad", "7"], "attr": "p"},
                            {"length": 200, "path": ["Subs", "en.srt"]}]}}
SINGLE = {"info": {"name": "Show.S01E01.1080p.WEB-GRP.mkv", "length": 1234,
                   "piece length": 16384, "pieces": b"y" * 20}}


class Bencode(unittest.TestCase):
    def test_multi_file_and_infohash(self):
        raw = benc(MULTI)
        meta, info = t.bdecode(raw)
        self.assertEqual(t.infohash(info), hashlib.sha1(benc(MULTI["info"])).hexdigest())
        self.assertEqual(t.torrent_files(meta), {
            "Show.S01.1080p.WEB-GRP/Show.S01E01.mkv": 100,
            "Show.S01.1080p.WEB-GRP/Subs/en.srt": 200})

    def test_single_file(self):
        meta, _ = t.bdecode(benc(SINGLE))
        self.assertEqual(t.torrent_files(meta), {"Show.S01E01.1080p.WEB-GRP.mkv": 1234})

    def test_rejects_garbage(self):
        for bad in (b"", b"d3:abc", b"x", benc(SINGLE) + b"junk"):
            with self.assertRaises(Exception):
                t.bdecode(bad)


class Matching(unittest.TestCase):
    def test_strict_match(self):
        a = {"Dir/a.mkv": 10, "Dir/b.srt": 2}
        self.assertTrue(t.files_match(a, {"Dir/b.srt": 2, "Dir/a.mkv": 10}))
        self.assertTrue(t.files_match({"Dir\\a.mkv": 10}, {"Dir/a.mkv": 10}))
        self.assertFalse(t.files_match(a, {"Dir/a.mkv": 10}))            # missing file
        self.assertFalse(t.files_match(a, {"Dir/a.mkv": 11, "Dir/b.srt": 2}))  # size differs
        self.assertFalse(t.files_match(a, {"Other/a.mkv": 10, "Other/b.srt": 2}))  # folder differs
        self.assertFalse(t.files_match({}, {}))

    def test_query_and_size(self):
        q = t.search_query
        self.assertEqual(q("Some.Show.S01E01.1080p.WEB-GRP.mkv"), "Some Show S01E01")
        self.assertEqual(q("some.show.s03e02.dv.hdr.2160p.web.h265-grp.mkv"), "some show S03E02")
        self.assertEqual(q("Some.Show.S02.1080p.WEB-DL-GRP"), "Some Show S02")
        self.assertEqual(q("A.Movie.Title.2024.2160p.WEB-DL-GRP"), "A Movie Title 2024")
        self.assertEqual(q("2001.A.Film.1968.1080p"), "2001 A Film 1968")
        self.assertEqual(q("Odd_Name_Without_Tags"), "Odd Name Without Tags")
        self.assertTrue(t.size_close(1000, 1005))
        self.assertFalse(t.size_close(1000, 1100))
        self.assertFalse(t.size_close(0, 0))

    def test_tracker_matches_host_only(self):
        urls = ["https://tracker.source.example/abc/announce"]
        self.assertTrue(t.tracker_matches(urls, ["source.example"]))
        self.assertFalse(t.tracker_matches(urls, ["abc"]))  # path/passkey never matches
        self.assertFalse(t.tracker_matches([""], ["source.example"]))


class Torznab(unittest.TestCase):
    def test_parse(self):
        xml = b"""<?xml version="1.0"?>
<rss xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
<item><title>A</title><link>http://idx.example/dl/1</link><size>500</size></item>
<item><title>B</title><enclosure url="http://idx.example/dl/2" length="0" type="application/x-bittorrent"/>
<torznab:attr name="size" value="900"/></item>
</channel></rss>"""
        self.assertEqual(t.parse_torznab(xml), [
            {"title": "A", "size": 500, "link": "http://idx.example/dl/1"},
            {"title": "B", "size": 900, "link": "http://idx.example/dl/2"}])

    def test_error(self):
        with self.assertRaises(RuntimeError):
            t.parse_torznab(b'<error code="100" description="Invalid API Key"/>')


class SeedLimits(unittest.TestCase):
    def test_from_prowlarr_fields(self):
        f = [{"name": "torrentBaseSettings.seedRatio", "value": 2},
             {"name": "torrentBaseSettings.seedTime", "value": 600},
             {"name": "torrentBaseSettings.packSeedTime", "value": None}, {"name": "x", "value": 1}]
        self.assertEqual(t.seed_limits_from_fields(f), (2, 600, 600))
        self.assertEqual(t.seed_limits_from_fields([]), (None, None, None))

    def test_share_limits(self):
        lim = (1, 2880, 8640)
        self.assertEqual(t.share_limits_for(lim, {"a.mkv": 1, "a.nfo": 1}), (1.0, 2880))
        self.assertEqual(t.share_limits_for(lim, {"S01/e1.mkv": 1, "S01/e2.mkv": 1}), (1.0, 8640))
        self.assertEqual(t.share_limits_for((1, None, None), {"a.mkv": 1}), (1.0, -2))
        self.assertIsNone(t.share_limits_for((None, None, None), {"a.mkv": 1}))
        self.assertIsNone(t.share_limits_for(None, {"a.mkv": 1}))

    def test_only_prowlarr_targets(self):
        cfg = t.Config({"sources": "tracker-a.example"})
        cfg.prowlarr_url, cfg.prowlarr_key = "http://192.0.2.10:9696", "k"
        self.assertIsNone(t.prowlarr_seed_limits(cfg, "http://192.0.2.20/api"))


class QBitAdd(unittest.TestCase):
    def test_add_responses(self):
        self.assertTrue(t.add_accepted(b"Ok."))
        self.assertTrue(t.add_accepted(b""))
        self.assertTrue(t.add_accepted(b'{"added_torrent_ids":["ab"],"failure_count":0,"pending_count":0,"success_count":1}'))
        self.assertTrue(t.add_accepted(b'{"failure_count":0,"pending_count":1,"success_count":0}'))
        self.assertFalse(t.add_accepted(b'{"failure_count":1,"pending_count":0,"success_count":0}'))
        self.assertFalse(t.add_accepted(b"Fails."))


class FakeQB:
    """qBittorrent stand-in for settle_checks: each recheck finishes at once with the next result."""
    def __init__(self, results):
        self.results = list(results)  # progress after each recheck
        self.progress = self.results.pop(0)
        self.started = self.rechecks = 0
        self.tags = []

    def torrents(self, hashes=None):
        return [{"hash": "aa", "name": "Show.S01E01", "progress": self.progress,
                 "state": "stoppedUP" if self.progress >= 1 else "stoppedDL"}]

    def recheck(self, h):
        self.rechecks += 1
        self.progress = self.results.pop(0)

    def start(self, h):
        self.started += 1

    def add_tags(self, h, tag):
        self.tags.append(tag)


class SettleChecks(unittest.TestCase):
    def settle(self, results):
        db = t.open_db(":memory:")
        db.execute("INSERT INTO twins VALUES ('aa','bb','SiteA','Show.S01E01','2026-01-01T00:00:00','checking')")
        qb = FakeQB(results)
        cfg = t.Config({})
        cfg.check_timeout = 60
        orig, t.time.sleep = t.time.sleep, lambda s: None
        try:
            t.settle_checks(cfg, qb, db)
        finally:
            t.time.sleep = orig
        return qb, db.execute("SELECT status FROM twins").fetchone()[0]

    def test_lost_check_gets_one_more(self):
        qb, status = self.settle([0.0, 1.0])  # first check never ran, second verifies
        self.assertEqual((status, qb.rechecks, qb.started), ("seeding", 1, 1))

    def test_real_mismatch_stays_stopped(self):
        qb, status = self.settle([0.4, 0.4])
        self.assertEqual((status, qb.rechecks, qb.started), ("mismatch", 1, 0))
        self.assertEqual(len(qb.tags), 1)

    def test_good_twin_no_extra_check(self):
        qb, status = self.settle([1.0])
        self.assertEqual((status, qb.rechecks, qb.started), ("seeding", 0, 1))

    def test_failed_run_still_starts_its_twins(self):
        """A run that times out after adding a twin must still start it, not leave it for tomorrow."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "twinarr.db")
        qb = FakeQB([1.0])
        cfg = t.Config({})
        cfg.check_timeout = 60
        cfg.problems = lambda: []

        def failing_run(cfg, qb, db):
            db.execute("INSERT INTO twins VALUES ('aa','bb','SiteA','Show.S01E01','2026-01-01T00:00:00','checking')")
            db.commit()
            raise TimeoutError("timed out")

        saved = (t.DB_PATH, t.current_config, t.QBit, t.run_once, t.time.sleep)
        t.DB_PATH, t.current_config = path, lambda c=None: cfg
        t.QBit, t.run_once, t.time.sleep = (lambda *a: qb), failing_run, (lambda s: None)
        try:
            with self.assertLogs("twinarr", level="ERROR"):
                t.do_run("test")
        finally:
            t.DB_PATH, t.current_config, t.QBit, t.run_once, t.time.sleep = saved
        db = t.open_db(path)
        self.addCleanup(db.close)
        self.assertEqual(db.execute("SELECT status FROM twins").fetchone()[0], "seeding")
        self.assertEqual(qb.started, 1)
        self.assertIn("timed out", db.execute("SELECT error FROM runs").fetchone()[0])


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self.body


class BusyOpener:
    """Times out `stalls` times, then answers."""
    def __init__(self, stalls, body=b"[]"):
        self.stalls, self.body, self.calls = stalls, body, 0

    def open(self, req, timeout=None):
        self.calls += 1
        if self.calls <= self.stalls:
            raise t.urllib.error.URLError(TimeoutError("timed out"))
        return FakeResponse(self.body)


class BusyQBit(unittest.TestCase):
    def call(self, stalls, retry=True):
        qb = t.QBit("http://qbit.example", "", "", timeout=1)
        qb.opener = BusyOpener(stalls)
        orig, t.time.sleep = t.time.sleep, lambda s: None
        try:
            with self.assertLogs("twinarr", level="WARNING") if stalls and retry else nullcontext():
                return qb, qb._req("/api/v2/torrents/info", retry=retry)
        finally:
            t.time.sleep = orig

    def test_busy_qbit_is_asked_again(self):
        qb, res = self.call(2)
        self.assertEqual((res, qb.opener.calls), ([], 3))

    def test_gives_up_after_retries(self):
        with self.assertRaises(t.urllib.error.URLError):
            self.call(5)

    def test_no_retry_when_asked(self):
        with self.assertRaises(t.urllib.error.URLError):
            self.call(1, retry=False)


class InjectQB:
    """qBittorrent stand-in for inject_twin / apply_seed_limits."""
    def __init__(self, add_times_out=False, appears=True, lost_limit_sets=0):
        self.add_times_out, self.appears = add_times_out, appears
        self.lost = lost_limit_sets
        self.limit_sets = self.rechecks = 0
        self.limits = (-2, -2)

    def add_stopped(self, *a):
        if self.add_times_out:
            raise TimeoutError("timed out")

    def wait_for(self, h, seconds):
        return {"hash": h} if self.appears else None

    def torrents(self, hashes=None):
        return [{"hash": "aa", "ratio_limit": self.limits[0], "seeding_time_limit": self.limits[1]}]

    def set_share_limits(self, h, ratio, minutes):
        self.limit_sets += 1
        if self.limit_sets > self.lost:
            self.limits = (ratio, minutes)

    def recheck(self, h):
        self.rechecks += 1


class InjectTwin(unittest.TestCase):
    SRC = {"hash": "bb", "name": "Show.S01E01", "save_path": "/media/tv", "category": "tv"}
    FILES = {"Show.S01E01.mkv": 100}

    def inject(self, qb):
        db = t.open_db(":memory:")
        cfg = t.Config({})
        cfg.qbit_timeout = 1
        cache = {"SiteA": (1.0, 4320, 4320)}  # seed limits, no Prowlarr lookup
        orig, t.time.sleep = t.time.sleep, lambda s: None
        try:
            with self.assertLogs("twinarr", level="INFO"):
                t.inject_twin(cfg, qb, db, cache, "SiteA", "http://idx.example/1/api",
                              self.SRC, b"", "aa", self.FILES)
        finally:
            t.time.sleep = orig
            self.rows = db.execute("SELECT twin_hash, status FROM twins").fetchall()

    def test_normal_add(self):
        qb = InjectQB()
        self.inject(qb)
        self.assertEqual((self.rows, qb.rechecks, qb.limits), ([("aa", "checking")], 1, (1.0, 4320)))

    def test_timed_out_add_that_was_accepted_is_finished(self):
        """qBittorrent too busy to answer the add, but it took the twin: limits, recheck, record."""
        qb = InjectQB(add_times_out=True)
        self.inject(qb)
        self.assertEqual((self.rows, qb.rechecks, qb.limits), ([("aa", "checking")], 1, (1.0, 4320)))

    def test_timed_out_add_that_never_appears_fails(self):
        qb = InjectQB(add_times_out=True, appears=False)
        with self.assertRaises(TimeoutError):
            self.inject(qb)
        self.assertEqual((self.rows, qb.rechecks), ([], 0))

    def test_lost_seed_limits_are_sent_again(self):
        qb = InjectQB(lost_limit_sets=1)
        self.inject(qb)
        self.assertEqual((qb.limit_sets, qb.limits), (2, (1.0, 4320)))


class Config(unittest.TestCase):
    def test_targets(self):
        self.assertEqual(t.parse_targets("one|http://p.example/1/api, two|https://p.example/2/api"),
                         [("one", "http://p.example/1/api"), ("two", "https://p.example/2/api")])
        for bad in ("noname", "x|ftp://a", "|http://a"):
            with self.assertRaises(ValueError):
                t.parse_targets(bad)

    def test_redact(self):
        self.assertEqual(t.redact("http://x/api?apikey=SECRET&q=1"), "http://x/api?apikey=***&q=1")

    def test_next_run(self):
        base = datetime(2030, 1, 1, 4, 0)
        self.assertEqual(t.next_run("03:30", base), datetime(2030, 1, 2, 3, 30))
        self.assertEqual(t.next_run("05:00", base), datetime(2030, 1, 1, 5, 0))


class SavedSettings(unittest.TestCase):
    def setUp(self):
        self.old = dict(os.environ)
        os.environ.update({"SOURCE_TRACKERS": "env-src.example", "TARGETS": "envt|http://idx.example/1/api",
                           "PROWLARR_URL": "http://prowlarr.example:9696", "PROWLARR_APIKEY": "k"})

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old)

    def test_env_only(self):
        cfg = t.Config({})
        self.assertEqual(cfg.sources, ["env-src.example"])
        self.assertEqual(cfg.targets, [("envt", "http://idx.example/1/api")])
        self.assertEqual(cfg.problems(), [])

    def test_saved_wins(self):
        cfg = t.Config({"sources": "a.example,b.example", "run_at": "05:15", "mode": "inject",
                        "targets": '[{"id": 7, "name": "Seven"}]'})
        self.assertEqual(cfg.sources, ["a.example", "b.example"])
        self.assertEqual(cfg.targets, [("Seven", "http://prowlarr.example:9696/7/api")])
        self.assertEqual((cfg.run_at, cfg.mode, cfg.apikey), ("05:15", "inject", "k"))

    def test_problems(self):
        cfg = t.Config({"sources": "", "targets": "[]"})
        self.assertEqual(len(cfg.problems()), 2)

    def test_valid_time(self):
        self.assertTrue(t.valid_time("03:30"))
        for bad in ("24:00", "3:7", "aa:bb", ""):
            self.assertFalse(t.valid_time(bad))


class WebUI(unittest.TestCase):
    """Start the real web server on a free port with a temp database (no qBittorrent needed)."""

    @classmethod
    def setUpClass(cls):
        import tempfile
        import threading
        cls.tmp = tempfile.mkdtemp()
        cls.old = dict(os.environ)
        os.environ.update({"SOURCE_TRACKERS": "", "TARGETS": "", "QBIT_URL": "http://127.0.0.1:9",
                           "UI_PASSWORD": "pw"})
        os.environ.pop("PROWLARR_URL", None)
        t.DB_PATH = os.path.join(cls.tmp, "twinarr.db")
        t.db_conn().close()
        cls.srv = t.ThreadingHTTPServer(("127.0.0.1", 0), t.Web)
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        os.environ.clear()
        os.environ.update(cls.old)

    def req(self, path, data=None, auth=True, origin=True):
        import base64
        import urllib.error
        import urllib.parse
        import urllib.request
        h = {}
        if auth:
            h["Authorization"] = "Basic " + base64.b64encode(b"x:pw").decode()
        if origin:
            h["Origin"] = self.base
        body = urllib.parse.urlencode(data, doseq=True).encode() if data is not None else None
        try:
            with urllib.request.urlopen(urllib.request.Request(self.base + path, data=body, headers=h)) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    def test_auth_and_pages(self):
        self.assertEqual(self.req("/", auth=False)[0], 401)
        self.assertEqual(self.req("/health", auth=False)[0], 200)
        code, body = self.req("/")
        self.assertEqual(code, 200)
        self.assertIn("Not set up", body)
        code, body = self.req("/settings")
        self.assertEqual(code, 200)
        self.assertIn("Could not read qBittorrent", body)

    def test_settings_post(self):
        self.assertEqual(self.req("/settings", {"run_at": "04:45", "mode": "report"}, origin=False)[0], 403)
        code, body = self.req("/settings", {"run_at": "04:45", "mode": "inject", "src": ["One.Example"],
                                            "src_extra": "two.example, bad host!"})
        self.assertIn("Saved.", body)
        cfg = t.current_config()
        self.assertEqual((cfg.run_at, cfg.mode, cfg.sources), ("04:45", "inject", ["one.example", "two.example"]))
        self.assertIn("Not saved", self.req("/settings", {"run_at": "99:00", "mode": "report"})[1])


if __name__ == "__main__":
    unittest.main()
