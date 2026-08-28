#!/bin/sh
set -eu
[ "$1" = "venv" ]
rm -rf .venv
mkdir -p .venv/bin
printf '%s\n' '#!/bin/sh' \
    'if [ "$1" = "-c" ]; then' \
    'case "$2" in *"sys.version_info >= (3, 13)"*) ;; *) exit 1 ;; esac' \
    'case "$2" in *"sys.prefix != sys.base_prefix"*) ;; *) exit 1 ;; esac' \
    'case "$2" in *"sys.executable"*) ;; *) exit 1 ;; esac' \
    '[ "$CTA_NATIVE_VENV" = "$PWD/.venv" ]' \
    'exit $?' \
    'fi' \
    'exec "$NATIVE_SMOKE_PYTHON" "$@"' > .venv/bin/python
chmod +x .venv/bin/python
