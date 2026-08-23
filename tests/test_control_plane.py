try:
    import _gitenv  # noqa: F401
except ImportError:
    from tests import _gitenv  # noqa: F401
import json, os, shutil, tempfile, unittest
from pathlib import Path


class ControlPlaneTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()); os.environ["HELM_HOME"] = str(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_scope_intersection_is_conservative_and_disjoint_prefixes_parallelize(self):
        from helm import scope
        self.assertTrue(scope.overlap(["src/*.py"], ["src/a*"]))
        self.assertTrue(scope.overlap(["unknown"], ["docs/**"]))
        self.assertTrue(scope.overlap([".github/workflows/ci.yml"], ["src/**"]))
        self.assertFalse(scope.overlap(["src/api/**"], ["src/web/**"]))
        self.assertTrue(scope.claim("p", "one", ["src/api/**"], "a", os.getpid()))
        self.assertTrue(scope.claim("p", "two", ["src/web/**"], "b", os.getpid()))
        self.assertFalse(scope.claim("p", "three", ["src/a*"], "c", os.getpid()))

    def test_cas_rejects_stale_writer_and_redacts_every_nested_surface(self):
        from helm import control
        path = self.root / "work" / "x" / "item.json"; path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"id": "x", "revision": 0, "status": "queued", "history": []}))
        control.cas_update("x", lambda item: item.update(phase="plan"), expected_revision=0)
        with self.assertRaises(SystemExit):
            control.cas_update("x", lambda item: item.update(phase="bad"), expected_revision=0)
        redacted = control.redact({"history": ["Authorization: Bearer abcdefghijk"],
                                   "blocker": "api_key=super-secret-value", "token": "raw"})
        self.assertNotIn("abcdefghijk", json.dumps(redacted))
        self.assertNotIn("super-secret-value", json.dumps(redacted))
        self.assertEqual(redacted["token"], "[REDACTED]")

    def test_rigor_routes_and_escalates_explainably(self):
        from helm import rigor
        scout = rigor.route({"kind": "scout", "text": "why", "scope": {"paths": ["unknown"]}})
        self.assertEqual(scout["level"], "scout")
        high = rigor.route({"kind": "ship", "text": "change auth permissions", "scope": {"paths": ["src/auth.py"]}})
        self.assertEqual(high["level"], "high-risk")
        quick = rigor.route({"kind": "ship", "text": "fix README typo", "scope": {"paths": ["README.md"]}})
        self.assertEqual(quick["level"], "quick")
        escalated = rigor.escalate(quick, verification_failed=True)
        self.assertEqual(escalated["level"], "high-risk"); self.assertIn("verification", escalated["rationale"])

    def test_review_parser_requires_real_evidence(self):
        from helm.work import _json_verdict
        self.assertEqual(_json_verdict('noise {"verdict":"accept","notes":"ok"}')["verdict"], "accept")
        with self.assertRaises(SystemExit):
            _json_verdict("looks good")


if __name__ == "__main__":
    unittest.main()
