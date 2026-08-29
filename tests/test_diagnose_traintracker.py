import contextlib
import io
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts import diagnose_traintracker as diagnostic


class Response:
    def __init__(self, body, status=200, content_type="application/json; charset=utf-8"):
        self.body = io.BytesIO(body)
        self.status = status
        self.headers = {"Content-Type": content_type, "X-Secret": "header-secret"}

    def read(self, size=-1):
        return self.body.read(size)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class DiagnoseTrainTrackerTests(unittest.TestCase):
    def test_summarizes_list_object_missing_and_null_train_forms(self):
        document = {"ctatt": {"errCd": "0", "errNm": None, "route": [
            {"@name": "red", "train": [{"rn": "1"}, None, "bad"]},
            {"@name": "blue", "train": {"rn": "2"}},
            {"@name": "brn"},
            {"@name": "g", "train": None},
        ]}}
        result = diagnostic.summarize(document, 200, "application/json", 123)
        self.assertEqual(result["http_status"], 200)
        self.assertEqual(result["content_type"], "application/json")
        self.assertEqual(result["byte_count"], 123)
        self.assertEqual(result["top_level_key_names"], ["ctatt"])
        self.assertEqual(result["root_key_names"], ["errCd", "errNm", "route"])
        self.assertEqual(result["cta_error_code"], 0)
        self.assertTrue(result["cta_error_name_present"])
        self.assertEqual(result["route_field_type"], "array")
        self.assertEqual(result["route_count"], 4)
        routes = result["routes"]
        self.assertEqual(routes[0], {"index": 0, "route_name": "red", "train_type": "array",
                                     "train_count": 3, "train_member_types": ["null", "object", "other"],
                                     "train_key_names": ["rn"]})
        self.assertEqual(routes[1]["train_type"], "object")
        self.assertEqual(routes[1]["train_count"], 1)
        self.assertEqual(routes[2]["train_type"], "missing")
        self.assertEqual(routes[3]["train_type"], "null")

    def test_hostile_values_are_bounded_and_never_echoed(self):
        secret = "do-not-leak-key-body-url-secret"
        routes = [{"@name": secret, "train": [{secret: secret}]} for _ in range(100)]
        document = {"ctatt": {"errCd": secret, "errNm": secret, "route": routes,
                               **{f"unknown-{i}-{secret}": i for i in range(100)}},
                    secret: secret}
        rendered = json.dumps(diagnostic.summarize(document, 200, secret, 999), sort_keys=True)
        self.assertNotIn(secret, rendered)
        self.assertEqual(len(diagnostic.summarize(document, 200, secret, 999)["routes"]), 8)
        self.assertLessEqual(len(diagnostic.summarize(document, 200, secret, 999)["root_key_names"]),
                             diagnostic.MAX_KEY_NAMES)
        self.assertIn("<other>", rendered)
        self.assertIn('"cta_error_code": null', rendered)
        self.assertIn('"content_type": "unknown"', rendered)
        malformed = diagnostic.summarize({"ctatt": {}}, 9999, "text/plain", 0)
        self.assertEqual(malformed["route_field_type"], "missing")
        self.assertIsNone(malformed["http_status"])

    def test_main_loads_config_makes_one_fixed_bounded_request_and_prints_json(self):
        seen = []
        body = json.dumps({"ctatt": {"errCd": "0", "route": []}}).encode()

        def fetch(request, timeout):
            seen.append((request, timeout))
            return Response(body)

        config = type("Config", (), {"environment": {"CTA_API_KEY": "credential-secret"}})()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = diagnostic.main(config=config, fetcher=fetch)
        self.assertEqual(result, 0)
        self.assertEqual(len(seen), 1)
        request, timeout = seen[0]
        parsed = urlsplit(request.full_url)
        query = parse_qs(parsed.query)
        self.assertEqual((parsed.scheme, parsed.netloc, parsed.path),
                         ("https", "lapi.transitchicago.com", "/api/1.0/ttpositions.aspx"))
        self.assertEqual(query["rt"], ["red,blue,brn,g,org,p,pink,y"])
        self.assertEqual(query["outputType"], ["JSON"])
        self.assertEqual(timeout, diagnostic.REQUEST_TIMEOUT_SECONDS)
        self.assertNotIn("credential-secret", stdout.getvalue())
        self.assertEqual(json.loads(stdout.getvalue())["byte_count"], len(body))

    def test_default_fetcher_installs_no_redirect_handler(self):
        response = Response(b'{"ctatt":{"errCd":"0","route":[]}}')
        opener = unittest.mock.Mock()
        opener.open.return_value = response
        with patch.object(diagnostic.urllib.request, "build_opener", return_value=opener) as build:
            diagnostic.run_diagnostic({"CTA_API_KEY": "credential"})
        self.assertEqual(len(build.call_args.args), 1)
        self.assertIsInstance(build.call_args.args[0], diagnostic.NoRedirectHandler)
        self.assertEqual(opener.open.call_count, 1)

    def test_response_limit_and_failures_are_allowlisted_without_details(self):
        secret = "exception-url-credential-secret"
        config = type("Config", (), {"environment": {"CTA_API_KEY": "key"}})()
        cases = [
            (lambda *_a, **_k: Response(b"x" * (diagnostic.MAX_RESPONSE_BYTES + 1)),
             "response_too_large"),
            (lambda *_a, **_k: Response(b"not-json"), "malformed_json"),
            (lambda *_a, **_k: (_ for _ in ()).throw(type(secret, (Exception,), {})()),
             "unexpected"),
            (lambda *_a, **_k: (_ for _ in ()).throw(
                diagnostic.DiagnosticFailure(secret)), "unexpected"),
        ]
        for fetcher, expected in cases:
            stdout = io.StringIO()
            with self.subTest(expected=expected), contextlib.redirect_stdout(stdout):
                result = diagnostic.main(config=config, fetcher=fetcher)
            self.assertEqual(result, 1)
            self.assertEqual(json.loads(stdout.getvalue()),
                             {"status": "diagnostic_failed", "error_type": expected})
            self.assertNotIn(secret, stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
