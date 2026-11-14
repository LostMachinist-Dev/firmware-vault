"""Board profiles: JSON files in profiles/. Adding a board family = adding a file."""
import json

from .paths import PROFILES_DIR
from .util import VaultError


def list_profiles() -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted(PROFILES_DIR.glob("*.json"))]


def get_profile(pid: str) -> dict:
    p = PROFILES_DIR / f"{pid}.json"
    if not p.exists():
        known = ", ".join(x["id"] for x in list_profiles())
        raise VaultError(f"No board profile '{pid}'. Known: {known}")
    return json.loads(p.read_text())


def _dig(d: dict, dotted: str):
    if isinstance(d, dict) and dotted in d:     # flat build properties (arduino-cli)
        return d[dotted]
    for part in dotted.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def _one(src: str, ini: dict, board: dict):
    kind, _, key = src.partition(":")
    if kind == "ini":
        return ini.get(key)
    if kind == "board":
        return _dig(board, key)
    if kind == "const":
        return key
    return None


def resolve_fields(profile: dict, ini: dict, board: dict) -> dict:
    """Extra manifest fields the profile declares, e.g. fuses or partition scheme."""
    out = {}
    for f in profile.get("fields", []):
        src = f["from"]
        if isinstance(src, dict):
            val = {k: _one(v, ini, board) for k, v in src.items()}
            val = val if any(v is not None for v in val.values()) else None
        else:
            val = None
            for s in ([src] if isinstance(src, str) else src):
                val = _one(s, ini, board)
                if val is not None:
                    break
        out[f["key"]] = val if val is not None else f.get("default")
    return out
