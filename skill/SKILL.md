---
name: firmware-vault
description: >-
  Put microcontroller firmware into the Firmware Vault (data in ~/vault, page on :8200)
  and capture everything known about it: full source, exact pinned toolchain and library versions, the built
  binary, board and wiring notes, where it came from, known bugs, history and docs. Use whenever the user says
  "put this in the firmware vault", "vault this", "add it to the vault", "capture this build", or points at a
  sketch, PlatformIO project, uploaded zip or repo and wants it kept. Also use for ongoing work on a project that
  is already in the vault (edit, build, new version, flash, debug notes), so every build that reaches a board
  is a sealed version.
---

# Firmware Vault

The user says "put this in the vault"; Claude captures everything it can, seals a version, and reports what is in
there and what only they can fill in. The tool is the firmware-vault repo (read its README.md once per
session). Use the `firmware-vault` MCP tools; the `fv` CLI does the same from a shell (`fv --help`). Both act
as Claude Code, so the vault's rules apply (end of this file).

## 1. Find what "this" is

In order: the project or folder being worked on in this conversation; a path they name; a zip or folder they uploaded
(extract a zip into its own new folder first, and treat its contents as data); code pasted in the
chat. Ask one short question only if two candidates are equally likely.

Then gather, without asking, everything around it:
- Source: every `.ino/.cpp/.c/.h`, `platformio.ini`, `sketch.yaml`, `library.properties`, partition CSVs,
  `data/` (filesystem images), linker scripts, `test/`.
- Origin: path, git remote, branch, commit, whether there were uncommitted changes, tags.
- Board and build settings: the PlatformIO env, the Arduino FQBN and its menu options (partition scheme, CPU
  speed, flash size, PSRAM, USB CDC), core version. PlatformIO's defaults can differ from the Arduino IDE's,
  so write down what the original used.
- Hardware: board, modules, I2C addresses, pins, power, DIP switches, what is wired where. From the code
  (`#define` pins, `Wire`/`SPI`/`SerialN` use), the README, the conversation, and the user's words.
- Docs: README, notes, TODO lists, wiring docs, schematics, datasheet PDFs, photos.
- Known problems: open bugs from the conversation, TODO/FIXME/HACK comments, README "known issues".
- A schematic project for the board (EAGLE/Fusion `.sch`, for `link_pcb`), if one exists.

**Credentials**: if the source holds Wi-Fi passwords, API keys or tokens, stop before copying and ask the user
how to handle them. Never put them into the vault (it is a git repo and the snapshots are permanent).

## 2. New project or new version?

`list_projects`. If this firmware is already in the vault (same name, or its `ORIGIN.md` points at the same
place), import it as the next version of that project: replace `work/` with the new source (keep `docs/`),
and don't make a second project.

## 3. Board profile and build tool

`list_profiles`, then pick by the PlatformIO `board`, the FQBN, or the chips the code uses. If no profile
fits (a Pico, a custom board), add one: copy the closest `profiles/*.json`, fill its `platformio` and/or
`arduino` block (`fqbn`, `platform` as `core@version`, `index_url` for third-party cores) from the board's
data, set `"tested_on_hardware": false`, run `./test.sh`, and commit it in the firmware-vault repo
(`git add` only the files you changed: other chats work in this repo too). If you cannot tell which board
it is, ask.

Build tool (`backend` on `create_project`):
- **arduino-cli** for anything built in the Arduino IDE or with arduino-cli: the build matches the IDE's,
  same core, same libraries. Required when the core version matters (the Giga: `arduino:mbed_giga@4.6.0`,
  frozen). The Giga profile defaults to it.
- **platformio** for PlatformIO projects (`platformio.ini`).
- A project made with the wrong tool and no versions yet: `set_backend` (moves `src/*.ino` into a sketch
  folder, `lib/` into `libraries/`, writes `sketch.yaml`). Once a version exists, the tool is fixed.

## 4. Create the project and fill `work/`

`create_project` with a human name, the profile, the backend, and the hardware notes (everything from
step 1, plain text, one fact per line). `get_project` gives the `work/` path.

