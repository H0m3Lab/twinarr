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


if __name__ == "__main__":
    unittest.main()
