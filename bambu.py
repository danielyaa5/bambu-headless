#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Headless H2D status, files, printing and guarded job controls."""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time

import bambu_files as files
import send_h2d_print as core

VERSION = '0.2.0'
ACTIVE = {'PREPARE', 'RUNNING', 'PAUSE'}
CONTROL_STATES = {'pause': {'RUNNING'}, 'resume': {'PAUSE'}, 'cancel': ACTIVE}


def output(args, value: dict, message: str | None = None):
    if args.json or message is None:
        print(json.dumps(value, ensure_ascii=True), flush=True)
    else:
        print(message, flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        core.write_private(args.report, value)


def quoted(value) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def live_status(connection: dict) -> dict:
    events, rc = core.bridge_run(connection, 'status')
    if rc:
        raise core.SendError('Live printer status failed; no control command was sent.')
    return core.merged_status(events)


def idle(status: dict):
    if status.get('gcode_state') not in core.IDLE_STATES:
        raise core.SendError('Printer is busy or its state is unknown; storage changes refused.')


def snapshot(status: dict) -> dict:
    names = {'gcode_state': 'state', 'subtask_name': 'job', 'mc_percent': 'percent',
             'layer_num': 'layer', 'total_layer_num': 'layers', 'stg_cur': 'stage',
             'mc_remaining_time': 'remaining_minutes', 'print_error': 'print_error',
             'bed_temper': 'bed_c', 'bed_target_temper': 'bed_target_c',
             'nozzle_temper': 'reported_nozzle_c', 'nozzle_target_temper': 'reported_nozzle_target_c',
             'trays': 'ams_trays', 'nozzles': 'nozzles'}
    return {out: status[key] for key, out in names.items() if key in status}


def status_line(value: dict) -> str:
    return (f"{value.get('state', 'UNKNOWN')} | {quoted(value.get('job', ''))} | "
            f"{value.get('percent', '?')}% | layer {value.get('layer', '?')}/{value.get('layers', '?')} | "
            f"{value.get('remaining_minutes', '?')} min remaining | "
            f"bed {value.get('bed_c', '?')}/{value.get('bed_target_c', '?')} C")


def monitor(args, connection: dict) -> int:
    deadline = time.monotonic() + args.timeout if args.timeout else None
    target, previous, count = args.job, None, 0
    waiting = False
    while True:
        if deadline and time.monotonic() >= deadline:
            output(args, {'event': 'timeout', **(previous or {})}, 'Monitor timeout reached; the printer was not changed.')
            return 3
        try:
            with core.printer_lock(connection):
                status = live_status(connection)
        except core.PrinterBusy:
            if not waiting:
                output(args, {'event': 'waiting', 'reason': 'printer_in_use'}, 'Waiting for another local printer command...')
                waiting = True
            remaining = max(0, deadline - time.monotonic()) if deadline else args.interval
            time.sleep(min(args.interval, remaining))
            continue
        waiting = False
        value = snapshot(status)
        job, state = value.get('job'), value.get('state')
        if args.until_done and target is None:
            if state not in ACTIVE or not job:
                raise core.SendError('No active job to follow. Use status, or name a completed job with --job.')
            target = job
        if target and job != target:
            raise core.SendError('Printer job changed or does not match --job; monitoring stopped.')
        count += 1
        if value != previous or args.json:
            output(args, {'event': 'status', **value}, status_line(value))
            previous = value
        if args.until_done and state in core.IDLE_STATES:
            success = state == 'FINISH'
            output(args, {'event': 'finished', 'success': success, **value},
                   'Job reported FINISH.' if success else f'Job ended in {state}; completion was not successful.')
            return 0 if success else 1
        if args.count and count >= args.count:
            if args.until_done:
                output(args, {'event': 'sample_limit', **value}, 'Sample limit reached before completion.')
                return 3
            return 0
        remaining = deadline - time.monotonic() if deadline else None
        if remaining is not None and remaining <= 0:
            output(args, {'event': 'timeout', **value}, 'Monitor timeout reached; the printer was not changed.')
            return 3
        time.sleep(min(args.interval, remaining) if remaining is not None else args.interval)


def control_preflight(status: dict, command: str, job: str):
    if not job or status.get('subtask_name') != job:
        raise core.SendError('Current job does not match --job; control refused.')
    if status.get('gcode_state') not in CONTROL_STATES[command]:
        raise core.SendError(f'{command.capitalize()} is not valid in the current printer state.')
    if command == 'resume' and status.get('print_error') not in (0, '0'):
        raise core.SendError('Printer reports an error or unknown error state; resolve it before resuming.')


def confirmed_control(events: list[dict], command: str, sequence: str, job: str) -> bool:
    wire = 'stop' if command == 'cancel' else command
    dispatched, acknowledged, changed, rejected = False, False, False, False
    current_job, state = job, None
    for event in events:
        if event.get('event') == 'dispatching':
            dispatched = True
        if not dispatched:
            continue
        if event.get('command') == wire and str(event.get('sequence_id')) == sequence:
            rejected |= event.get('err_code', 0) not in (0, '0') or str(event.get('result', '')).upper() == 'FAIL'
            acknowledged |= str(event.get('result', '')).upper() == 'SUCCESS'
        if event.get('command') == 'push_status':
            current_job = event.get('subtask_name', current_job)
            state = event.get('gcode_state', state)
            if command == 'cancel':
                changed = state in {'IDLE', 'FAILED'} and current_job in (job, '')
            else:
                changed = current_job == job and state == ('PAUSE' if command == 'pause' else 'RUNNING')
    return dispatched and acknowledged and changed and not rejected


def bed_temp_preflight(status: dict, temp: int, job: str | None = None):
    if not 0 <= temp <= 120:
        raise core.SendError('Bed temperature must be between 0 and 120 C.')
    state = status.get('gcode_state')
    if state in core.IDLE_STATES:
        if job and status.get('subtask_name') != job:
            raise core.SendError('Current job does not match --job; bed temperature refused.')
        return
    if state in ACTIVE:
        if not job or status.get('subtask_name') != job:
            raise core.SendError('Printer has an active job; pass the exact --job name from status to change bed temperature.')
        return
    raise core.SendError('Printer state is unknown; bed temperature refused.')


def confirmed_bed_temp(events: list[dict], temp: int, sequence: str) -> bool:
    dispatched, changed, rejected = False, False, False
    for event in events:
        if event.get('event') == 'dispatching':
            dispatched = True
        if not dispatched:
            continue
        if event.get('command') == 'gcode_line' and str(event.get('sequence_id')) == sequence:
            rejected |= event.get('err_code', 0) not in (0, '0') or str(event.get('result', '')).upper() == 'FAIL'
        if event.get('command') == 'push_status' and 'bed_target_temper' in event:
            try:
                changed = round(float(event['bed_target_temper'])) == temp
            except (TypeError, ValueError):
                changed = False
    return dispatched and changed and not rejected


def set_bed_temp(args, connection: dict) -> int:
    if not 0 <= args.temperature <= 120:
        raise core.SendError('Bed temperature must be between 0 and 120 C.')
    with core.printer_lock(connection) as (cache, key):
        status = live_status(connection)
        bed_temp_preflight(status, args.temperature, args.job)
        sequence = str(20000 + time.time_ns() % 9998)
        payload = {'print': {'command': 'gcode_line', 'param': f'M140 S{args.temperature}\n', 'sequence_id': sequence}}
        receipt = cache / f'{key}-bed-temp-{time.time_ns()}.json'
        result = {'command': 'bed-temp', 'target_c': args.temperature, 'attempted': True, 'confirmed': False}
        if args.job:
            result['job'] = args.job
        core.write_private(receipt, result)
        events, rc = core.bridge_run(connection, 'bed_temp', {
            'target_c': args.temperature, 'job_name': args.job or '', 'sequence_id': sequence,
            'payload': json.dumps(payload, separators=(',', ':'))})
        result['confirmed'] = rc == 0 and confirmed_bed_temp(events, args.temperature, sequence)
        result['status'] = snapshot(core.merged_status(events)) if any(e.get('gcode_state') for e in events) else {}
        core.write_private(receipt, result)
        bed_c = result['status'].get('bed_c', status.get('bed_temper', '?'))
        output(args, result, f"Bed target: {'confirmed' if result['confirmed'] else 'UNCONFIRMED'} at {args.temperature} C (current {bed_c} C).")
        if not result['confirmed']:
            raise core.SendError('Bed temperature outcome is uncertain or rejected. Check status before issuing another command; no automatic retry was made.')
    return 0


def control(args, connection: dict) -> int:
    if not args.yes:
        raise core.SendError('Job controls require --yes and the exact --job name from status.')
    with core.printer_lock(connection) as (cache, key):
        status = live_status(connection)
        control_preflight(status, args.command, args.job)
        sequence = str(20000 + time.time_ns() % 9998)
        wire = 'stop' if args.command == 'cancel' else args.command
        payload = {'print': {'command': wire, 'param': '', 'sequence_id': sequence}}
        receipt = cache / f'{key}-control-{time.time_ns()}.json'
        result = {'command': args.command, 'job': args.job, 'attempted': True, 'confirmed': False}
        core.write_private(receipt, result)
        events, rc = core.bridge_run(connection, 'control', {
            'control': wire, 'job_name': args.job, 'sequence_id': sequence,
            'payload': json.dumps(payload, separators=(',', ':'))})
        result['confirmed'] = rc == 0 and confirmed_control(events, args.command, sequence, args.job)
        result['status'] = snapshot(core.merged_status(events)) if any(e.get('gcode_state') for e in events) else {}
        core.write_private(receipt, result)
        output(args, result, f"{args.command.capitalize()}: {'confirmed' if result['confirmed'] else 'UNCONFIRMED'} for {quoted(args.job)}.")
        if not result['confirmed']:
            raise core.SendError('Control outcome is uncertain or rejected. Check status before issuing another command; no automatic retry was made.')
    return 0


def legacy_print(args) -> int:
    # Keep the established print path and duplicate-submission receipts intact.
    with tempfile.TemporaryDirectory(prefix='bambu-result-') as directory:
        report = args.report or Path(directory) / 'result.json'
        argv = [str(args.file), '--host', args.host, '--tray', str(args.tray),
                '--plate', str(args.plate), '--report', str(report)]
        if args.serial:
            argv += ['--serial', args.serial]
        if args.command == 'print':
            argv += ['--start']
        if args.repeat:
            argv += ['--repeat']
        if args.no_timelapse:
            argv += ['--no-timelapse']
        with contextlib.redirect_stdout(sys.stderr if args.json else sys.stdout):
            rc = core.main(argv)
        if args.json:
            output(args, json.loads(report.read_text()))
        return rc


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version', action='version', version=f'bambu-headless {VERSION}')
    sub = parser.add_subparsers(dest='command', required=True)

    def options(p, connection=True):
        if connection:
            p.add_argument('--host', default=os.environ.get('BAMBU_HOST'), help='LAN address (or BAMBU_HOST)')
            p.add_argument('--serial', default=os.environ.get('BAMBU_SERIAL'), help='Saved printer serial (or BAMBU_SERIAL)')
        p.add_argument('--json', action='store_true', help='Machine-readable JSON; watch emits JSON Lines')
        p.add_argument('--report', type=Path, help='Save the latest result as a private JSON file')

    options(sub.add_parser('printers', help='List saved Studio printers, without access codes'), False)
    inspect = sub.add_parser('inspect', help='Validate a sliced file without contacting a printer')
    options(inspect, False)
    inspect.add_argument('file', type=Path)
    inspect.add_argument('--plate', type=int, default=1)
    options(sub.add_parser('status', help='Read live status, temperatures, AMS and nozzle information'))
    watch = sub.add_parser('watch', help='Follow live job progress without changing the printer')
    options(watch)
    watch.add_argument('--interval', type=float, default=5, help='Seconds between status requests (minimum 1)')
    watch.add_argument('--timeout', type=float, default=3600, help='Monitoring deadline in seconds; 0 = unlimited')
    watch.add_argument('--count', type=int, default=0, help='Stop after this many snapshots; 0 = unlimited')
    watch.add_argument('--job', help='Require this exact job name throughout monitoring')
    watch.add_argument('--until-done', action='store_true', help='Follow one job; exit 0 only on FINISH')
    for name in ('check', 'print'):
        p = sub.add_parser(name, help='Check file/printer compatibility' if name == 'check' else 'Upload and start one print')
        options(p)
        p.add_argument('file', type=Path)
        p.add_argument('--plate', type=int, default=1)
        p.add_argument('--tray', type=int, default=1)
        p.add_argument('--repeat', action='store_true', help='Intentionally reprint a file with an existing receipt')
        p.add_argument('--no-timelapse', action='store_true', help='Skip the printer camera timelapse for this job')
    upload = sub.add_parser('upload', help='Upload and verify a sliced file without starting it')
    options(upload)
    upload.add_argument('file', type=Path)
    upload.add_argument('--plate', type=int, default=1)
    upload.add_argument('--name', help='Remote path; defaults to the file basename in the printer root')
    group = sub.add_parser('files', help='List, download, rename or delete files on the printer')
    verbs = group.add_subparsers(dest='file_command', required=True)
    listing = verbs.add_parser('list', help='List a printer directory')
    options(listing)
    listing.add_argument('--directory', default='/')
    for name in ('download', 'rename', 'delete'):
        p = verbs.add_parser(name)
        options(p)
        p.add_argument('source', help='Remote path')
        if name in ('download', 'rename'):
            p.add_argument('destination', type=Path if name == 'download' else str)
        if name == 'delete':
            p.add_argument('--yes', action='store_true', help='Confirm deletion of this exact sliced file')
    for name in CONTROL_STATES:
        p = sub.add_parser(name, help=f'{name.capitalize()} the named current job (requires --yes)')
        options(p)
        p.add_argument('--job', required=True, help='Exact job name shown by status')
        p.add_argument('--yes', action='store_true', help='Explicitly send the control command')
    bed = sub.add_parser('bed-temp', help='Set target bed temperature in Celsius (0..120)')
    options(bed)
    bed.add_argument('temperature', type=int, help='Target bed temperature in Celsius (0..120)')
    bed.add_argument('--job', help='Exact job name if setting bed temperature during an active job')
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == 'printers':
        result = {'printers': core.saved_printers(), 'source': 'Bambu Studio saved configuration'}
        output(args, result, '\n'.join(quoted(p['serial']) + ' | access code saved' for p in result['printers']) or 'No saved printers.')
        return 0
    if args.command == 'inspect':
        output(args, core.inspect_file(args.file.expanduser(), args.plate))
        return 0
    if not args.host:
        parser.error('Set --host or BAMBU_HOST. Saved-printer discovery does not infer LAN addresses.')
    if args.command == 'watch':
        if not 1 <= args.interval <= 3600 or not 0 <= args.timeout <= 86400 or args.count < 0:
            parser.error('Use interval 1..3600, timeout 0..86400 and a nonnegative count.')
    if args.command == 'bed-temp' and not 0 <= args.temperature <= 120:
        raise core.SendError('Bed temperature must be between 0 and 120 C.')
    if args.command in ('check', 'print'):
        return legacy_print(args)
    if args.command in CONTROL_STATES and not args.yes:
        raise core.SendError('Job controls require --yes and the exact --job name from status.')
    if args.command == 'files' and args.file_command == 'delete' and not args.yes:
        raise core.SendError('Delete requires --yes and one exact remote .gcode.3mf path.')
    connection = core.load_connection(args.host, args.serial)
    if args.command == 'watch':
        return monitor(args, connection)
    if args.command == 'bed-temp':
        return set_bed_temp(args, connection)
    if args.command in CONTROL_STATES:
        return control(args, connection)
    with core.printer_lock(connection):
        if args.command == 'status':
            result = snapshot(live_status(connection))
            output(args, result, status_line(result) + '\nAMS: ' + json.dumps(result.get('ams_trays', [])) + '\nNozzles: ' + json.dumps(result.get('nozzles', [])))
        elif args.command == 'upload':
            path = args.file.expanduser().resolve()
            info = core.inspect_file(path, args.plate)
            destination = files.print_file_path(args.name or path.name)
            idle(live_status(connection))
            result = files.upload(connection, path, info, destination)
            output(args, result, f"{'Reused' if result['reused'] else 'Uploaded'} {quoted(destination)}; full read-back verified. No print started.")
        elif args.command == 'files':
            if args.file_command == 'list':
                result = {'files': files.list_files(connection, args.directory)}
                output(args, result, '\n'.join(f"{f['type']:8} {str(f['bytes']):>10}  {quoted(f['path'])}" for f in result['files']) or 'Directory is empty.')
            elif args.file_command == 'download':
                output(args, files.download(connection, args.source, args.destination.expanduser()))
            else:
                source = files.print_file_path(args.source)
                if args.file_command == 'rename':
                    destination = files.print_file_path(args.destination)
                idle(live_status(connection))
                result = files.rename(connection, source, destination) if args.file_command == 'rename' else files.delete(connection, source)
                output(args, result)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except core.SendError as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print('Interrupted. Check printer state before retrying any command already dispatched.', file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        print(f'ERROR: {type(exc).__name__}; details suppressed to protect credentials.', file=sys.stderr)
        raise SystemExit(2)
