# Validation scope

## Linux/P1S, November 2, 2026

Tested on a Debian x86-64 Docker host with an Ubuntu 24.04 container, Studio
`02.08.02.61`, Linux networking plugin `02.08.02.54`, and P1S firmware `01.10.00.00`.
The test slice uses one PETG filament, AMS A2, a 0.4 mm nozzle, and Textured PEI Plate.

- Slice checksum, plate/model, single-extruder mapping, live model/identity, AMS
  material, and nozzle diameter checks passed through the CLI.
- One signed `project_file` submission was acknowledged successfully and the
  matching job entered PREPARE, then RUNNING and advanced through printed layers.
- Pause confirmed PAUSE at layer 4. Resume confirmed RUNNING and subsequent
  telemetry advanced through layers 5 and 6. Cancel confirmed the stopped job at
  layer 9. A separate status query reported print_error 0 and both heater targets
  at 0°C. Each control used its normal exact-job/state checks and required a
  matching acknowledgment plus post-dispatch telemetry.
- Bed-target commands at 35°C and back to 0°C each received matching telemetry.
  The bed was already warmer than 35°C; this verifies target control, not a
  heating-performance measurement.
- A fresh upload completed with full read-back verification. The P1S withheld
  TLS close_notify on the original path after accepting all file bytes; a traced
  shutdown call timed out after 20 seconds. The P1S-specific completion path
  closed the encrypted data connection, required the FTP success reply, and
  verified all bytes and the checksum without that timeout.
- CLI download and rename preserved the test file's size and checksum; deletion
  was verified. Both temporary upload probes were removed from printer storage.
- All 54 offline tests passed on Linux. On macOS, 53 passed and the Linux-only
  shim test was skipped; the shared bridge also read live P1S status successfully.

The October H2D print observations below used the original Objective-C++ bridge.
The shared C++ bridge has not been retested on a physical H2D.
The P1S test was deliberately canceled; it does not establish natural completion,
part quality, or sustained automation reliability.

## Expanded CLI, version 0.2.0

Verified October 26, 2026 on the same macOS/H2D installation as the original sender:

- Saved-printer discovery returned the configured printer without exposing its access code.
- Live `status`, two-sample JSON `watch`, offline `inspect`, and JSON `check` completed successfully. The printer remained in `FINISH` throughout.
- A uniquely named temporary sliced file exercised the real CLI's upload and full read-back, identical-file reuse, download and byte comparison, rename and hash verification, and delete verification. The original remote directory membership was restored.
- 43 automated tests passed, including the original sender checks, path/overwrite/interrupted-download guards, job monitoring, explicit control confirmation, and compiled native-helper tests using a fake plugin with no networking.
- The native fixture exercises pause/resume/cancel acknowledgments and state transitions, a job/state change during certificate initialization, printer rejection, and resume with an unresolved error. The original print-start path also passes that fixture.

**No physical pause, resume or cancel was sent, and no additional print was started.** Those hardware transitions remain unverified. The fake plugin validates the helper's control logic and ABI call shape; it does not prove a particular firmware will acknowledge or perform those controls.

All hardware transfer checks used temporary task-owned files. No existing print file was renamed, deleted or overwritten. No GUI was opened or printer mode changed.

## Hardware observation, October 26, 2026

The original sender was exercised on an Apple silicon Mac with Studio `02.08.02.61`, networking plugin `02.08.02.60`, and an H2D in cloud mode with Developer Mode off.

- A sliced PLA file selected one filament, the right 0.4 mm nozzle and AMS A2.
- Preflight read fresh idle-state, AMS-material and nozzle telemetry.
- The existing file on printer storage matched a full download by size and MD5.
- One submission received a successful response; matching-job telemetry reached `PREPARE`. An independent printer logger subsequently reported `RUNNING`.
- A separate temporary copy tested a new upload, complete download verification and remote cleanup. That copy was never printed.
- A later check-only CLI invocation during the running job refused to proceed.
- During release preparation, the extracted CLI's check-only invocation read `FINISH` and passed the material/nozzle checks. The independent logger reported the same job at `FINISH`, 100%, layer 15.

Raw logs, printer identities, credentials, personal print files and account caches are intentionally excluded from this public repository. These are maintainer-reported hardware observations, not independently reproducible public traces. Firmware-reported completion was observed; part quality and mechanical fit were not inspected.

## Public extraction

The sender, native adapter and shim were extracted from that working implementation. Packaging adds documentation, licensing, focused tests, restrictive permissions for existing report files, deterministic lock-file cleanup, and clearer wording about the diagnostic calibration probe. No additional print is needed to run the automated checks.

All 13 focused tests passed. They cover archive corruption, nozzle mapping, busy and paused states, material and nozzle mismatch, delta telemetry, job-name matching, acknowledgment-only responses, the default check-only path, report permissions, and receipts blocking a retry after an uncertain submission. They use synthetic fixtures and fake connections.

The native helper compiled successfully without loading the proprietary plugin. Loading it or querying a real printer requires the user's existing Studio installation and credentials. The public extraction passed a live **read-only** invocation; this did not repeat the physical start test.

## Known limits

- Only the configuration above has been tested. A version-family guard is a conservative filter, not proof that every plugin in that family is ABI-compatible.
- The printer's exact firmware version was not captured in the retained start evidence.
- File checksums detect transfer errors. They do not establish that arbitrary G-code is safe or appropriate for a printer.
- Live checks cannot establish a clear build plate, the physical spool's identity or the absence of concurrent commands from other clients.
- Matching telemetry proves the requested start state, not a completed or successful print.
- Cloud certificate refresh and an existing proprietary plugin installation remain dependencies.
