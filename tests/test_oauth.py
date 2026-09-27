import unittest
from unittest.mock import AsyncMock, patch

from server import oauth


class ConfiguredTests(unittest.TestCase):
    def test_google_not_configured_without_client_id(self):
        with patch.object(oauth, "GOOGLE_CLIENT_ID", ""):
            self.assertFalse(oauth.google_configured())

    def test_google_configured_with_client_id(self):
        with patch.object(oauth, "GOOGLE_CLIENT_ID", "fake-client-id"):
            self.assertTrue(oauth.google_configured())

    def test_facebook_needs_both_app_id_and_secret(self):
        with patch.object(oauth, "FACEBOOK_APP_ID", "fake-id"), \
             patch.object(oauth, "FACEBOOK_APP_SECRET", ""):
            self.assertFalse(oauth.facebook_configured())
        with patch.object(oauth, "FACEBOOK_APP_ID", "fake-id"), \
             patch.object(oauth, "FACEBOOK_APP_SECRET", "fake-secret"):
            self.assertTrue(oauth.facebook_configured())


class VerifyGoogleTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_without_client_id(self):
        with patch.object(oauth, "GOOGLE_CLIENT_ID", ""):
            self.assertIsNone(await oauth.verify_google("some-token"))

    async def test_returns_none_without_token(self):
        with patch.object(oauth, "GOOGLE_CLIENT_ID", "fake-client-id"):
            self.assertIsNone(await oauth.verify_google(""))

    async def test_returns_profile_on_valid_matching_token(self):
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = lambda: {
            "aud": "fake-client-id",
            "sub": "1234567890",
            "email": "reader@example.com",
            "email_verified": "true",
            "name": "Reader",
            "picture": "https://example.com/pic.jpg",
        }
        with patch.object(oauth, "GOOGLE_CLIENT_ID", "fake-client-id"), \
             patch("httpx.AsyncClient.get", return_value=mock_response):
            profile = await oauth.verify_google("good-token")
        self.assertEqual(profile["subject"], "1234567890")
        self.assertEqual(profile["email"], "reader@example.com")
        self.assertEqual(profile["name"], "Reader")

    async def test_rejects_token_issued_for_a_different_app(self):
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = lambda: {
            "aud": "someone-elses-client-id",
            "sub": "1234567890",
            "email": "reader@example.com",
            "email_verified": "true",
        }
        with patch.object(oauth, "GOOGLE_CLIENT_ID", "fake-client-id"), \
             patch("httpx.AsyncClient.get", return_value=mock_response):
            self.assertIsNone(await oauth.verify_google("token-for-another-app"))

    async def test_rejects_unverified_email(self):
        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.json = lambda: {
            "aud": "fake-client-id",
            "sub": "1234567890",
            "email": "reader@example.com",
            "email_verified": "false",
        }
        with patch.object(oauth, "GOOGLE_CLIENT_ID", "fake-client-id"), \
             patch("httpx.AsyncClient.get", return_value=mock_response):
            self.assertIsNone(await oauth.verify_google("token"))

    async def test_returns_none_on_network_failure(self):
        with patch.object(oauth, "GOOGLE_CLIENT_ID", "fake-client-id"), \
             patch("httpx.AsyncClient.get", side_effect=RuntimeError("network down")):
            self.assertIsNone(await oauth.verify_google("token"))


class VerifyFacebookTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_none_without_app_secret(self):
        with patch.object(oauth, "FACEBOOK_APP_ID", "fake-id"), \
             patch.object(oauth, "FACEBOOK_APP_SECRET", ""):
            self.assertIsNone(await oauth.verify_facebook("some-token"))

    async def test_returns_profile_on_valid_token(self):
        debug_response = AsyncMock()
        debug_response.json = lambda: {"data": {"is_valid": True, "app_id": "fake-id"}}
        profile_response = AsyncMock()
        profile_response.status_code = 200
        profile_response.json = lambda: {
            "id": "9999",
            "name": "Reader",
            "email": "reader@example.com",
            "picture": {"data": {"url": "https://example.com/pic.jpg"}},
        }
        with patch.object(oauth, "FACEBOOK_APP_ID", "fake-id"), \
             patch.object(oauth, "FACEBOOK_APP_SECRET", "fake-secret"), \
             patch("httpx.AsyncClient.get", side_effect=[debug_response, profile_response]):
            profile = await oauth.verify_facebook("good-token")
        self.assertEqual(profile["subject"], "9999")
        self.assertEqual(profile["email"], "reader@example.com")

    async def test_rejects_token_issued_for_a_different_app(self):
        debug_response = AsyncMock()
        debug_response.json = lambda: {"data": {"is_valid": True, "app_id": "someone-elses-app"}}
        with patch.object(oauth, "FACEBOOK_APP_ID", "fake-id"), \
             patch.object(oauth, "FACEBOOK_APP_SECRET", "fake-secret"), \
             patch("httpx.AsyncClient.get", return_value=debug_response):
            self.assertIsNone(await oauth.verify_facebook("token-for-another-app"))

    async def test_rejects_account_with_no_email_on_file(self):
        debug_response = AsyncMock()
        debug_response.json = lambda: {"data": {"is_valid": True, "app_id": "fake-id"}}
        profile_response = AsyncMock()
        profile_response.status_code = 200
        profile_response.json = lambda: {"id": "9999", "name": "Reader"}
        with patch.object(oauth, "FACEBOOK_APP_ID", "fake-id"), \
             patch.object(oauth, "FACEBOOK_APP_SECRET", "fake-secret"), \
             patch("httpx.AsyncClient.get", side_effect=[debug_response, profile_response]):
            self.assertIsNone(await oauth.verify_facebook("token"))

    async def test_returns_none_on_network_failure(self):
        with patch.object(oauth, "FACEBOOK_APP_ID", "fake-id"), \
             patch.object(oauth, "FACEBOOK_APP_SECRET", "fake-secret"), \
             patch("httpx.AsyncClient.get", side_effect=RuntimeError("network down")):
            self.assertIsNone(await oauth.verify_facebook("token"))


if __name__ == "__main__":
    unittest.main()
