# Firmware Vault

Every known build of a microcontroller project, kept whole: the binary you
flash, the full source, the exact toolchain and library versions, a
plain-language build story, and a running log of what was wrong with it. You
flash from the vault, not from an IDE, so a library update can never take a
working build away from you.

It runs on a Linux machine that the boards plug into, and has three faces:
a web page for the human (browse versions, flash, mark what works, serial
terminals), an MCP server for Claude Code (edit, build, seal versions, log
issues), and the `fv` command line. Spec: `docs/SPEC.md`. Original UI mockup:
`docs/mockup.html`.

## Status

Early, and used on real hardware by one person so far.

| Board | Build | Flash | Tried on hardware |
|---|---|---|---|
| Arduino Mega 2560 | PlatformIO, arduino-cli | avrdude from the vault host | yes |
| Arduino Giga R1 (M7) | arduino-cli | DFU (vault host, or flash kit + FLASH.bat on Windows) | yes, deployed from a flash kit |
| Arduino Uno, Nano | PlatformIO, arduino-cli | avrdude | builds only |
| ESP32 DevKit, Olimex ESP32-POE-ISO | PlatformIO, arduino-cli | esptool (+ merged.bin for browser flashers) | builds only |
| Nucleo-F401RE | PlatformIO | openocd | not tried, written from the datasheet |

Known limits:
- Linux only (systemd user service, `/dev/serial/by-id`). The flash kits are
  for flashing from Windows.
- The web page has no login. Run it on a trusted network only.
- With arduino-cli 1.2, a sketch with its own local libraries builds with the
  installed core and libraries and checks their versions, instead of an
  isolated install (see "arduino-cli projects").
- The browser mic for voice notes and Web Serial need HTTPS, which the page
  does not have yet.

## Install

Needs Python 3.11+, git, and for Arduino IDE sketches `arduino-cli`
(`sudo apt install arduino-cli`, then install the cores you use with
`arduino-cli core install ...`).

```
git clone <this repo> firmware-vault
cd firmware-vault
./install.sh             # .venv with PlatformIO, esptool, pyserial; links `fv` into ~/.local/bin
./install.sh --service   # also runs the web page as a systemd user service on :8200
./test.sh                # fast tests; ./test.sh --slow also does real builds
```

Then open http://localhost:8200 (open port 8200 in the firewall to reach it
from other machines). The vault lives in `~/vault` (override with
`FIRMWARE_VAULT_ROOT`). Your user needs to be in the `dialout` group to
flash and use serial ports.

With Claude Code (optional):

```
claude mcp add --scope user firmware-vault -- /path/to/firmware-vault/bin/fv mcp
ln -s /path/to/firmware-vault/skill ~/.claude/skills/firmware-vault
```

The skill lets you say "put this in the firmware vault" about a sketch or
PlatformIO project, and Claude imports it with everything it can find
(see "The Claude Code skill").

A first project from the command line:

```
fv profiles                                   # board profiles
fv new "Blink" arduino-uno --backend arduino-cli
fv show blink                                 # shows the work/ folder; put your sketch there
fv pin blink && fv snapshot blink "first build" --summary "Blinks the LED"
fv flash blink v1                             # or use the Flash button on the page
```

## Parts

| Part | Where | What it does |
|---|---|---|
| Store | `fvault/store.py` | Project folders, versions, statuses, issues, flash records, and every rule about who may do what. Each project folder is its own git repo. |
| Backends | `fvault/backends/` | Build tools behind one interface (build, report versions, check pinning, pin): PlatformIO and arduino-cli. |
| Flashers | `fvault/flashers.py` | Write *stored* artifacts to a board. Never rebuild. avrdude, esptool, openocd. |
| Profiles | `profiles/*.json` | Board families. Adding a board = adding a file. |
| PCB link | `fvault/pcb.py` | Reads the real pin map from an EAGLE/Fusion schematic. |
| Web page | `fvault/web.py`, `web/index.html` | The human's side: browse, flash, stamp, debug notes. Port 8200. |
| MCP server | `fvault/mcp.py` | Claude Code's side: same store, Claude's rules. |
| CLI | `bin/fv` | Same as MCP, from a shell. Acts as Claude Code. |
| Terminals | `fvault/serialmon.py` | Live serial terminals on the page, several ports at once. Held by the web service. |

## Who may do what

| | Human (web page) | Claude Code (MCP, CLI) |
|---|---|---|
| Set draft / testing | yes | yes, unless the human already stamped it |
| Set known good / caveats / do not deploy | yes | no |
| Flash a do-not-deploy version | yes, after a warning | no |
| Close an issue that needs the hardware | yes | no, only mark fix attempted |
| Close a checkable issue (build, tests, serial) | yes | yes, with evidence stored |

## Data layout

The vault lives in `~/vault` (override with `FIRMWARE_VAULT_ROOT`).

```
~/vault/<project>/
  project.json  state.json  SUMMARY.md
  work/                     live source Claude edits
  versions/v3/
    artifacts/              firmware.hex | bootloader.bin partitions.bin boot_app0.bin firmware.bin merged.bin, firmware.elf
    source/                 full snapshot
    manifest.json           exact versions, hashes, flash plan, profile fields, human notes
    status.json             status, reason, who, when, history
    build-story.md          Claude's story of the build (study-guide style)
    debug-log.md            append-only
    wiring.md               pin map (from the PCB when linked)
    build.log
    issues/I-007.json
    serial/                 captured serial output, used as evidence
```

