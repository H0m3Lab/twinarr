import hashlib
import os
import sys
import unittest
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
