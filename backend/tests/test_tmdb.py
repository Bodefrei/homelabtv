import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tmdb


class FakeResponse:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class FakeAsyncClient:
    def __init__(self, response):
        self.response = response
        self.url = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None

    async def get(self, url):
        self.url = url
        return self.response


class TmdbSearchTests(unittest.IsolatedAsyncioTestCase):
    async def test_movie_search_returns_movie_title_and_artwork(self):
        response = FakeResponse({
            "results": [{
                "id": 42,
                "title": "Example Film",
                "overview": "A film description",
                "poster_path": "/poster.jpg",
                "backdrop_path": "/backdrop.jpg",
            }],
        })
        client = FakeAsyncClient(response)

        with (
            patch.object(tmdb, "TMDB_API_KEY", "test-key"),
            patch.object(tmdb.httpx, "AsyncClient", return_value=client),
        ):
            result = await tmdb.search_tmdb("Example Film", "movie")

        self.assertIn("/search/movie?", client.url)
        self.assertEqual(result["title"], "Example Film")
        self.assertEqual(result["poster_url"], "https://image.tmdb.org/t/p/w500/poster.jpg")
        self.assertEqual(result["backdrop_url"], "https://image.tmdb.org/t/p/w1280/backdrop.jpg")

    async def test_tv_search_keeps_existing_name_field(self):
        client = FakeAsyncClient(FakeResponse({"results": [{"id": 7, "name": "Example Series"}]}))

        with (
            patch.object(tmdb, "TMDB_API_KEY", "test-key"),
            patch.object(tmdb.httpx, "AsyncClient", return_value=client),
        ):
            result = await tmdb.search_tmdb("Example Series")

        self.assertIn("/search/tv?", client.url)
        self.assertEqual(result["title"], "Example Series")
