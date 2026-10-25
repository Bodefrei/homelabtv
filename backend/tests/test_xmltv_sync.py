import asyncio
import gzip
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import xmltv_sync


class AsyncBytes(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content

    async def aclose(self):
        pass


def xmltv_time(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y%m%d%H%M%S +0000")


def make_xmltv(now):
    root = ET.Element("tv")
    ET.SubElement(root, "channel", id="test.channel")
    for start, stop, title in (
        (now - 4 * 3600, now - 3 * 3600, "Old show"),
        (now - 30 * 60, now + 30 * 60, "Current show"),
        (now + 31 * 3600, now + 32 * 3600, "Far future"),
    ):
        program = ET.SubElement(
            root,
            "programme",
            channel="test.channel",
            start=xmltv_time(start),
            stop=xmltv_time(stop),
        )
        ET.SubElement(program, "title").text = title
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


class XmltvSyncTests(unittest.TestCase):
    def test_prunes_old_and_distant_programmes_but_keeps_channel_metadata(self):
        now = 2_000_000_000
        cleaned = xmltv_sync._prune_xmltv(make_xmltv(now), now, past_hours=1, future_hours=30)
        root = ET.fromstring(cleaned)

        self.assertEqual([channel.get("id") for channel in root.findall("channel")], ["test.channel"])
        self.assertEqual([program.findtext("title") for program in root.findall("programme")], ["Current show"])

    def test_download_replaces_file_with_pruned_xmltv(self):
        async def run():
            now = 2_000_000_000
            compressed_feed = gzip.compress(make_xmltv(now))
            transport = httpx.MockTransport(
                lambda request: httpx.Response(200, stream=AsyncBytes(compressed_feed), request=request)
            )
            client = httpx.AsyncClient(transport=transport)
            with tempfile.TemporaryDirectory() as temp_dir:
                destination = Path(temp_dir) / "automatic.xml"
                destination.write_text("previous feed")
                with (
                    patch("xmltv_sync.httpx.AsyncClient", return_value=client),
                    patch("xmltv_sync.time.time", return_value=now),
                ):
                    await xmltv_sync.download_xmltv(
                        "https://example.test/epg.xml.gz",
                        str(destination),
                        past_hours=1,
                        future_hours=30,
                    )
                root = ET.parse(destination).getroot()
                self.assertEqual([program.findtext("title") for program in root.findall("programme")], ["Current show"])
            await client.aclose()

        asyncio.run(run())

    def test_invalid_download_does_not_replace_existing_file(self):
        async def run():
            transport = httpx.MockTransport(
                lambda request: httpx.Response(200, stream=AsyncBytes(b"not gzip"), request=request)
            )
            client = httpx.AsyncClient(transport=transport)
            with tempfile.TemporaryDirectory() as temp_dir:
                destination = Path(temp_dir) / "automatic.xml"
                destination.write_text("previous valid feed")
                with patch("xmltv_sync.httpx.AsyncClient", return_value=client):
                    with self.assertRaises(OSError):
                        await xmltv_sync.download_xmltv(
                            "https://example.test/epg.xml.gz",
                            str(destination),
                            past_hours=1,
                            future_hours=30,
                        )
                self.assertEqual(destination.read_text(), "previous valid feed")
            await client.aclose()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
