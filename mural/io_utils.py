from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path

import fcntl


def ensure_private_dir(path: Path) -> None:
    """Ensure directory exists with private permissions (0700)."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except PermissionError:
        pass


@contextmanager
def locked_file(path: Path):
    """Advisory exclusive lock on a stable sibling .lock file."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            os.chmod(lock_path, 0o600)
        except PermissionError:
            pass
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Atomically write UTF-8 text to path (tmp + fsync + replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")

    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    os.replace(tmp_path, path)
    try:
        os.chmod(path, mode)
    except PermissionError:
        pass


def atomic_save_json(path: Path, obj: object, mode: int = 0o600) -> None:
    text = json.dumps(obj, indent=2, ensure_ascii=False) + "\n"
    atomic_write_text(path, text, mode=mode)


def load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
