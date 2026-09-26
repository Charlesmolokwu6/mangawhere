import unittest
from unittest.mock import AsyncMock, patch

from server import turnstile


class TurnstileTests(unittest.IsolatedAsyncioTestCase):
    def test_not_configured_without_secret_key(self):
        with patch.object(turnstile, "SECRET_KEY", ""):
            self.assertFalse(turnstile.is_configured())

    def test_configured_with_secret_key(self):
        with patch.object(turnstile, "SECRET_KEY", "fake-secret"):
            self.assertTrue(turnstile.is_configured())

    async def test_verify_returns_false_without_secret_key(self):
        with patch.object(turnstile, "SECRET_KEY", ""):
            self.assertFalse(await turnstile.verify("some-token"))

    async def test_verify_returns_false_without_token(self):
        with patch.object(turnstile, "SECRET_KEY", "fake-secret"):
            self.assertFalse(await turnstile.verify(""))
            self.assertFalse(await turnstile.verify(None))

    async def test_verify_returns_true_on_cloudflare_success(self):
        mock_response = AsyncMock()
        mock_response.json = lambda: {"success": True}
        with patch.object(turnstile, "SECRET_KEY", "fake-secret"), \
             patch("httpx.AsyncClient.post", return_value=mock_response):
            self.assertTrue(await turnstile.verify("good-token"))

    async def test_verify_returns_false_on_cloudflare_rejection(self):
        mock_response = AsyncMock()
        mock_response.json = lambda: {"success": False, "error-codes": ["invalid-input-response"]}
        with patch.object(turnstile, "SECRET_KEY", "fake-secret"), \
             patch("httpx.AsyncClient.post", return_value=mock_response):
            self.assertFalse(await turnstile.verify("bad-token"))

    async def test_verify_returns_false_on_network_failure(self):
        with patch.object(turnstile, "SECRET_KEY", "fake-secret"), \
             patch("httpx.AsyncClient.post", side_effect=RuntimeError("network down")):
            self.assertFalse(await turnstile.verify("some-token"))


if __name__ == "__main__":
    unittest.main()
