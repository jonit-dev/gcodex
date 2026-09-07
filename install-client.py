"""Validate and install an extracted OAuth client without partial files."""
import argparse
import json
import os
from pathlib import Path
import stat
import sys
import tempfile


def validate(data):
    if not isinstance(data, dict) or any(
        not isinstance(data.get(key), str) or not data[key].strip()
        for key in ("client_id", "client_secret")
    ):
        raise ValueError("expected nonempty client_id and client_secret")
    return data


def check(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise ValueError("credentials must be a regular file owned by this user")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise ValueError("credentials must have private permissions (chmod 600)")
    validate(json.loads(path.read_text()))


def install(path, data):
    validate(data)
    fd, name = tempfile.mkstemp(prefix=".gcodex-client-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Atomic publication that refuses to overwrite a file or symlink.
        os.link(name, path)
    finally:
        os.unlink(name)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        if args.check:
            check(args.path)
        else:
            install(args.path, json.load(sys.stdin))
    except (OSError, ValueError):
        sys.exit("gcodex: invalid, unsafe, or unwritable credentials; no credentials were replaced")
