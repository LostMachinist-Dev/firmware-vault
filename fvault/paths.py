"""Where things live. Override the vault root with FIRMWARE_VAULT_ROOT."""
import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
PROFILES_DIR = APP_DIR / "profiles"
TEMPLATES_DIR = APP_DIR / "templates"
WEB_DIR = APP_DIR / "web"
VENV_BIN = APP_DIR / ".venv" / "bin"


def vault_root() -> Path:
    return Path(os.environ.get("FIRMWARE_VAULT_ROOT", Path.home() / "vault")).expanduser()


def pio_home() -> Path:
    return Path(os.environ.get("PLATFORMIO_CORE_DIR", Path.home() / ".platformio")).expanduser()


def tool(name: str) -> str:
    """A tool from the project venv if present, else from PATH."""
    p = VENV_BIN / name
    return str(p) if p.exists() else name
