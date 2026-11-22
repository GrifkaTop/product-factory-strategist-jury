"""Filesystem paths for local Windows checkouts with long checkpoint names."""

import os
from pathlib import Path


def io_path(path: Path, *, recursive: bool = False) -> Path:
    """Use Win32 extended-length syntax for long paths at the I/O boundary."""
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith(("\\\\?\\", "\\\\.\\")) or (not recursive and len(absolute) < 240):
        return path
    if absolute.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + absolute[2:])
    return Path("\\\\?\\" + absolute)


def resolved_path(path: Path) -> Path:
    """Return a comparable resolved path even if part of it does not exist."""
    resolved = io_path(path).resolve()
    if os.name == "nt":
        value = str(resolved)
        if value.startswith("\\\\?\\UNC\\"):
            return Path("\\\\" + value[8:])
        if value.startswith("\\\\?\\"):
            return Path(value[4:])
    return resolved
