"""Backend registry. A project's manifest names its backend; add new ones here."""
from ..util import VaultError
from .arduinocli import ArduinoCli
from .platformio import PlatformIO

BACKENDS = {b.name: b for b in (PlatformIO, ArduinoCli)}


def get_backend(name: str):
    if name not in BACKENDS:
        raise VaultError(f"No backend called '{name}'. Known: {', '.join(BACKENDS)}")
    return BACKENDS[name]()
