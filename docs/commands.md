# CLI commands

Examples run from the repository root. Replace `printer.example` with the printer's LAN address:

```sh
export BAMBU_HOST=printer.example
```

Use `--host` on an individual command to override that value. `--serial YOUR_PRINTER_SERIAL` or `BAMBU_SERIAL` selects a saved printer when Studio knows more than one. Access codes still come from your existing Studio configuration; none belongs in arguments or environment variables.

## Discover and inspect

```sh
python3 bambu.py printers
python3 bambu.py status
python3 bambu.py status --json
python3 bambu.py inspect part.gcode.3mf
python3 bambu.py check part.gcode.3mf --tray 1
```

`printers` discovers saved identities in Studio's two access-code maps. It reveals serials, not code values. It does not scan the network or infer an address. `inspect` is offline; `check` compares the slice with a fresh idle state, AMS material and nozzle diameter. Both retain the original H2D/right-0.4-mm/single-filament slice restrictions.

`status` works while printing and needs no local file. Human output includes the job, progress, layer, remaining time, bed temperature, AMS trays and nozzle configuration. JSON also retains the printer's reported generic nozzle-temperature fields without assuming which side they represent.

## Follow a job

```sh
python3 bambu.py watch --until-done
python3 bambu.py watch --job part.gcode --until-done --timeout 7200 --json
python3 bambu.py watch --count 3 --interval 5
```

Plain `watch` prints changed snapshots until its deadline or Ctrl-C. JSON mode emits every sample as JSON Lines. Defaults are five seconds between requests and a one-hour deadline; `--timeout 0` removes the deadline. Monitoring waits when another local command holds the printer lock, then resumes without retrying that command. This is a foreground command, not a background service.

`--until-done` captures the current active job and stops if another job replaces it. With `--job`, the exact name must match. A stale `FINISH` from an earlier job is not silently treated as a new successful print. A specifically named already-finished job may return immediately. Deadlines are checked between network requests; a stalled request can delay exit by the adapter's connection timeout.

`FINISH` returns 0, `FAILED` or an unsuccessful idle ending returns 1, and a deadline or sample limit reached before completion returns 3. A changed/mismatched job returns 2. Ctrl-C ends monitoring without controlling the printer.

## Upload without starting

```sh
python3 bambu.py upload part.gcode.3mf
python3 bambu.py upload part.gcode.3mf --name /cache/review-part.gcode.3mf
```

Upload validates the sliced file, requires an idle printer, transfers through certificate-validated FTPS, and downloads the full remote file to compare its length and MD5. An identical destination is reused. Different existing contents are refused; choose a new name. The destination directory must already exist. Upload never sends a print command and does not require the intended material to be loaded yet.

## Manage printer storage

```sh
python3 bambu.py files list
python3 bambu.py files list --directory /cache --json
python3 bambu.py files download /part.gcode.3mf ./saved-part.gcode.3mf
python3 bambu.py files rename /part.gcode.3mf /renamed-part.gcode.3mf
python3 bambu.py files delete /renamed-part.gcode.3mf --yes
```

Paths may point into existing directories and may contain spaces when quoted. Traversal segments, control characters and wildcards are rejected. On firmware without MLSD, listing falls back to NLST plus binary-mode SIZE; entries whose type cannot be established are labeled `unknown`.

Downloads can read regular files and work while printing. The local parent directory must exist. A download is written to a temporary file, checked against remote metadata and length, then installed without overwriting any existing destination. It returns a local MD5. An interrupted transfer does not leave a partial final file.

Renaming and deleting are limited to sliced `.gcode.3mf` files and require an idle printer. Rename refuses an occupied destination and compares the full file hash before/after. Delete requires `--yes`, applies to one exact file, and verifies its disappearance. There is no recursive deletion or remote overwrite option. Deletion has no trash/recovery mechanism.

Local per-printer locks coordinate this account's commands only. Another client can still change printer state or storage during an operation; this is not a distributed locking protocol.

## Start and control a job

```sh
python3 bambu.py print part.gcode.3mf --tray 1
python3 bambu.py pause --job part.gcode --yes
python3 bambu.py resume --job part.gcode --yes
python3 bambu.py cancel --job part.gcode --yes
```

`print` uses the original sender, including its receipt written before dispatch, file/nozzle/material checks and matching-job start confirmation. `--repeat` deliberately allows another attempt for a previously submitted file. The original `send_h2d_print.py FILE --host HOST --start` form remains supported.

Pause requires `RUNNING`; resume requires `PAUSE` and a reported zero print-error value; cancel accepts `PREPARE`, `RUNNING` or `PAUSE`. All three require an exact job name and `--yes`. The native helper rechecks the job and permitted state after certificate initialization and before dispatch. These checks do not override the printer's interlocks or resolve physical faults.

Each control is sent once. Confirmation requires both a matching command/sequence acknowledgment and post-dispatch telemetry: `PAUSE`, `RUNNING`, or `IDLE`/`FAILED` respectively. A naturally completed `FINISH` is not reported as a successful cancel. Missing evidence returns an uncertain result; inspect status before deciding whether to try again. The CLI never retries a control automatically.

**Validation limit:** pause/resume/cancel have been exercised through the compiled helper using an offline fake networking plugin. Physical control transitions have not yet been tested on an active print.

The wire command shapes follow [Bambu Studio v02.08.02.61's task-control implementation](https://github.com/bambulab/BambuStudio/blob/v02.08.02.61/src/slic3r/GUI/DeviceManager.cpp#L1418-L1460): `pause`, `resume`, and `stop`, with an empty `param` and a sequence ID.

## Output and exit codes

All commands accept `--json` and `--report PATH` after the command name (after the file subcommand for `files`). Reports use mode 0600 and contain the latest result; watch replaces its report with the newest snapshot/event. Keep stdout/stderr separate when consuming JSON. Diagnostic errors go to stderr; print/check progress moves to stderr in JSON mode.

| Exit | Meaning |
| --- | --- |
| 0 | Requested operation or bounded observation succeeded |
| 1 | A followed job ended unsuccessfully |
| 2 | Invalid input, rejected operation, connection error or unconfirmed command |
| 3 | Watch deadline/sample limit before requested completion |
| 130 | Interrupted by the user |

Reports and output exclude access codes and tokens, but may contain private filenames, material information and saved printer identities. Review before sharing. Persistent submission/control receipts are in `~/Library/Caches/h2d-headless/`.