## Pinning

A version cannot be created while anything floats. "Anything" means the
platform, the framework package, the toolchain package and every library.
`fv pin <project>` (MCP: `pin_versions`) resolves them once and writes the
exact versions into `platformio.ini`. A PlatformIO platform only pins its
framework to a range such as `~4.30312.0`, which is how a "library update"
can change a build that nobody touched, so the framework and toolchain go
into `platform_packages` too.

## Flashing from the laptop

Boards are normally plugged into the vault host. For a board on another
computer, each version has a **Flash kit** (zip: binaries, manifest,
steps). ESP32 kits include `merged.bin`, flashed at 0x0 from Espressif's
browser flasher in Chrome. Then **Flashed from laptop** on the page records it, so debug
notes attach to the right version.

## Serial terminals

The page's left column has a **Comms** section: every USB serial port on
the vault host (with the board type its USB id matches and which project's
version is on it) and any number of terminals. Each terminal has a port,
a baud rate, follow/timestamps/hex, a reset pulse (RTS then DTR), save to
file, and a send line with a line-ending choice and history. **⤢** docks
a terminal full width under the main panels, so several ports can be
watched side by side. Terminals are remembered per browser.

The web service holds the ports (`fvault/serialmon.py`), so several tabs
can watch the same port and it keeps running when the page is closed (a
terminal nobody reads for 15 minutes is closed). Before any flash, from
the page, the CLI or the MCP server, the port is released and then
reopened, and the terminal shows both events inline. `serial_capture`
reads through an open terminal instead of reopening the port, so it does
not reset the board. Claude Code can read what the terminals show with
the MCP tool `read_terminals` (or `fv terminals`).

API: `GET /api/serial/ports`, `GET /api/serial/read?port=&since=` (base64
bytes plus events, by stream position), `POST /api/serial/open|close|send|baud|reset|pause|resume|tap`.

## Choosing the port to flash

Next to **Flash to board** the page lists the USB ports, the ones matching
the project's board profile first. It picks the port the project was last
flashed on, or the only matching board, and remembers your choice.

## arduino-cli projects

For sketches built in the Arduino IDE, where the build must match the IDE's
(same core, same libraries), e.g. the Giga R1 on `arduino:mbed_giga@4.6.0`.

```
work/<Sketch>/<Sketch>.ino   + tabs + sketch.yaml
work/libraries/<Lib>/        local or modified libraries (optional)
work/docs/
```

`sketch.yaml` holds a profile called `vault`: the FQBN (with menu options),
the core at an exact version (which fixes its compiler and tools), and
every library at an exact version. `fv pin` fills it from what is installed
on the vault host (`arduino-cli compile --dump-profile`). It never installs over
a core version that is already installed.

Builds run in one of two modes, recorded as `build_mode` in the manifest:
- `profile` (no `work/libraries`): `arduino-cli compile --profile vault`.
  arduino-cli installs exactly the named versions into its own folder
  (`~/.arduino15/internal`), so updating a library for another sketch can
  never change this build.
- `checked` (with `work/libraries`): arduino-cli 1.2 cannot mix profiles and
  local libraries, so it builds with the installed core and libraries plus
  `work/libraries`, then refuses if any version differs from `sketch.yaml`.

Outputs are renamed to what the flashers expect (`firmware.bin/.hex/.elf`,
`bootloader.bin`, `partitions.bin`). Checked against a real Giga sketch:
a profile build is byte-identical between runs and differs from the IDE's
build only by the source paths compiled into assert messages.

`fv set-backend <project> arduino-cli` converts a project with no versions
yet (PlatformIO layout `src/*.ino` + `lib/` becomes the sketch layout).

## The Claude Code skill

`skill/SKILL.md` (linked as `~/.claude/skills/firmware-vault`) tells Claude
Code how to import a project when asked to "put this in the firmware vault":
find the source, choose or add a board profile, fill `work/` (Arduino
sketches go into `src/` as `.ino`), write `docs/ORIGIN.md`, pin, build, seal,
then log known bugs on the new version (`add_issue` with `version`), set the
hardware notes (`set_hardware_notes` / `fv hw-notes`) and write the story.
The page opens a version directly at `#<project>/<version>`.

## Voice notes

`POST /api/notes {"text": "...", "source": "voice"}` adds an issue to the
project whose debug session is live, on the version on its board. That is
the hook for the voice notepad. The page's own mic button needs HTTPS (or
localhost); over plain http on the LAN it offers typing instead.

## Decisions on the spec's open questions

- **Front end**: a local web page on the machine the boards plug into.
- **Backends**: PlatformIO and arduino-cli (see below). A profile names its
  default; `create_project --backend` picks the other when the profile has
  settings for both.
- **STM32**: Nucleo-F401RE profile written from the datasheet. No board yet.
- **Wiring notes**: a separate `wiring.md` per version, generated from the
  PCB schematic when the project is linked, and named in the manifest.

## License

No license has been chosen yet, so all rights are reserved for now. Ask
before reusing the code.

Maintained by lostmachinist.
