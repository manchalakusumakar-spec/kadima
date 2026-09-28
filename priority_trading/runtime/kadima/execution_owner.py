"""One lifetime owner for engine state and the standalone exit fallback."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat

LOCK = Path('/var/lib/kadima-execution-owner/owner.lock')


@contextmanager
def acquire():
    if LOCK.resolve() != LOCK:
        raise ValueError('execution_lock_path_changed')
    parent = LOCK.parent.stat()
    if parent.st_uid != 0 or parent.st_mode & 0o022:
        raise ValueError('execution_lock_directory_unsafe')
    fd = os.open(LOCK, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or info.st_mode & 0o077):
            raise ValueError('execution_lock_unsafe')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)
