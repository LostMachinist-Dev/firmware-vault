"""The interface every build backend implements.

A backend knows how to build a project's working source, report the exact
versions of everything that went into the build, and check that nothing is
left floating ("latest"). Flashing works on stored artifacts and is done by
the flasher named in the board profile (see fvault.flashers), so a backend
never has to rebuild to flash.
"""
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Artifact:
    path: Path              # file on disk after the build
    name: str               # name it is stored under in the version folder
    role: str               # "app", "bootloader", "partitions", "boot_app0", "merged", "elf"
    offset: int | None = None   # flash address, when the flasher needs one


@dataclass
class BuildResult:
    ok: bool
    log: str
    artifacts: list[Artifact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    build_dir: Path | None = None


class Backend:
    name = "base"

    def version(self) -> str:
        raise NotImplementedError

    def new_project(self, work: Path, profile: dict, name: str) -> None:
        raise NotImplementedError

    def check_pinned(self, work: Path, project: dict) -> list[str]:
        """Plain-language problems; empty list means everything is pinned."""
        raise NotImplementedError

    def pin(self, work: Path, project: dict) -> list[str]:
        """Rewrite floating versions to the exact ones installed. Returns changes."""
        raise NotImplementedError

    def build(self, work: Path, project: dict, profile: dict) -> BuildResult:
        raise NotImplementedError

    def dependencies(self, work: Path, project: dict, profile: dict) -> dict:
        """Exact versions: backend, platform, framework, toolchain, compiler, libraries, flags."""
        raise NotImplementedError
