# Firmware Vault - tasks

## Done
- Store: projects as git repos; versions with binary, source snapshot,
  manifest (exact versions, hashes, flash plan), build story, debug log,
  wiring and issues; SUMMARY.md with the latest known good. Status and issue
  rules enforced in the store (human vs Claude Code).
- Backends: PlatformIO (pins platform, framework, toolchain, libraries in
  platformio.ini) and arduino-cli (sketch.yaml `vault` profile; profile and
  checked build modes).
- Flashers on stored artifacts, hash check first: avrdude, esptool (offsets
  from the partition table, plus merged.bin), openocd, dfu-util.
- Board profiles: Uno, Mega 2560, Nano, ESP32 DevKit, Olimex ESP32-POE-ISO,
  Giga R1 (M7), Nucleo-F401RE.
- Web page (port 8200): versions, flashing with a port chooser, flash kits
  (FLASH.bat for DFU boards), several live serial terminals, deep links
  `#<project>/<version>`.
- MCP server, `fv` CLI, Claude Code skill (`skill/`), PCB pin-map link,
  voice-notes endpoint.
- install.sh, requirements.txt, service template.
- Tried on hardware: Mega 2560 flashed from the vault host (avrdude); Giga R1
  firmware built with arduino-cli and deployed from a flash kit.

## Next
- First-flash test for an ESP32 (esptool path) and the ESP32-POE-ISO.
- Newer arduino-cli may allow `dir:` libraries in profiles, which would make
  every arduino-cli build an isolated profile build.
- Rebuild check: rebuild a version from its snapshot and compare hashes.
- HTTPS on the page so the browser mic and Web Serial work.
- Login on the page before exposing it beyond a trusted LAN.

## Blocked
- STM32 profile untested: no STM32 board on hand.
