# bambu-headless

Manage files, follow jobs and start a sliced print on a Bambu Lab H2D from a macOS terminal, with **Cloud/Handy still enabled** and **Developer Mode off**. Bambu Studio does not need to be running.

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

The helper rejects plugins outside the `02.08.02` version series; only the version above has been exercised on hardware. Other printers, Intel Macs, regions, external spools, dual-material prints and other nozzle sizes are untested or unsupported. The print command verifies startup; `watch --until-done` follows firmware-reported completion. Neither inspects part quality. Pause/resume/cancel are tested with an offline plugin fixture, **not yet with a physical print**.

## Requirements

- Python 3.10 or newer. No third-party Python packages.
- Xcode Command Line Tools (`clang` and `clang++`). Install with `xcode-select --install` if needed.
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
| `status` | Fresh job, progress, temperatures, AMS and nozzle details |
| `watch` | Progress monitoring, JSON Lines, deadlines and optional completion exit |
| `inspect FILE` | Offline validation and slice details |
| `check FILE` | Slice and live printer compatibility checks |
| `upload FILE` | Upload plus full read-back verification; no print command |
| `print FILE` | Upload and start, preserving the original submission guards |
| `files list/download/rename/delete` | Printer storage management; deletion requires `--yes` |
| `pause/resume/cancel` | One control command, requiring `--yes` and the exact `--job` name |

See the [command guide](docs/commands.md) for examples, automation output and exit codes. Destructive file operations are limited to `.gcode.3mf`; uploads and renames refuse to replace different existing files. Storage mutations require an idle printer.

After checking that the plate is clear and the intended material is loaded:

```sh
python3 bambu.py print part.gcode.3mf --host printer.example
```

Defaults: plate 1, right 0.4 mm nozzle, AMS tray `1` (A2). Use `--tray 0` for A1, `--tray 2` for A3, etc. Add `--serial YOUR_PRINTER_SERIAL` if Studio has more than one saved printer. Credentials are read from Studio; do not put access codes or tokens on the command line.

`--json` gives machine-readable output; `watch` emits one JSON object per line. `--report reports/result.json` saves the latest result. Reports exclude access codes and tokens, but may contain job names, file paths, materials, and (for `printers`) saved printer serials. Review them before sharing.

## Start guards and retry behavior

- Verifies the archive and embedded G-code checksum, selected plate, printer model and supported nozzle/filament mapping.
- Reads fresh telemetry; refuses busy, paused or unknown states and mismatched AMS material or nozzle diameter. It cannot verify the physical plate or spool contents.
- Uploads over certificate-validated implicit FTPS and downloads the full file to compare size and MD5. An existing identical file is reused.
- Uses a per-printer local lock and saves a receipt **before** dispatch. A timeout is an uncertain outcome, not permission to retry.
- Reports success only when telemetry identifies the requested job in `PREPARE` or `RUNNING`. A publish acknowledgment alone is insufficient.

Receipts live in `~/Library/Caches/h2d-headless/`. To intentionally print the same file again, inspect the printer and add `--repeat --start`. This does not override the busy-printer guard. The lock and receipts only coordinate copies running under the same local account; they cannot prevent another computer or Handy from submitting a job concurrently.

`--sign-probe` is a read-only calibration request kept for diagnosis. Its success does **not** establish that the plugin will authorize a print command.

## Development and evidence

```sh
python3 -m unittest discover -p 'test_*.py' -v
python3 -c 'import send_h2d_print as s; s.ensure_bridge()'
```

Tests use synthetic files and mocked connections; they do not contact a printer. The native compile check does not load the plugin. [Validation notes](docs/validation.md) distinguish these checks from the earlier physical-printer run.

Source files: [expanded CLI](bambu.py), [file operations](bambu_files.py), [original sender](send_h2d_print.py), [native adapter](h2d_network_bridge.mm), [executable-path shim](h2d_host_identity.c). Tests include [CLI and transfer behavior](test_cli.py), [native control behavior](test_native_bridge.py) and [original sender guards](test_send_h2d_print.py).

## License and credits

The wrapper is licensed under [GNU AGPL v3](LICENSE). The vendored, unmodified ABI header comes from [Bambu Studio v02.08.02.61](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/Utils/bambu_networking.hpp); its upstream license is retained in `vendor/`.

Bambu's proprietary plugin is **not included** or relicensed. Users supply their own installed copy. The investigation benefited from [ClusterM/open-bamboo-networking](https://github.com/ClusterM/open-bamboo-networking)'s published LAN, certificate and print-ABI research. See [source credits](docs/how-it-works.md#references). This project is unaffiliated with Bambu Lab.
