#!/bin/sh
set -eu

SCRIPT_PATH=$0
LINK_DEPTH=0
while [ -L "$SCRIPT_PATH" ]; do
    LINK_DEPTH=$((LINK_DEPTH + 1))
    if [ "$LINK_DEPTH" -gt 40 ]; then
        echo "error: unable to resolve launcher path" >&2
        exit 1
    fi
    LINK_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)
    LINK_TARGET=$(readlink "$SCRIPT_PATH") || {
        echo "error: unable to resolve launcher path" >&2
        exit 1
    }
    case "$LINK_TARGET" in
        /*) SCRIPT_PATH=$LINK_TARGET ;;
        *) SCRIPT_PATH=$LINK_DIR/$LINK_TARGET ;;
    esac
done
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P)
cd "$SCRIPT_DIR"

if ! command -v uv >/dev/null 2>&1; then
    echo "error: uv is required; install it from https://docs.astral.sh/uv/" >&2
    exit 1
fi

PYTHON="$SCRIPT_DIR/.venv/bin/python"
VALIDATION_CODE='import pathlib, sys; venv = pathlib.Path(sys.prefix).resolve(); executable = pathlib.Path(sys.executable).absolute(); expected = pathlib.Path(".venv").resolve(); raise SystemExit(0 if sys.version_info >= (3, 13) and sys.prefix != sys.base_prefix and venv == expected and (executable == expected or expected in executable.parents) else 1)'

valid_venv() {
    [ -x "$PYTHON" ] && CTA_NATIVE_VENV="$SCRIPT_DIR/.venv" "$PYTHON" -c "$VALIDATION_CODE" >/dev/null 2>&1
}

if [ ! -e "$SCRIPT_DIR/.venv" ]; then
    uv venv --python 3.13 .venv
elif ! valid_venv; then
    uv venv --clear --python 3.13 .venv
fi

if ! valid_venv; then
    echo "error: uv did not create a valid Python 3.13 virtual environment" >&2
    exit 1
fi

PYTHONPATH="$SCRIPT_DIR/src"; export PYTHONPATH
exec "$PYTHON" -m cta_pipeline.native_launcher "$@"
