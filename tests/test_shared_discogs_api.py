import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIRECTORY = PROJECT_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS_DIRECTORY))

import discogs_style_enricher as enricher  # noqa: E402
import discogs_tracklists as tracklists  # noqa: E402
from shared import discogs_api  # noqa: E402


class SharedDiscogsApiTests(unittest.TestCase):
    def test_workflow_modules_use_shared_discogs_api_infrastructure(self):
        self.assertIs(enricher.DiscogsRateLimiter, discogs_api.DiscogsRateLimiter)
        self.assertIs(enricher.http_get, discogs_api.http_get)
        self.assertIs(enricher.make_http_json_getter, discogs_api.make_http_json_getter)
        self.assertIs(tracklists.DiscogsRateLimiter, discogs_api.DiscogsRateLimiter)
        self.assertIs(tracklists.http_get, discogs_api.http_get)
        self.assertEqual(enricher.DISCOGS_API_ROOT, discogs_api.DISCOGS_API_ROOT)
        self.assertEqual(tracklists.DISCOGS_API_ROOT, discogs_api.DISCOGS_API_ROOT)

    def test_default_discogs_rate_limit_reflects_authentication(self):
        self.assertEqual(discogs_api.default_discogs_rate_limit("token"), 60)
        self.assertEqual(discogs_api.default_discogs_rate_limit(""), 25)


if __name__ == "__main__":
    unittest.main()
