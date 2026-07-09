import hashlib
import hmac
import unittest

from server import Settings, has_source_of_truth_deviation, parse_resolved_prior_issues, verify_github_signature


class ServerTests(unittest.TestCase):
    def test_github_signature(self):
        body = b'{"ok": true}'
        secret = "secret"
        signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_github_signature(secret, body, signature))
        self.assertFalse(verify_github_signature(secret, body, "sha256=bad"))

    def test_empty_secret_disables_signature_check(self):
        self.assertTrue(verify_github_signature("", b"body", None))

    def test_deviation_marker(self):
        self.assertTrue(has_source_of_truth_deviation("SOURCE_OF_TRUTH_DEVIATION: yes\n"))
        self.assertFalse(has_source_of_truth_deviation("SOURCE_OF_TRUTH_DEVIATION: no\n"))

    def test_resolved_prior_issues(self):
        report = "RESOLVED_PRIOR_ISSUES: auth regression; missing tests\n"
        self.assertEqual(parse_resolved_prior_issues(report), ["auth regression", "missing tests"])
        self.assertEqual(parse_resolved_prior_issues("RESOLVED_PRIOR_ISSUES: none\n"), [])

    def test_default_review_provider_is_codex(self):
        settings = Settings.load()
        self.assertIn(settings.review_provider, {"codex", "claude"})


if __name__ == "__main__":
    unittest.main()
