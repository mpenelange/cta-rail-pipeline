"""Safe native-host configuration and process launcher."""

import argparse
import ipaddress
import os
import re
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path


NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


class ConfigError(ValueError):
    """A concise configuration error that never includes secret values."""


class SafeArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        del message
        raise ConfigError("invalid command-line arguments")


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass
class Config:
    repo: Path
    env_file: Path
    env_status: str
    host: str
    port: int
    db_path: Path
    openai_base_url: str
    environment: dict


def parse_dotenv(path):
    """Parse a deliberately small dotenv format as inert text."""
    result = {}
    try:
        content = Path(path).read_text(encoding="utf-8")
    except UnicodeError:
        raise ConfigError("env file must be UTF-8") from None
    except OSError:
        raise ConfigError("unable to read env file") from None
    if "\0" in content:
        raise ConfigError("env file contains a NUL byte")
    for number, original in enumerate(content.splitlines(), 1):
        line = original.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export") and (len(line) == 6 or line[6].isspace()):
            line = line[6:].lstrip()
        if "=" not in line:
            raise ConfigError(f"malformed env line {number}")
        name, value = line.split("=", 1)
        name = name.strip()
        if not NAME.fullmatch(name):
            raise ConfigError(f"invalid env name on line {number}")
        value = value.strip()
        if value.startswith(("'", '"')):
            quote = value[0]
            if len(value) < 2 or value[-1] != quote:
                raise ConfigError(f"unterminated quoted value on line {number}")
            value = value[1:-1]
            if quote == '"':
                value = (value.replace("\\n", "\n").replace("\\r", "\r")
                         .replace("\\t", "\t").replace('\\"', '"')
                         .replace("\\\\", "\\"))
        result[name] = value
    return result


def _parser():
    parser = SafeArgumentParser(prog="native_launcher")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--env-file")
    parser.add_argument("--host")
    parser.add_argument("--port", type=_cli_port)
    parser.add_argument("--db-path")
    parser.add_argument("--openai-base-url")
    return parser


def _cli_port(value):
    if re.fullmatch(r"[0-9]+", value) is None:
        raise argparse.ArgumentTypeError("invalid port")
    try:
        port = int(value)
    except (TypeError, ValueError):
        raise argparse.ArgumentTypeError("invalid port") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("invalid port")
    return port


def _path(value, repo):
    path = Path(value).expanduser()
    return path if path.is_absolute() else repo / path


def _openai_url(value):
    if any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value):
        raise ConfigError("invalid OpenAI base URL")
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ConfigError("invalid OpenAI base URL") from None
    if (parsed.scheme not in {"http", "https"} or not hostname or
            parsed.username is not None or parsed.password is not None or
            parsed.query or parsed.fragment):
        raise ConfigError("invalid OpenAI base URL")
    try:
        _native_host(hostname)
    except ConfigError:
        raise ConfigError("invalid OpenAI base URL") from None
    del port
    return value


def _dotenv_boolean(value):
    normalized = value.lower()
    if normalized in {"true", "1", "yes", "on"}:
        return "true"
    if normalized in {"false", "0", "no", "off"}:
        return "false"
    raise ConfigError("invalid CTA_LLM_ANOMALIES boolean")


def _native_host(value):
    if not value or len(value) > 253:
        raise ConfigError("invalid native host")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    labels = value[:-1].split(".") if value.endswith(".") else value.split(".")
    if not labels or any(
        not label or len(label) > 63 or
        re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label) is None
        for label in labels
    ):
        raise ConfigError("invalid native host")
    return value


def build_config(argv, repo, process_environment):
    repo = Path(repo).resolve()
    args = _parser().parse_args(argv)
    process = dict(process_environment)
    env_file = _path(args.env_file, repo) if args.env_file else repo / ".env"
    if env_file.exists():
        file_values = parse_dotenv(env_file)
        status = "loaded"
    else:
        if args.env_file:
            raise ConfigError("unable to read env file")
        file_values = {}
        status = "not found"
    environment = {**file_values, **process}
    if not environment.get("CTA_API_KEY", "").strip():
        raise ConfigError("CTA_API_KEY is required and must be nonempty")
    if args.port is not None:
        port = args.port
    else:
        port_value = environment.get("CTA_NATIVE_PORT", "8001")
        if re.fullmatch(r"[0-9]+", port_value) is None:
            raise ConfigError("CTA_NATIVE_PORT must be an integer")
        try:
            port = int(port_value)
        except ValueError:
            raise ConfigError("CTA_NATIVE_PORT must be an integer") from None
    if not 1 <= port <= 65535:
        raise ConfigError("port must be between 1 and 65535")
    host = _native_host(args.host or environment.get("CTA_NATIVE_HOST") or "127.0.0.1")
    db_value = args.db_path or environment.get("CTA_NATIVE_DB_PATH") or "data/cta.db"
    db_path = _path(db_value, repo).resolve()
    base_url = _openai_url(args.openai_base_url or environment.get("OPENAI_NATIVE_BASE_URL") or "http://127.0.0.1:8000/v1")
    environment["CTA_LLM_ANOMALIES"] = _dotenv_boolean(environment.get("CTA_LLM_ANOMALIES", "false"))
    environment["CTA_DB_PATH"] = str(db_path)
    environment["OPENAI_BASE_URL"] = base_url
    return Config(repo, env_file, status, host, port, db_path, base_url, environment)


def check_openai_models(config):
    if not config.environment.get("OPENAI_API_KEY"):
        return
    parsed = urllib.parse.urlparse(config.openai_base_url)
    if parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return
    url = config.openai_base_url.rstrip("/") + "/models"
    request = urllib.request.Request(url, method="GET", headers={"User-Agent": "cta-native-check/1"})
    try:
        opener = urllib.request.build_opener(NoRedirectHandler())
        with opener.open(request, timeout=1.5) as response:
            response.read(1)
    except Exception:
        print("warning: local OpenAI-compatible /models check failed; continuing with deterministic fallback", file=sys.stderr)


def _summary(config):
    anomaly = config.environment.get("CTA_LLM_ANOMALIES", "false")
    print(f"Python: {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")
    print(f"Repository: {config.repo}")
    print(f"Env file: {config.env_file} ({config.env_status})")
    print(f"Database: {config.db_path}")
    display_host = f"[{config.host}]" if ":" in config.host else config.host
    print(f"Dashboard: http://{display_host}:{config.port}")
    print(f"OpenAI base URL: {config.openai_base_url}")
    print("CTA key configured: yes")
    print(f"Anomaly LLM: {anomaly}")


def main(argv=None, *, repo_root=None, environ=None):
    argv = sys.argv[1:] if argv is None else argv
    repo = Path(repo_root) if repo_root is not None else Path(__file__).resolve().parents[2]
    try:
        config = build_config(argv, repo, os.environ if environ is None else environ)
    except (ConfigError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if "--check" in argv:
        _summary(config)
        return 0
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    check_openai_models(config)
    environment = dict(config.environment)
    environment["PYTHONPATH"] = str(config.repo / "src")
    command = [sys.executable, "-m", "cta_pipeline", "live", "--host", config.host, "--port", str(config.port)]
    os.execve(sys.executable, command, environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
