"""Token del panel web (Fase 1, 2026-09-27, ver CLAUDE.md): issue()/verify() sin tenant_id (una
sola cuenta), pero con el mismo rigor de firma que sniperfantasy."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("FANTASY_DATA_DIR", tempfile.mkdtemp())
os.environ.setdefault("DASHBOARD_TOKEN_SECRET", "test-secret-no-usar-en-real")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fantasy_agent import dashboard_token  # noqa: E402


class DashboardTokenTest(unittest.TestCase):
    def test_issued_token_verifies(self):
        token = dashboard_token.issue()
        self.assertTrue(dashboard_token.verify(token))

    def test_garbage_does_not_verify(self):
        self.assertFalse(dashboard_token.verify("no-soy-un-token"))
        self.assertFalse(dashboard_token.verify(""))

    def test_tampered_signature_does_not_verify(self):
        token = dashboard_token.issue()
        expires_at, sig = token.split(".", 1)
        self.assertFalse(dashboard_token.verify(f"{expires_at}.{sig[:-1]}x"))

    def test_expired_token_does_not_verify(self):
        expires_at = int(time.time()) - 10
        import hashlib
        import hmac

        sig = hmac.new(
            os.environ["DASHBOARD_TOKEN_SECRET"].encode(),
            f"dash.{expires_at}".encode(), hashlib.sha256,
        ).hexdigest()
        self.assertFalse(dashboard_token.verify(f"{expires_at}.{sig}"))

    def test_ttl_is_long_lived(self):
        self.assertGreaterEqual(dashboard_token.TOKEN_TTL_SECONDS, 60 * 24 * 3600)


if __name__ == "__main__":
    unittest.main()
