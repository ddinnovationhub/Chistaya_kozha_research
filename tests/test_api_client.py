"""Проверка веток handle_api_response: 200, 402, 429, 401, 403, 503, прочие.
Запуск: python -m pytest tests/ -q  (или python tests/test_api_client.py)"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.api_client import handle_api_response
from src.errors import AuthError, QuotaExhaustedError


class FakeResponse:
    def __init__(self, code):
        self.status_code = code


class TestHandleApiResponse(unittest.TestCase):
    def test_200_returns_response(self):
        r = FakeResponse(200)
        self.assertIs(handle_api_response(r, "X"), r)

    def test_402_raises_quota(self):
        with self.assertRaises(QuotaExhaustedError):
            handle_api_response(FakeResponse(402), "X")

    def test_429_raises_quota(self):
        with self.assertRaises(QuotaExhaustedError):
            handle_api_response(FakeResponse(429), "X")

    def test_401_raises_auth(self):
        with self.assertRaises(AuthError):
            handle_api_response(FakeResponse(401), "X")

    def test_403_raises_auth(self):
        with self.assertRaises(AuthError):
            handle_api_response(FakeResponse(403), "X")

    def test_503_returns_none_after_wait(self):
        with patch("time.sleep") as slept:
            self.assertIsNone(handle_api_response(FakeResponse(503), "X"))
            slept.assert_called_once_with(30)

    def test_504_returns_none_after_wait(self):
        with patch("time.sleep"):
            self.assertIsNone(handle_api_response(FakeResponse(504), "X"))

    def test_other_code_returns_none(self):
        self.assertIsNone(handle_api_response(FakeResponse(418), "X"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