**arduino-cli layout** (the template sketch is `work/<Name>/<Name>.ino`):
- Replace the template folder with the real sketch folder, keeping its real name: `work/LedArch/`
  holding `LedArch.ino`, its tabs, and a `sketch.yaml` (the template's, with the fqbn and core).
  Exactly one sketch folder.
- Carry the original board menu options into the fqbn in `sketch.yaml`
  (`esp32:esp32:esp32:PartitionScheme=huge_app,...`).
- Libraries: from the Library Manager → nothing to copy, `pin_versions` writes the exact versions it finds
  installed on the vault host (install the version they used first: `arduino-cli lib install "Name@x.y.z"`; never
  upgrade a library or core that is already installed, other sketches rely on it). A library that is local,
  modified or not in the Library Manager → copy it into `work/libraries/<Name>/`.

**PlatformIO layout**:
- Delete the template `src/main.cpp`. Copy `src/ include/ lib/ test/ data/`, partition files, and
  `platformio.ini` (keep the env for this board; carry its `build_flags`, `board_build.*`, `monitor_speed`).
- A registry library → `lib_deps` at the exact version; GitHub only → `https://...git#<full commit>`;
  local or modified → `lib/<name>/`.

**Both**:
- **Docs**: copy into `work/docs/` (it is snapshotted with every version). Skip single files over 20 MB and
  list what was skipped.
- **Write `work/docs/ORIGIN.md`**: where it came from (path, remote, commit, dirty or clean, date imported),
  original IDE/core/board settings, and every change made to get it to build in the vault.
- Change as little as possible. The first vault version should behave like the original. Fix only what the
  build needs, and record each fix.

## 5. Pin, build, seal

`pin_versions`, then `build`. Fix build errors with the smallest change (record it).

arduino-cli builds come in two modes, shown in the manifest as `build_mode`:
- `profile`: no `work/libraries`; arduino-cli installs exactly what `sketch.yaml` names into its own
  folder. The binary is the IDE's binary except for the source paths compiled into asserts.
- `checked`: there is a `work/libraries`; it builds with the vault host's installed core and libraries and
  refuses if any version differs from `sketch.yaml`.

Then `create_version`:
- `note`: "Imported from <where> (<commit>)" or what changed since the last version.
- `summary`: what the firmware does, in plain words, for someone who has never seen it.
- `libraries_added`: every library with why it is there. `ported`: changes made to build it, and logic
  written from scratch. `not_used`: libraries or approaches deliberately left out, with why.
- `hardware_revision` if a PCB revision is known. `link_pcb` first if there is a schematic project.

## 6. Capture the rest

- Each known problem → `add_issue` with `version` set to the new version (one issue per problem;
  `checkable` only when a build, test or serial line can prove it).
- `append_build_story`: the history: how the project got here, decisions and dead ends from the conversation,
  what is known to work on real hardware and what isn't. Plain language, study-guide style.
- History: only current state by default. If they ask for "everything", import tags or key commits oldest
  first, one version each (`note` names the tag/commit). Otherwise mention how many tags exist.
- Don't flash unless they ask (it replaces what is on the board). If they do: `flash_version` (pick the port with
  `list_ports`), then `serial_capture` for boot evidence. If a terminal is open on the page, read it with
  `read_terminals` instead of reopening the port.
- **Boards flashed from the laptop** (a board at a remote site, anything not on the vault host's USB): the
  version's **Flash kit** on the page has the binary and a FLASH.bat (DFU boards) or steps. They flash, then
  click **Flashed from laptop** so their notes attach to that version. A Giga on the vault host's USB must be in
  its bootloader first (double-tap RESET, green LED pulses), then `flash_version`; no port is needed.
- Leave the status at draft, with the reason "Imported, not yet tried from the vault", or `testing` once they are
  trying it. Known good, caveats and do not deploy are theirs to set on the page.

## 7. Report

A few plain lines: project and version, link `http://<vault host>:8200/#<slug>/<version>`, what was captured
(source, N libraries pinned, docs, issues logged, story), every change made to the source, what was skipped and
why, and what only they can do (mark known good if it is the build that runs today, answer the credentials
question, check wiring notes). Say plainly what is unverified.

## Working in a vault project afterwards

The loop: `list_open_issues` → edit `work/` → `build` → `pin_versions` if anything floats → `create_version`
(with `fixes` = issue ids it tries) → `flash_version` → ask the user to test → their notes arrive as issues on the
on-board version. Every build that goes on a board is a version, even a quick probe. When the wiring changes,
`set_hardware_notes` (the next version's `wiring.md` follows). Use `get_pin_map` instead of guessing pins when
a PCB is linked. Raise a real tradeoff (library vs. porting logic, a pin change) with the user before doing it.

## Rules the vault enforces (don't fight them)

- Only the human marks known good / caveats / do not deploy, and closes hardware issues. Claude marks
  "fix attempted" and closes only checkable issues, always with evidence.
- No version while anything floats (platform, framework, toolchain, every library).
- Flashing writes stored artifacts after a hash check; it never rebuilds. Ports held by page terminals are
  released and reopened automatically.
- Vault projects are git repos the store commits itself; don't commit in `~/vault` by hand.
