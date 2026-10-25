import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from xmltv_parser import parse_xmltv_programs


class XmltvProgramImageTests(unittest.TestCase):
    def test_programme_image_and_categories_are_parsed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            guide = Path(temp_dir) / "guide.xml"
            guide.write_text(
                """
                <tv>
                  <programme channel="movie.channel" start="20261025140000 +0000" stop="20261025160000 +0000">
                    <title>Example Film</title>
                    <category>Film</category>
                    <icon src="https://images.example/poster.jpg" />
                  </programme>
                </tv>
                """,
                encoding="utf-8",
            )

            programs = parse_xmltv_programs(str(guide))

        self.assertEqual(len(programs), 1)
        self.assertEqual(programs[0]["categories"], ["Film"])
        self.assertEqual(programs[0]["image_url"], "https://images.example/poster.jpg")
