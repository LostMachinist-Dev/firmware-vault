# Firmware Vault: Project Spec

## 1. Purpose

Firmware Vault is a local app that keeps every known build of a microcontroller project: the compiled binary, the full source snapshot, the exact toolchain and library versions, a plain-language story of how it was made, and a running log of what was wrong with it. You flash from the vault, not from an IDE. The original problem it solves: a library update bricked a working project and there was no way to get the old build back.

## 2. Principles

- The hex is the thing you flash. Source, manifest and notes exist so it can be understood and rebuilt.
- Exact versions are always pinned. Never "latest".
- Nothing is hidden. Full source is stored with every version and any two versions can be diffed.
- Only the human can mark something as working on real hardware. Claude Code may close issues it can verify itself, and must record the evidence.
- Plain-language visibility over approval gates. Claude Code can pull libraries and make choices on its own, but every version carries a summary of what it did and why.
- Treat each project as a learning project: the build story doubles as a study guide.
- The tool is board-agnostic. No board family is hardcoded. Boards are described by profiles that can be added without changing the app.

## 3. Architecture

Three layers.

1. Project store. A folder per project on disk, tracked in git, so revision history comes free.
2. Engine. A thin adapter layer that calls the build tool and the flasher. Each project's manifest names its backend, and each backend is a plugin behind the same interface (build, flash, list dependencies, report versions). PlatformIO is the first backend, because it pins versions in a config file and covers a wide range of boards. Others, such as arduino-cli, can be added as further plugins without touching the rest of the app.
3. Front end. A desktop app or local web page for browsing versions, flashing, and starting debug sessions. It is not a code editor and never hides the code.

An MCP server sits in front of the engine and store, so Claude Code, the voice notepad and the UI all work against the same vault.

## 4. Project folder layout

Each project folder holds a project file (name, board, backend, hardware notes), a versions folder with one subfolder per version, and a top-level summary showing the latest known-good version.

Each version subfolder holds:

- the compiled hex or binary
- the full source snapshot
- manifest.json
- build-story.md, owned by Claude Code
- debug-log.md, append-only
- issues, one record per issue

## 5. Version record

The manifest captures everything needed to reproduce the build and the flash.

Build side:

- board profile and variant
- backend and its version
- compiler version
- board core or platform version
- every library with its exact pinned version
- build flags and optimization level
- clock speed
- any extra fields the board profile adds, such as a partition scheme or flash mode
- hash of the source tree
- hash of the final binary, for later verification

Flash side:

- upload method or programmer
- bootloader version
- any extra flash-side fields the board profile adds, such as fuse settings

Human side:

- wiring or pinout notes
- hardware revision
- date and a short note on what changed from the previous version

## 6. Version status

Each version has one status plus a one-line reason:

- draft
- testing
- known good
- known good with caveats
- do not deploy

Only the human sets known good, known good with caveats, or do not deploy. Claude Code may move a version to testing. Flashing a do-not-deploy version shows a warning. The project's front page always shows the latest known-good version at the top, so recovery is one click.

Status is separate from the issue list. A version can have open issues and still be the best one available.

## 7. Issues and the debug workflow

Every flash marks that version as currently on the board. A single button in the app starts a debug session tied to that version. While it is listening, each spoken note becomes its own timestamped issue, for example "the graph scales incorrectly" or "the data is mirrored".

Issue lifecycle: open, fix attempted (linked to the version that tries the fix), then verified or reopened.

Who can close an issue:

- Anything that needs the physical hardware is closed only by the human, by click or by saying it is fixed.
- Anything Claude Code can check itself, such as build warnings, failing tests or readable serial output, Claude Code may close. It must store the evidence with the closure, such as the test output or the serial log line.

Each issue has a verified-by field showing human or Claude Code.

When a new version is flashed, the app shows the still-open issues from the previous version.

## 8. How the LLM fits in

Claude Code works through MCP and runs the whole loop: read open issues, write or port code, pull libraries, build, capture the manifest, and write the version summary. It may pick common, well-maintained libraries on its own and should raise a choice to the human when there is a real tradeoff or when it wants to port logic instead of using a library.

Every version carries a short plain-language summary: which libraries were added and why, what was ported or written from scratch, and what was deliberately not used. Library changes show as a diff between versions. The build story is written so it can be used to recreate the project with an LLM and also works as a study guide.

## 9. MCP tools

- list projects
- get project
- list versions
- get version, including manifest and summary
- create version from the current source
- build
- flash a version
- set version status, human only
- list open issues
- add debug note or issue
- mark fix attempted
- close issue, with evidence and verified-by
- diff two versions, source and libraries
- append to build story

## 10. Voice debug notes

The existing voice notepad, or a close sibling of it, writes timestamped entries into the current version's debug log and creates issues. One click starts and stops listening. Notes always attach to the version currently on the board.

## 11. Boards and toolchains

A board is a profile, not a hardcoded case. A profile holds a name, the toolchain backend to use, the upload method, and the list of extra manifest fields that apply to that board family. Adding a new family means adding a profile.

The manifest has a core set of fields that apply to every project: toolchain and library versions, build flags, hashes, status and notes. Anything board-specific hangs off the profile. For example, an AVR profile adds fuse settings, and an ESP32 profile adds a partition scheme and flash mode.

Starting profiles: the Arduino boards in use, ESP32 boards, and STM32 boards as the family to grow into.

## 12. PCB tool link

The vault links to the existing PCB layout tool, which currently works as a Claude skill and produces .sch and .brd files plus a web interface for review. Each version's manifest stores a reference to the PCB project and its board revision, so every firmware build records which physical board it ran on.

The pin map comes from the schematic. Claude Code reads the real pin assignments from the PCB project when writing or porting code, so pinout notes are no longer typed by hand. The link stays loose: a project path and revision in the manifest, with the PCB tool free to become an MCP server later.

## 13. Interface mockup

&#91;embedded content: main screen mockup · 3 panels\]

The main screen has three panels: projects on the left, the version timeline in the middle, and the selected version's detail on the right. The latest known-good version is pinned above the timeline, and Flash and Start debug notes sit at the top of the detail panel.

## 14. Open questions

## 

## 

- Desktop app or local web page for the front end
- Which backends to build first, beyond PlatformIO
- Which STM32 board to use as the first test profile
- Where the wiring and pinout notes live, in the manifest or as a separate file
