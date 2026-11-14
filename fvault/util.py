import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

SKIP_DIRS = {".pio", ".build", ".git", ".vscode", "__pycache__", ".venv"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def iter_tree(root: Path):
    """Files under root, sorted, skipping build output and VCS dirs."""
    root = Path(root)
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            out.append(p)
    return sorted(out, key=lambda p: p.relative_to(root).as_posix())


def tree_hash(root: Path) -> str:
    """Hash of every file's path and content, independent of timestamps."""
    h = hashlib.sha256()
    for p in iter_tree(root):
        rel = p.relative_to(root).as_posix()
        h.update(rel.encode() + b"\0" + sha256_file(p).encode() + b"\n")
    return h.hexdigest()


def read_json(path: Path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def append_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(text)


def slugify(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "project"


class VaultError(Exception):
    """A refusal or failure with a plain-language message for the human."""
