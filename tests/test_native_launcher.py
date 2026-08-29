import contextlib
import http.server
import io
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from cta_pipeline import native_launcher


class DotenvTests(unittest.TestCase):
    def test_parses_basic_values_without_executing_shell_syntax(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "executed"
            env_file = Path(directory) / ".env"
            env_file.write_text(
                "# comment\nexport CTA_API_KEY='cta key'\n"
                'OPENAI_MODEL="local model"\nPLAIN=value # retained as data\n'
                f"DANGEROUS=$(touch {marker})\n",
                encoding="utf-8",
            )
            parsed = native_launcher.parse_dotenv(env_file)
        self.assertEqual(parsed["CTA_API_KEY"], "cta key")
        self.assertEqual(parsed["OPENAI_MODEL"], "local model")
        self.assertEqual(parsed["PLAIN"], "value # retained as data")
        self.assertEqual(parsed["DANGEROUS"], f"$(touch {marker})")
        self.assertFalse(marker.exists())

    def test_rejects_malformed_lines_names_and_nuls_without_values(self):
        cases = ("NO_EQUALS", "BAD-NAME=secret", "GOOD=hidden\x00tail")
        for content in cases:
            with self.subTest(content=content.split("=", 1)[0]):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / ".env"
                    path.write_text(content + "\n", encoding="utf-8")
                    with self.assertRaises(native_launcher.ConfigError) as raised:
                        native_launcher.parse_dotenv(path)
                self.assertNotIn("secret", str(raised.exception))
                self.assertNotIn("hidden", str(raised.exception))

    def test_read_errors_are_generic_for_directory_unreadable_and_missing_race(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = (Path(directory), Path(directory) / "vanished-secret.env")
            for path in paths:
                with self.subTest(kind="directory" if path.is_dir() else "missing"):
                    with self.assertRaises(native_launcher.ConfigError) as raised:
                        native_launcher.parse_dotenv(path)
                    self.assertEqual(str(raised.exception), "unable to read env file")
                    self.assertNotIn(str(path), str(raised.exception))
            with patch.object(Path, "read_text", side_effect=PermissionError("secret-path")):
                with self.assertRaises(native_launcher.ConfigError) as raised:
                    native_launcher.parse_dotenv(Path(directory) / "unreadable-secret.env")
            self.assertEqual(str(raised.exception), "unable to read env file")
            self.assertNotIn("secret", str(raised.exception))


class NativeLauncherTests(unittest.TestCase):
    def make_repo(self, directory):
        repo = Path(directory)
        (repo / "src" / "cta_pipeline").mkdir(parents=True)
        return repo

    def test_check_honors_openai_base_url_from_dotenv(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            (repo / ".env").write_text(
                "CTA_API_KEY=key\nOPENAI_BASE_URL=https://llm.example.test/v1\n",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = native_launcher.main(["--check"], repo_root=repo, environ={})
        self.assertEqual(result, 0)
        self.assertIn(
            "OpenAI base URL: https://llm.example.test/v1", stdout.getvalue()
        )

    def test_process_openai_base_url_overrides_dotenv_value(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            (repo / ".env").write_text(
                "CTA_API_KEY=file-key\nOPENAI_BASE_URL=https://file.example.test/v1\n",
                encoding="utf-8",
            )
            config = native_launcher.build_config(
                [], repo,
                {"CTA_API_KEY": "process-key",
                 "OPENAI_BASE_URL": "https://process.example.test/v1"},
            )
        self.assertEqual(config.openai_base_url, "https://process.example.test/v1")
        self.assertEqual(
            config.environment["OPENAI_BASE_URL"],
            "https://process.example.test/v1",
        )

    def test_native_openai_base_url_overrides_generic_value(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            config = native_launcher.build_config(
                [], repo,
                {"CTA_API_KEY": "key",
                 "OPENAI_BASE_URL": "https://generic.example.test/v1",
                 "OPENAI_NATIVE_BASE_URL": "https://native.example.test/v1"},
            )
        self.assertEqual(config.openai_base_url, "https://native.example.test/v1")
        self.assertEqual(
            config.environment["OPENAI_BASE_URL"],
            "https://native.example.test/v1",
        )

    def test_cli_openai_base_url_overrides_native_and_generic_values(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            config = native_launcher.build_config(
                ["--openai-base-url", "https://cli.example.test/v1"], repo,
                {"CTA_API_KEY": "key",
                 "OPENAI_BASE_URL": "https://generic.example.test/v1",
                 "OPENAI_NATIVE_BASE_URL": "https://native.example.test/v1"},
            )
        self.assertEqual(config.openai_base_url, "https://cli.example.test/v1")
        self.assertEqual(
            config.environment["OPENAI_BASE_URL"], "https://cli.example.test/v1"
        )

    def test_invalid_openai_base_url_from_dotenv_is_rejected_without_echo(self):
        hostile = "https://user:hostile-secret@example.test/v1"
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            (repo / ".env").write_text(
                f"CTA_API_KEY=key\nOPENAI_BASE_URL={hostile}\n", encoding="utf-8"
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = native_launcher.main(
                    ["--check"], repo_root=repo, environ={}
                )
        self.assertEqual(result, 2)
        self.assertEqual(stderr.getvalue(), "error: invalid OpenAI base URL\n")
        self.assertNotIn(hostile, stderr.getvalue())
        self.assertNotIn("hostile-secret", stderr.getvalue())

    def test_environment_precedence_and_native_defaults_override_container_path(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            (repo / ".env").write_text(
                "CTA_API_KEY=file-key\nOPENAI_MODEL=file-model\n"
                "CTA_TELEMETRY_INTERVAL=44\nCTA_LLM_ANOMALIES=true\n",
                encoding="utf-8",
            )
            process = {"CTA_API_KEY": "process-key", "CTA_DB_PATH": "/data/cta.db"}
            config = native_launcher.build_config([], repo, process)
        self.assertEqual(config.environment["CTA_API_KEY"], "process-key")
        self.assertEqual(config.environment["OPENAI_MODEL"], "file-model")
        self.assertEqual(config.environment["CTA_TELEMETRY_INTERVAL"], "44")
        self.assertEqual(config.environment["CTA_LLM_ANOMALIES"], "true")
        self.assertEqual(config.db_path, repo / "data" / "cta.db")
        self.assertEqual(config.environment["CTA_DB_PATH"], str(repo / "data" / "cta.db"))
        self.assertEqual(config.openai_base_url, "http://127.0.0.1:8000/v1")
        self.assertEqual(config.port, 8001)

    def test_native_override_flags_win_over_native_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            process = {
                "CTA_API_KEY": "key",
                "CTA_NATIVE_DB_PATH": "relative.db",
                "CTA_NATIVE_PORT": "8123",
                "OPENAI_NATIVE_BASE_URL": "http://localhost:9000/v1",
            }
            config = native_launcher.build_config(
                ["--db-path", "chosen/db.sqlite", "--port", "8333",
                 "--openai-base-url", "http://127.0.0.1:7000/v1"], repo, process
            )
        self.assertEqual(config.db_path, repo / "chosen" / "db.sqlite")
        self.assertEqual(config.port, 8333)
        self.assertEqual(config.openai_base_url, "http://127.0.0.1:7000/v1")

    def test_openai_base_url_rejects_credentials_query_fragment_and_controls_without_echo(self):
        hostile_values = (
            "https://user:secret@example.test/v1",
            "https://example.test/v1?token=secret",
            "https://example.test/v1#secret",
            "https://example.test/v1\nsecret",
            "file://example.test/v1",
            "https:///v1",
        )
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            for value in hostile_values:
                with self.subTest(value=value.split("secret", 1)[0]):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        result = native_launcher.main(
                            ["--check"], repo_root=repo,
                            environ={"CTA_API_KEY": "key", "OPENAI_NATIVE_BASE_URL": value},
                        )
                    self.assertEqual(result, 2)
                    self.assertEqual(stderr.getvalue(), "error: invalid OpenAI base URL\n")
                    self.assertNotIn(value, stderr.getvalue())

            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = native_launcher.main(
                    ["--check"], repo_root=repo,
                    environ={"CTA_API_KEY": "key", "OPENAI_NATIVE_BASE_URL": "https://example.test/v1"},
                )
        self.assertEqual(result, 0)
        self.assertIn("OpenAI base URL: https://example.test/v1", stdout.getvalue())

    def test_anomaly_boolean_is_normalized_and_invalid_values_are_redacted(self):
        accepted = {"true": "true", "1": "true", "YES": "true", "on": "true",
                    "false": "false", "0": "false", "No": "false", "OFF": "false"}
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            for supplied, canonical in accepted.items():
                with self.subTest(supplied=supplied):
                    config = native_launcher.build_config(
                        [], repo, {"CTA_API_KEY": "key", "CTA_LLM_ANOMALIES": supplied}
                    )
                    self.assertEqual(config.environment["CTA_LLM_ANOMALIES"], canonical)
            for supplied in ("secret-mode", "true\nsecret"):
                with self.subTest(invalid=True):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        result = native_launcher.main(
                            ["--check"], repo_root=repo,
                            environ={"CTA_API_KEY": "key", "CTA_LLM_ANOMALIES": supplied},
                        )
                    self.assertEqual(result, 2)
                    self.assertEqual(stderr.getvalue(), "error: invalid CTA_LLM_ANOMALIES boolean\n")
                    self.assertNotIn(supplied, stderr.getvalue())

    def test_missing_cta_key_is_a_concise_error(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            with self.assertRaises(native_launcher.ConfigError) as raised:
                native_launcher.build_config([], repo, {"CTA_API_KEY": "  "})
        self.assertEqual(str(raised.exception), "CTA_API_KEY is required and must be nonempty")

    def test_invalid_native_port_error_does_not_echo_its_value(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            with self.assertRaises(native_launcher.ConfigError) as raised:
                native_launcher.build_config(
                    [], repo, {"CTA_API_KEY": "key", "CTA_NATIVE_PORT": "secret-port"}
                )
        self.assertEqual(str(raised.exception), "CTA_NATIVE_PORT must be an integer")

    def test_invalid_cli_port_uses_a_generic_non_echoing_argparse_error(self):
        hostile = "secret-port\nsecond-line"
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = native_launcher.main(
                    ["--check", "--port", hostile], repo_root=repo,
                    environ={"CTA_API_KEY": "key"},
                )
        self.assertEqual(result, 2)
        self.assertEqual(stderr.getvalue(), "error: invalid command-line arguments\n")
        self.assertNotIn(hostile, stderr.getvalue())

    def test_native_host_accepts_safe_classes_honors_precedence_and_redacts_hostile_values(self):
        accepted = ("localhost", "rail-api.example.test", "192.0.2.10", "2001:db8::1")
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            for host in accepted:
                with self.subTest(host=host):
                    config = native_launcher.build_config(
                        [], repo, {"CTA_API_KEY": "key", "CTA_NATIVE_HOST": host}
                    )
                    self.assertEqual(config.host, host)
            config = native_launcher.build_config(
                ["--host", "localhost"], repo,
                {"CTA_API_KEY": "key", "CTA_NATIVE_HOST": "192.0.2.10"},
            )
            self.assertEqual(config.host, "localhost")
            hostile_values = ("secret/path", "http://secret", "bad host", "bad\nsecret", "a" * 254)
            for host in hostile_values:
                with self.subTest(hostile=True):
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        result = native_launcher.main(
                            ["--check", "--host", host], repo_root=repo,
                            environ={"CTA_API_KEY": "key"},
                        )
                    self.assertEqual(result, 2)
                    self.assertEqual(stderr.getvalue(), "error: invalid native host\n")
                    self.assertNotIn(host, stderr.getvalue())

    def test_check_is_redacted_and_does_not_exec(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            (repo / ".env").write_text("CTA_API_KEY=super-secret\nCTA_GTFS_API_KEY=gtfs-secret\nOPENAI_API_KEY=model-secret\n", encoding="utf-8")
            stdout = io.StringIO()
            with patch.object(native_launcher.os, "execve") as execve, contextlib.redirect_stdout(stdout):
                result = native_launcher.main(["--check"], repo_root=repo, environ={})
        output = stdout.getvalue()
        self.assertEqual(result, 0)
        self.assertIn("CTA key configured: yes", output)
        self.assertIn("Telemetry source: gtfs-realtime", output)
        self.assertIn("Dashboard: http://127.0.0.1:8001", output)
        self.assertNotIn("super-secret", output)
        self.assertNotIn("model-secret", output)
        self.assertNotIn("gtfs-secret", output)
        execve.assert_not_called()

    def test_exec_uses_current_interpreter_exact_argv_and_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.make_repo(directory)
            with patch.object(native_launcher.sys, "executable", "/current/.venv/bin/python"), \
                 patch.object(native_launcher, "check_openai_models"), \
                 patch.object(native_launcher.os, "execve") as execve:
                native_launcher.main(
                    ["--host", "localhost", "--port", "8111"], repo_root=repo,
                    environ={"CTA_API_KEY": "cta", "PYTHONPATH": "/old"},
                )
            data_directory_created = (repo / "data").is_dir()
            executable, argv, env = execve.call_args.args
        self.assertEqual(executable, "/current/.venv/bin/python")
        self.assertEqual(argv, ["/current/.venv/bin/python", "-m", "cta_pipeline", "live", "--host", "localhost", "--port", "8111"])
        self.assertEqual(env["PYTHONPATH"], str(repo / "src"))
        self.assertTrue(data_directory_created)

    def test_local_models_probe_warns_and_continues_without_sending_key(self):
        config = native_launcher.Config(
            repo=Path("/repo"), env_file=Path("/repo/.env"), env_status="missing",
            host="127.0.0.1", port=8001, db_path=Path("/repo/data/cta.db"),
            openai_base_url="http://localhost:8000/v1",
            environment={"OPENAI_API_KEY": "never-send-this", "CTA_API_KEY": "cta"},
        )
        stderr = io.StringIO()
        opener = Mock()
        opener.open.side_effect = OSError("offline")
        with patch.object(native_launcher.urllib.request, "build_opener", return_value=opener), \
             contextlib.redirect_stderr(stderr):
            native_launcher.check_openai_models(config)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://localhost:8000/v1/models")
        self.assertNotIn("Authorization", request.headers)
        self.assertIn("health probe is unavailable; inference was not tested", stderr.getvalue())
        self.assertNotIn("deterministic fallback", stderr.getvalue())
        self.assertNotIn("never-send-this", stderr.getvalue())

    def test_local_models_probe_refuses_redirects_and_never_sends_authorization(self):
        requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((self.path, self.headers.get("Authorization")))
                if self.path.endswith("/models"):
                    self.send_response(302)
                    self.send_header("Location", "/redirect-target")
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.end_headers()

            def log_message(self, format, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            config = native_launcher.Config(
                repo=Path("/repo"), env_file=Path("/repo/.env"), env_status="missing",
                host="127.0.0.1", port=8001, db_path=Path("/repo/data/cta.db"),
                openai_base_url=f"http://127.0.0.1:{server.server_port}/v1",
                environment={"OPENAI_API_KEY": "never-send-this", "CTA_API_KEY": "cta"},
            )
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                native_launcher.check_openai_models(config)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
        self.assertEqual(requests, [("/v1/models", None)])
        self.assertIn("health probe is unavailable; inference was not tested", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
