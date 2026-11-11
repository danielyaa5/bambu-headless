# bambu-headless

Manage files, follow jobs and start a sliced print on a Bambu Lab H2D or P1S from a terminal, with **Cloud/Handy still enabled** and **Developer Mode off**. Bambu Studio does not need to be running.

Read the build write-up: **[Bypassing Bambu's MQTT signature check by masquerading as Bambu Studio](https://danielyakobian.com/blog/bambu-headless-cli.html)**.

Experimental Linux x86-64 support is available through [Docker Compose](docs/linux-docker.md). Both platforms use the same C++ bridge. Slice submission supports a 0.4 mm nozzle and AMS feeding: one filament (slice filament ID 1) with `--tray`, or a multi-filament plate with `--ams-map`, one AMS slot per filament. On H2D, AMS 0 feeds the right nozzle and AMS 1 the left.

This is an experimental open-source wrapper around **Bambu's installed proprietary networking plugin**. It is not an official Bambu CLI or an independent replacement for that plugin. A small process-local shim changes the executable-path lookup used by the plugin's signed-host check. See [how it works](docs/how-it-works.md) before using it.

## Tested scope

| Component | Verified configuration |
| --- | --- |
| Computer | macOS, Apple silicon |
| Studio / plugin | Studio `02.08.02.61`, plugin `02.08.02.60` |
| Printer | H2D, right 0.4 mm nozzle |
| Print | One filament, PLA, AMS A2, one selected plate |
| Account | Existing signed-in Studio configuration, US region |
| Result | Matching job reached `PREPARE`; independent logger observed `RUNNING`, later `FINISH` at 100% |

The helper rejects plugins outside the `02.08.02` version series. The macOS configuration above and the [Linux/P1S configuration](docs/linux-docker.md#validation) have been exercised on hardware. Linux/P1S validation includes print submission, layer progress, pause, resume, cancel, and bed-target control. Multi-filament submission with `--ams-map` was exercised once on H2D (8 filaments on both nozzles from two AMS units, November 8, 2026): the live check passed and the job was confirmed in `PREPARE`. It has not been followed to completion. Other configurations, Intel Macs, regions, external spools and other nozzle sizes are untested or unsupported. The print command verifies startup; `watch --until-done` follows firmware-reported completion. Neither inspects part quality. The P1S test was deliberately canceled after verifying resume; natural completion was not tested.

## Requirements

These are the macOS requirements; see the [Linux setup guide](docs/linux-docker.md) for Docker requirements and configuration.

- Python 3.10 or newer. No third-party Python packages.
- Xcode Command Line Tools (`clang` and `clang++`). Install with `xcode-select --install` if needed.
- nlohmann-json C++ headers: `brew install nlohmann-json`. The compiler searches the standard Apple silicon and Intel Homebrew include directories; for another installation, set `CPLUS_INCLUDE_PATH` to its include directory.
- Bambu Studio at `/Applications/BambuStudio.app`, its networking plugin installed, and a saved printer access code and signed-in session in your own Studio configuration.
- LAN access to the printer on ports 990 and 8883. The plugin also needs internet access for certificate refresh; this is not a fully offline workflow.
- An already sliced `.gcode.3mf` for inspect/check/upload/print commands. Status, monitoring and file management do not require a local print file. This tool does not slice models.

## Use

```sh
git clone https://github.com/danielyaa5/bambu-headless.git
cd bambu-headless
python3 send_h2d_print.py part.gcode.3mf --host printer.example
```

Replace `printer.example` with your printer's LAN address. That original command remains supported: it checks the file and live printer state without uploading or starting. Native helpers compile automatically on first use.

The expanded `bambu.py` CLI provides these commands. `BAMBU_HOST` and optional `BAMBU_SERIAL` avoid repeating connection options; `--host` and `--serial` override them.

```sh
export BAMBU_HOST=printer.example
python3 bambu.py printers
python3 bambu.py status
python3 bambu.py watch --until-done
python3 bambu.py upload part.gcode.3mf
python3 bambu.py files list
```

| Command | Behavior |
| --- | --- |
| `printers` | Lists Studio's saved printer identities, never access-code values; no LAN scan |
| `history` | Lists Bambu Handy / Studio cloud print history tasks (`--limit`, `--offset`) |
| `status` | Fresh job, progress, temperatures, AMS and nozzle details |
| `watch` | Progress monitoring, JSON Lines, deadlines and optional completion exit |
| `inspect FILE` | Offline validation and slice details |
| `check FILE` | Slice and live printer compatibility checks |
| `upload FILE` | Upload plus full read-back verification; no print command |
| `print FILE` | Upload, register in Bambu Handy cloud history (or `--no-cloud-record`), and start |
| `files list/download/rename/delete` | Printer storage management; deletion requires `--yes` |
| `pause/resume/cancel` | One control command, requiring `--yes` and the exact `--job` name |
| `bed-temp TEMP` | Set target bed temperature (`0..120` C) via `M140 S<TEMP>` and confirm target telemetry |

See the [command guide](docs/commands.md) for examples, automation output and exit codes. Destructive file operations are limited to `.gcode.3mf`; uploads and renames refuse to replace different existing files. Storage mutations require an idle printer.

After checking that the plate is clear and the intended material is loaded:

```sh
python3 bambu.py print part.gcode.3mf --host printer.example
```

Defaults: plate 1, 0.4 mm nozzle (left or right on H2D), AMS tray `1` (A2). Use `--tray 0` for A1, `--tray 2` for A3, `--tray 5` for B2, etc. A plate with several filaments needs `--ams-map` with one slot per filament in filament order, for example `--ams-map B3,B1,B2,A4,A2,A1,B4,A3`; each slot must hold that filament's material and reported colour on the AMS feeding the filament's sliced nozzle, or nothing is uploaded (`--allow-color-mismatch` relaxes only the colour check). Prints are registered in Bambu Handy / Studio cloud printing history by default via `bambu_network_start_local_print_with_record`; add `--no-cloud-record` to send a raw LAN MQTT `project_file` command without creating a cloud task. The printer's camera timelapse is recorded by default; add `--no-timelapse` to skip it for one job. Add `--serial YOUR_PRINTER_SERIAL` if Studio has more than one saved printer. Credentials are read from Studio; do not put access codes or tokens on the command line.

`--json` gives machine-readable output; `watch` emits one JSON object per line. `--report reports/result.json` saves the latest result. Reports exclude access codes and tokens, but may contain job names, file paths, materials, and (for `printers`) saved printer serials. Review them before sharing.

## Start guards and retry behavior

- Verifies the archive and embedded G-code checksum, selected plate, printer model and supported nozzle/filament mapping.
- Verifies the printer's model and identity through the official plugin before upload and again before dispatch. Reads fresh telemetry; refuses busy, paused or unknown states and mismatched AMS material or nozzle diameter. It cannot verify the physical plate or spool contents.
- Uploads over certificate-validated implicit FTPS and downloads the full file to compare size and MD5. An existing identical file is reused.
- Uses a per-printer local lock and saves a receipt **before** dispatch. A timeout is an uncertain outcome, not permission to retry.
- Reports success only when telemetry identifies the requested job in `PREPARE` or `RUNNING`. A publish acknowledgment alone is insufficient.

Receipts live in `~/Library/Caches/h2d-headless/` on macOS, or `$XDG_CACHE_HOME/h2d-headless/` (default `~/.cache/h2d-headless/`) on Linux. Docker Compose persists them in `./cache/`. To intentionally print the same file again, inspect the printer and add `--repeat --start`. This does not override the busy-printer guard. The lock and receipts only coordinate copies running under the same local account; they cannot prevent another computer or Handy from submitting a job concurrently.

`--sign-probe` is a read-only calibration request kept for diagnosis. Its success does **not** establish that the plugin will authorize a print command.

## Development and evidence

```sh
python3 -m unittest discover -p 'test_*.py' -v
python3 -c 'import send_h2d_print as s; s.ensure_bridge()'
```

Tests use synthetic files and mocked connections; they do not contact a printer. The native compile check does not load the plugin. [Validation notes](docs/validation.md) distinguish these checks from the earlier physical-printer run.

Source files: [expanded CLI](bambu.py), [file operations](bambu_files.py), [original sender](send_h2d_print.py), [shared native adapter](h2d_network_bridge.cpp), [macOS executable-path shim](h2d_host_identity.c), [Linux executable-path shim](h2d_host_identity_linux.c). Tests include [CLI and transfer behavior](test_cli.py), [native control behavior](test_native_bridge.py) and [original sender guards](test_send_h2d_print.py).

## License and credits

The wrapper is licensed under [GNU AGPL v3](LICENSE). The vendored, unmodified ABI header comes from [Bambu Studio v02.08.02.61](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/Utils/bambu_networking.hpp); its upstream license is retained in `vendor/`.

Bambu's proprietary plugin is **not included** or relicensed. Users supply their own installed copy. The investigation benefited from [ClusterM/open-bamboo-networking](https://github.com/ClusterM/open-bamboo-networking)'s published LAN, certificate and print-ABI research. See [source credits](docs/how-it-works.md#references). This project is unaffiliated with Bambu Lab.
