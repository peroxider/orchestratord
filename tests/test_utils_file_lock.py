"""flock helper tests: exclusivity and release.

These tests exercise the POSIX ``fcntl.flock`` path and are skipped when
``fcntl`` is unavailable (e.g. plain Windows Python); the acceptance
environment is Ubuntu WSL where ``flock`` is available.
"""

from __future__ import annotations

import os

import pytest

from orchestratord.utils.file_lock import (
    HAS_FLOCK,
    exclusive_file_lock,
    flock_exclusive,
    flock_unlock,
)

pytestmark = pytest.mark.skipif(
    not HAS_FLOCK,
    reason="fcntl.flock is unavailable on this platform",
)


def test_flock_exclusive_blocks_second_lock(tmp_path) -> None:
    lock_path = tmp_path / "app.lock"
    fd1 = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fd2 = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        flock_exclusive(fd1)
        # A second non-blocking exclusive lock on the same file must fail.
        with pytest.raises(OSError):
            flock_exclusive(fd2, non_blocking=True)
        # Re-locking the already-held descriptor stays a no-op (flock is
        # per-fd, and re-acquiring the same fd's lock succeeds).
        flock_exclusive(fd1, non_blocking=True)
    finally:
        flock_unlock(fd1)
        flock_unlock(fd2)
        os.close(fd1)
        os.close(fd2)


def test_flock_unlock_releases_the_lock(tmp_path) -> None:
    lock_path = tmp_path / "app.lock"
    fd1 = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fd2 = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        flock_exclusive(fd1)
        with pytest.raises(OSError):
            flock_exclusive(fd2, non_blocking=True)
        flock_unlock(fd1)
        # After the release, the other descriptor can take the lock.
        flock_exclusive(fd2, non_blocking=True)
    finally:
        flock_unlock(fd1)
        flock_unlock(fd2)
        os.close(fd1)
        os.close(fd2)


def test_exclusive_file_lock_context_manager(tmp_path) -> None:
    lock_path = tmp_path / "nested.lock"
    with exclusive_file_lock(lock_path) as fd:
        assert isinstance(fd, int)
        other = os.open(str(lock_path), os.O_RDWR)
        try:
            with pytest.raises(OSError):
                flock_exclusive(other, non_blocking=True)
        finally:
            os.close(other)
    # After the context manager exits, the lock is released.
    fd2 = os.open(str(lock_path), os.O_RDWR)
    try:
        flock_exclusive(fd2, non_blocking=True)
    finally:
        flock_unlock(fd2)
        os.close(fd2)
