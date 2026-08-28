"""Container bootstrap: prepare persistent data, then exec as the app user."""

import os
import sys
from pathlib import Path


APP_UID = 10001
APP_GID = 10001
DATA_DIRECTORY = Path("/data")


def prepare_data_directory(path, uid, gid):
    path.mkdir(parents=True, exist_ok=True)
    os.chown(path, uid, gid)


def drop_privileges(uid, gid):
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def main(command=None):
    command = list(sys.argv[1:] if command is None else command)
    if not command:
        raise SystemExit("container entrypoint requires a command")
    prepare_data_directory(DATA_DIRECTORY, APP_UID, APP_GID)
    drop_privileges(APP_UID, APP_GID)
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
