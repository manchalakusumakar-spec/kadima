"""Created policy/permit directories must be readable despite a private umask."""
import os
from pathlib import Path
import stat
import tempfile
import unittest
import test_session_auto as suite


class ScheduleDirectoryTests(unittest.TestCase):
    def test_private_umask_does_not_hide_new_policy_directories(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); root.chmod(0o755)
            target=root/'etc'/'kadima'/'session-auto'
            before=os.umask(0o077)
            try:suite.install.ensure_root_directory(target,owner=os.geteuid())
            finally:os.umask(before)
            for folder in (root/'etc',root/'etc'/'kadima',target):
                self.assertEqual(stat.S_IMODE(folder.stat().st_mode),0o755)
    def test_existing_private_parent_is_not_silently_exposed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); private=root/'private';private.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError,'private_parent_directory_requires_review'):
                suite.install.ensure_root_directory(private/'schedule',owner=os.geteuid())
            self.assertEqual(stat.S_IMODE(private.stat().st_mode),0o700)
    def test_private_state_directory_remains_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);root.chmod(0o755);target=root/'state'
            suite.install.ensure_root_directory(target,0o700,owner=os.geteuid())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o700)

if __name__=='__main__':unittest.main()
