import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path


PROJECT_SCRIPT = Path(__file__).resolve().parents[1] / "run-native.sh"


class RunNativeShellTests(unittest.TestCase):
    def repo(self, directory):
        repo = Path(directory) / "repo with spaces"
        repo.mkdir()
        shutil.copy2(PROJECT_SCRIPT, repo / "run-native.sh")
        (repo / "run-native.sh").chmod(0o755)
        return repo

    def fake_uv(self, directory, version="Python 3.13.2"):
        bindir = Path(directory) / "bin"
        bindir.mkdir(exist_ok=True)
        uv = bindir / "uv"
        uv.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$PWD|$*\" >> \"$UV_LOG\"\n"
            "venv=$PWD/.venv\nrm -rf \"$venv\"\nmkdir -p \"$venv/bin\"\n"
            "printf '%s\\n' '#!/bin/sh' 'if [ \"$1\" = \"-c\" ]; then' "
            "'case \"$2\" in *\"sys.version_info >= (3, 13)\"*) ;; *) exit 1 ;; esac' "
            "'case \"$2\" in *\"sys.prefix != sys.base_prefix\"*) ;; *) exit 1 ;; esac' "
            "'case \"$2\" in *\"sys.executable\"*) ;; *) exit 1 ;; esac' "
            "'[ \"$CTA_NATIVE_VENV\" = \"$PWD/.venv\" ] && [ -f \"$PWD/.venv/valid\" ]' "
            "'exit $?' 'fi' 'printf \"%s\\n\" \"$PWD|$*\" >> \"$PY_LOG\"' > \"$venv/bin/python\"\n"
            "chmod +x \"$venv/bin/python\"\n"
            "[ \"${FAKE_UV_INVALID:-0}\" = 1 ] || : > \"$venv/valid\"\n",
            encoding="utf-8",
        )
        uv.chmod(0o755)
        return bindir, {"FAKE_PY_VERSION": version}

    def run_script(self, repo, cwd, path, extra_env=None, *args):
        env = {"PATH": str(path) + os.pathsep + os.defpath,
               "UV_LOG": str(repo / "uv.log"), "PY_LOG": str(repo / "py.log")}
        env.update(extra_env or {})
        return subprocess.run([str(repo / "run-native.sh"), *args], cwd=cwd, env=env, text=True, capture_output=True)

    def test_missing_uv_has_concise_install_hint(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repo(directory)
            empty = Path(directory) / "empty-bin"
            empty.mkdir()
            result = self.run_script(repo, directory, empty, None, "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("uv is required", result.stderr)
        self.assertIn("https://docs.astral.sh/uv/", result.stderr)

    def test_creates_venv_from_repo_root_and_forwards_args_from_any_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repo(directory)
            bindir, extra = self.fake_uv(directory)
            result = self.run_script(repo, directory, bindir, extra, "--check", "--port", "8123")
            uv_log = (repo / "uv.log").read_text()
            self.assertEqual(result.returncode, 0, result.stderr)
            py_log = (repo / "py.log").read_text()
        self.assertEqual(uv_log.strip(), f"{repo}|venv --python 3.13 .venv")
        self.assertEqual(py_log.strip(), f"{repo}|-m cta_pipeline.native_launcher --check --port 8123")

    def test_resolves_relative_and_absolute_symlink_invocation_with_path_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repo(directory)
            bindir, extra = self.fake_uv(directory)
            links = Path(directory) / "linked launchers with spaces"
            links.mkdir()
            relative = links / "relative launcher"
            relative.symlink_to(os.path.relpath(repo / "run-native.sh", links))
            absolute = links / "absolute launcher"
            absolute.symlink_to(repo / "run-native.sh")
            env = {"PATH": str(bindir) + os.pathsep + os.defpath,
                   "UV_LOG": str(repo / "uv.log"), "PY_LOG": str(repo / "py.log"), **extra}
            for launcher in (relative, absolute):
                with self.subTest(kind=launcher.name.split()[0]):
                    shutil.rmtree(repo / ".venv", ignore_errors=True)
                    result = subprocess.run(
                        [str(launcher), "--check"], cwd=directory, env=env,
                        text=True, capture_output=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
            uv_lines = (repo / "uv.log").read_text().splitlines()
            self.assertEqual(uv_lines, [f"{repo}|venv --python 3.13 .venv"] * 2)

    def test_reuses_valid_venv_without_uv_command(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repo(directory)
            bindir, extra = self.fake_uv(directory)
            setup_env = {"UV_LOG": str(repo / "uv.log"), "PY_LOG": str(repo / "py.log"), **extra}
            subprocess.run([str(bindir / "uv"), "venv", ".venv"], cwd=repo,
                           env={**os.environ, **setup_env}, check=True)
            (repo / "uv.log").unlink()
            result = self.run_script(repo, directory, bindir, extra, "--check")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((repo / "uv.log").exists())

    def test_recreates_invalid_venv_with_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repo(directory)
            bindir, extra = self.fake_uv(directory)
            (repo / ".venv" / "bin").mkdir(parents=True)
            python = repo / ".venv" / "bin" / "python"
            python.write_text("#!/bin/sh\nexit 1\n")
            python.chmod(0o755)
            result = self.run_script(repo, directory, bindir, extra, "--check")
            uv_log = (repo / "uv.log").read_text()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(uv_log.strip(), f"{repo}|venv --clear --python 3.13 .venv")

    def test_recreates_system_python_symlink_and_version_spoof_with_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repo(directory)
            bindir, extra = self.fake_uv(directory)
            python = repo / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            variants = ("system", "spoof")
            for variant in variants:
                with self.subTest(variant=variant):
                    if python.exists() or python.is_symlink():
                        python.unlink()
                    if variant == "system":
                        python.symlink_to(shutil.which("python3"))
                    else:
                        python.write_text(
                            "#!/bin/sh\n"
                            "[ \"$1\" = --version ] && echo 'Python 3.13.99' && exit 0\n"
                            "exit 1\n", encoding="utf-8",
                        )
                        python.chmod(0o755)
                    result = self.run_script(repo, directory, bindir, extra, "--check")
                    self.assertEqual(result.returncode, 0, result.stderr)
            uv_lines = (repo / "uv.log").read_text().splitlines()
        self.assertEqual(uv_lines, [f"{repo}|venv --clear --python 3.13 .venv"] * 2)

    def test_fails_concisely_when_uv_creation_does_not_produce_a_valid_venv(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = self.repo(directory)
            bindir, extra = self.fake_uv(directory)
            extra["FAKE_UV_INVALID"] = "1"
            result = self.run_script(repo, directory, bindir, extra, "--check")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            result.stderr,
            "error: uv did not create a valid Python 3.13 virtual environment\n",
        )


if __name__ == "__main__":
    unittest.main()
