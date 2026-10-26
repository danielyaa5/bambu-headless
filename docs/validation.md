# Validation scope

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
