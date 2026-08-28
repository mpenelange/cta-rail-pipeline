import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cta_pipeline import container_entrypoint


class ContainerEntrypointTests(unittest.TestCase):
    def test_prepare_data_directory_creates_and_chowns_it(self):
        with tempfile.TemporaryDirectory() as parent:
            data = Path(parent) / "data"
            with patch.object(os, "chown") as chown:
                container_entrypoint.prepare_data_directory(data, 10001, 10001)
            self.assertTrue(data.is_dir())
            chown.assert_called_once_with(data, 10001, 10001)

    def test_drop_privileges_clears_groups_before_gid_and_uid(self):
        calls = []
        with patch.object(os, "setgroups", side_effect=lambda groups: calls.append(("groups", groups))), \
             patch.object(os, "setgid", side_effect=lambda gid: calls.append(("gid", gid))), \
             patch.object(os, "setuid", side_effect=lambda uid: calls.append(("uid", uid))):
            container_entrypoint.drop_privileges(10001, 10001)
        self.assertEqual(calls, [("groups", []), ("gid", 10001), ("uid", 10001)])

    def test_main_prepares_drops_and_execs_command_unchanged(self):
        command = ["python3", "-m", "cta_pipeline", "init-db"]
        with patch.object(container_entrypoint, "prepare_data_directory") as prepare, \
             patch.object(container_entrypoint, "drop_privileges") as drop, \
             patch.object(os, "execvp") as execvp:
            container_entrypoint.main(command)
        prepare.assert_called_once_with(Path("/data"), 10001, 10001)
        drop.assert_called_once_with(10001, 10001)
        execvp.assert_called_once_with(command[0], command)


if __name__ == "__main__":
    unittest.main()
