#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Upload and start a single-material H2D/P1S sliced 3MF headlessly.

Default: validate the file and read fresh printer status only. --start explicitly
uploads and submits once. Uses the installed, unmodified Bambu networking plugin
with a process-local executable-path compatibility shim in the helper only.
Access codes stay in memory and the private subprocess stdin pipe. No user token
is extracted, logged, or passed on the command line. The official plugin manages
its own existing BambuNetworkEngine login/certificate cache.
"""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import ftplib
import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
import zipfile

HERE = Path(__file__).resolve().parent
STUDIO = Path('/Applications/BambuStudio.app')
IS_LINUX = sys.platform.startswith('linux')
CONFIG = Path(os.environ.get('BAMBU_CONFIG_DIR', str(
    Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'BambuStudio'
    if IS_LINUX else Path.home() / 'Library/Application Support/BambuStudio')))
CERT_DIR = Path(os.environ.get('BAMBU_CERT_DIR', '/opt/bambu-studio/resources/cert')) if IS_LINUX else STUDIO / 'Contents/Resources/cert'
STUDIO_EXECUTABLE = os.environ.get('BAMBU_STUDIO_EXECUTABLE', '/opt/bambu-studio/bin/bambu-studio') if IS_LINUX else str(STUDIO / 'Contents/MacOS/BambuStudio')
IDLE_STATES = {'IDLE', 'FINISH', 'FAILED'}
MODEL_IDS = {'Bambu Lab H2D': 'O1D', 'Bambu Lab P1S': 'C12'}

class SendError(Exception):
    pass

class PrinterBusy(SendError):
    pass

def inspect_file(path: Path, plate: int = 1) -> dict:
    if not path.name.endswith('.gcode.3mf'):
        raise SendError('Select a sliced .gcode.3mf, not an editable project.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+', path.name):
        raise SendError('Use a filename containing only letters, digits, dots, hyphens and underscores.')
    if not path.is_file() or not 0 < path.stat().st_size < 1024**3:
        raise SendError('Print file missing, empty, or larger than the supported 1 GiB limit.')
    content = path.read_bytes()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            if z.testzip():
                raise SendError('3MF archive integrity check failed.')
            settings = json.loads(z.read('Metadata/project_settings.config'))
            gcode_path = f'Metadata/plate_{plate}.gcode'
            gcode = z.read(gcode_path)
            expected = z.read(gcode_path + '.md5').decode().strip().lower()
            if hashlib.md5(gcode).hexdigest() != expected:
                raise SendError('Embedded G-code checksum does not match.')
            slices = ET.fromstring(z.read('Metadata/slice_info.config'))
            plates = slices.findall('plate')
            selected = [p for p in plates if any(m.get('key') == 'index' and m.get('value') == str(plate) for m in p.findall('metadata'))]
            if len(selected) != 1:
                raise SendError('Requested plate is missing or ambiguous.')
            p = selected[0]
            metadata = {m.get('key'): m.get('value') for m in p.findall('metadata')}
            filaments = p.findall('filament')
            if len(filaments) != 1 or filaments[0].get('id') != '1':
                raise SendError('This sender supports one physical filament (slot 1) per plate.')
            model = settings.get('printer_model')
            if model not in MODEL_IDS:
                raise SendError('The sliced file must target Bambu Lab H2D or P1S.')
            if metadata.get('printer_model_id', MODEL_IDS[model]) != MODEL_IDS[model]:
                raise SendError('Plate and project printer models disagree.')
            if model == 'Bambu Lab H2D':
                if metadata.get('filament_maps') != '2':
                    raise SendError('This sender supports the right H2D nozzle only; recheck the sliced nozzle mapping.')
            elif not metadata.get('filament_maps') or set(metadata['filament_maps'].split()) != {'1'}:
                raise SendError('P1S slices must map all filament presets to the single extruder.')
            nozzle = filaments[0].get('nozzle_diameter', '')
            if float(nozzle) != 0.4:
                raise SendError('This sender supports a 0.4 mm nozzle only (right nozzle on H2D).')
            material = filaments[0].get('type')
            bed_type = settings.get('curr_bed_type')
            bed_types = {'High Temp Plate': 'hot_plate', 'Textured PEI Plate': 'textured_plate', 'Cool Plate': 'cool_plate', 'Engineering Plate': 'eng_plate', 'SuperTack Plate': 'supertack_plate'}
            if bed_type not in bed_types:
                raise SendError('Unsupported plate type; do not guess the printer plate code.')
            info = dict(filename=path.name, bytes=len(content), md5=hashlib.md5(content).hexdigest(),
                        gcode_md5=expected, plate=plate, param=gcode_path, material=material,
                        color=filaments[0].get('color'), printer_model=model, model_id=MODEL_IDS[model], nozzle_mm=0.4,
                        bed_type=bed_types[bed_type], layer_height=settings.get('layer_height'),
                        estimated_seconds=int(metadata.get('prediction', 0)), grams=float(metadata.get('weight', 0)))
            if model == 'Bambu Lab H2D':
                info['right_nozzle_mm'] = 0.4  # Preserve the existing H2D report field.
            return info
    except (KeyError, ValueError, ET.ParseError, zipfile.BadZipFile) as e:
        raise SendError(f'Invalid or unsupported sliced 3MF ({type(e).__name__}).') from None

def ensure_bridge() -> Path:
    binary = HERE / 'h2d_network_bridge'
    source = HERE / 'h2d_network_bridge.cpp'
    header = HERE / 'vendor/bambu_networking.hpp'
    if not header.exists():
        raise SendError('Pinned public ABI header is missing.')
    if not binary.exists() or binary.stat().st_mtime < max(source.stat().st_mtime, header.stat().st_mtime):
        command = ['g++' if IS_LINUX else 'clang++', '-std=c++17', '-O2',
                   str(source), '-o', str(binary), '-pthread']
        command += ['-ldl'] if IS_LINUX else ['-I', '/opt/homebrew/include', '-I', '/usr/local/include']
        result = subprocess.run(command,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=90)
        if result.returncode:
            raise SendError('Could not compile the native adapter; install the platform compiler and '
                            'nlohmann-json headers (macOS: brew install nlohmann-json; '
                            'Debian/Ubuntu: apt install nlohmann-json3-dev).')
    shim_source = HERE / ('h2d_host_identity_linux.c' if IS_LINUX else 'h2d_host_identity.c')
    shim = HERE / ('h2d_host_identity.so' if IS_LINUX else 'h2d_host_identity.dylib')
    if not shim.exists() or shim.stat().st_mtime < shim_source.stat().st_mtime:
        command = (['gcc', '-shared', '-fPIC', str(shim_source), '-o', str(shim), '-ldl']
                   if IS_LINUX else ['clang', '-dynamiclib', str(shim_source), '-o', str(shim)])
        result = subprocess.run(command,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if result.returncode:
            raise SendError('Could not compile the process-local compatibility shim.')
    return binary

def load_connection(host: str, serial: str | None) -> dict:
    try:
        conf = json.loads((CONFIG / 'BambuStudio.conf').read_text())
        codes = dict(conf.get('user_access_code', {}))
        codes.update({k: v for k, v in conf.get('access_code', {}).items() if v})
        codes = {k: v for k, v in codes.items() if v}
        if serial is None:
            if len(codes) != 1:
                raise SendError('Multiple or no saved printers; select one with --serial.')
            serial = next(iter(codes))
        code = codes.get(serial) or conf.get('user_access_code', {}).get(serial)
        if not code:
            raise SendError('The selected printer has no saved Bambu Studio access code.')
        region = conf.get('app', {}).get('region', '')
        if region not in ('North America', 'US', 'United States', ''):
            raise SendError('This sender is validated for the US cloud region only.')
        return dict(serial=serial, ip=host, access_code=code, country='US',
                    plugin=str(CONFIG / ('plugins/libbambu_networking.so' if IS_LINUX else 'plugins/libbambu_networking.dylib')),
                    config_dir=str(CONFIG), cert_dir=str(CERT_DIR))
    except (OSError, ValueError):
        raise SendError('Cannot read the saved Bambu Studio connection configuration.') from None

def saved_printers() -> list[dict]:
    """List saved identities, never their access-code values or account data."""
    try:
        conf = json.loads((CONFIG / 'BambuStudio.conf').read_text())
        codes = dict(conf.get('user_access_code', {}))
        codes.update({k: v for k, v in conf.get('access_code', {}).items() if v})
        return [{'serial': serial, 'access_code_saved': True}
                for serial in sorted(codes) if codes[serial]]
    except (OSError, ValueError, TypeError, AttributeError):
        raise SendError('Cannot read saved printers from Bambu Studio.') from None

@contextlib.contextmanager
def printer_lock(connection: dict):
    cache = (Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache')) / 'h2d-headless'
             if IS_LINUX else Path.home() / 'Library/Caches/h2d-headless')
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(connection['serial'].encode()).hexdigest()[:20]
    with (cache / (key + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PrinterBusy('Another headless sender is using this printer.') from None
        yield cache, key

def bridge_run(connection: dict, action: str, job: dict | None = None) -> tuple[list[dict], int]:
    binary = ensure_bridge()
    with tempfile.TemporaryDirectory(prefix='h2d-headless-') as scratch:
        config = dict(connection, action=action, scratch=scratch)
        if job:
            config.update(job)
        config.setdefault('sequence_id', str(20000 + time.time_ns() % 9998))
        env = {k: v for k, v in os.environ.items() if not k.startswith(('DYLD_', 'LD_PRELOAD', 'H2D_STUDIO_'))}
        env['LD_PRELOAD' if IS_LINUX else 'DYLD_INSERT_LIBRARIES'] = str(
            HERE / ('h2d_host_identity.so' if IS_LINUX else 'h2d_host_identity.dylib'))
        env['H2D_STUDIO_EXECUTABLE'] = STUDIO_EXECUTABLE
        try:
            result = subprocess.run([str(binary)], input=json.dumps(config), text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=95, env=env)
        except subprocess.TimeoutExpired:
            raise SendError('Adapter timed out. If a command was sent, outcome is uncertain: check the printer before any retry.') from None
        events = []
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
                if isinstance(event, dict) and event.get('event'):
                    events.append(event)
            except ValueError:
                continue  # Never echo uncontrolled vendor output.
        return events, result.returncode

def merged_status(events: list[dict]) -> dict:
    status = {}
    for event in events:
        if event.get('event') == 'printer_identity':
            status['model_id'] = event.get('model_id')
        if event.get('command') == 'push_status':
            status.update(event)
    if not status.get('gcode_state'):
        raise SendError('No fresh MQTT printer status was received.')
    return status

def preflight(status: dict, info: dict, tray: int) -> None:
    if status.get('gcode_state') not in IDLE_STATES:
        raise SendError(f'Printer is {status.get("gcode_state", "unknown")}; refusing to interrupt an active job.')
    if status.get('model_id') != info['model_id']:
        raise SendError('Live printer model is unknown or does not match the sliced file.')
    matching = [t for t in status.get('trays', []) if str(t.get('ams_id')) == str(tray // 4) and str(t.get('id')) == str(tray % 4)]
    if len(matching) != 1 or matching[0].get('tray_type') != info['material']:
        raise SendError('Selected AMS tray is missing or its material differs from the sliced file.')
    if info['printer_model'] == 'Bambu Lab P1S':
        diameter = status.get('nozzle_diameter')
    else:
        nozzles = [n for n in status.get('nozzles', []) if n.get('id') == 0]
        diameter = nozzles[0].get('diameter') if len(nozzles) == 1 else None
    try:
        matches = float(diameter) == info['nozzle_mm']
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise SendError('Live nozzle diameter is unknown or does not match the sliced file.')

def print_payload(info: dict, tray: int, sequence: str, timelapse: bool = True) -> dict:
    # Right logical nozzle is already baked into this slice (filament_maps=2).
    # H2D hardware nozzle id 0 is right; AMS 0/slot 1 is A2.
    payload = {'print': dict(command='project_file', sequence_id=sequence, param=info['param'],
                          project_id='0', profile_id='0', task_id='0', subtask_id='0',
                          subtask_name=info['filename'][:-4], url='ftp://' + info['filename'],
                          md5='from_sd_card', timelapse=timelapse, bed_type=info['bed_type'],
                          bed_leveling=True, auto_bed_leveling=2, flow_cali=False,
                          vibration_cali=True, layer_inspect=True, use_ams=True,
                          ams_mapping=[tray], ams_mapping2=[{'ams_id': tray // 4, 'slot_id': tray % 4}],
                          nozzle_mapping=[0], nozzle_offset_cali=0, extrude_cali_flag=0,
                          extrude_cali_manual_mode=-1)}
    if info['printer_model'] == 'Bambu Lab P1S':
        p = payload['print']
        for key in ('auto_bed_leveling', 'ams_mapping2', 'nozzle_mapping', 'nozzle_offset_cali',
                    'extrude_cali_flag', 'extrude_cali_manual_mode'):
            p.pop(key)
        p.update(url='ftp:///' + info['filename'], md5=info['md5'], layer_inspect=False)
    return payload

class ImplicitFTPS(ftplib.FTP_TLS):
    def __init__(self, host: str, serial: str):
        context = ssl.create_default_context(cafile=str(CERT_DIR / 'printer.cer'))
        super().__init__(context=context, timeout=20)
        self.host = serial  # Verify the printer certificate's serial-number CN.
        self.sock = context.wrap_socket(socket.create_connection((host, 990), timeout=20), server_hostname=serial)
        self.af = self.sock.family
        self.file = self.sock.makefile('r', encoding=self.encoding)
        self.welcome = self.getresp()
        self._connect_host = host
    def makepasv(self):
        _, port = super().makepasv()
        return self._connect_host, port
    def ntransfercmd(self, cmd, rest=None):
        # H2D requires TLS session reuse on the FTPS data connection (522
        # otherwise). Python FTP_TLS does not pass the control session.
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
        return conn, size
    def storbinary(self, cmd, fp, blocksize=8192, callback=None, rest=None):
        # P1S (Studio's 01P serial family) can withhold TLS close_notify after
        # STOR. Close the encrypted data socket, then require the FTP completion
        # reply. Callers still verify the complete file by reading it back.
        if not self.host.startswith('01P'):
            return super().storbinary(cmd, fp, blocksize, callback, rest)
        self.voidcmd('TYPE I')
        with self.transfercmd(cmd, rest) as conn:
            while data := fp.read(blocksize):
                conn.sendall(data)
                if callback:
                    callback(data)
        return self.voidresp()

def upload_verified(path: Path, info: dict, connection: dict) -> str:
    try:
        with ImplicitFTPS(connection['ip'], connection['serial']) as ftp:
            ftp.login('bblp', connection['access_code'])
            ftp.prot_p()
            ftp.voidcmd('TYPE I')
            def remote_hash() -> str:
                digest = hashlib.md5()
                ftp.retrbinary('RETR /' + path.name, digest.update)
                return digest.hexdigest()
            try:
                if ftp.size('/' + path.name) == info['bytes'] and remote_hash() == info['md5']:
                    return 'existing file verified byte-for-byte'
                raise SendError('Remote destination exists with different contents; choose a new filename. Print was not submitted.')
            except ftplib.error_perm as e:
                if not str(e).startswith('550'):
                    raise
            with path.open('rb') as source:
                ftp.storbinary('STOR /' + path.name, source)
            if ftp.size('/' + path.name) != info['bytes'] or remote_hash() != info['md5']:
                raise SendError('Uploaded file checksum mismatch; print was not submitted.')
            return 'uploaded and verified byte-for-byte'
    except SendError:
        raise
    except Exception as e:
        # FTP/TLS exceptions can include connection details; omit raw contents.
        raise SendError(f'FTPS upload/read-back failed ({type(e).__name__}); print was not submitted.') from None

def verified_start(events: list[dict], name: str) -> dict | None:
    job = None
    state = None
    for e in events:
        if e.get('command') != 'push_status':
            continue
        job = e.get('subtask_name', job)
        state = e.get('gcode_state', state)
        if job == name and state in {'PREPARE', 'RUNNING'}:
            return {k: e[k] for k in ('gcode_state', 'stg_cur', 'layer_num', 'mc_percent', 'mc_remaining_time') if k in e} | {'job': job, 'gcode_state': state}
    return None

def write_private(path: Path, value: dict) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump(value, out, indent=2)
        out.write('\n')

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('file', type=Path)
    parser.add_argument('--host', required=True, help='Printer LAN address; credentials come from Bambu Studio')
    parser.add_argument('--serial', help='Defaults to the sole printer saved in Bambu Studio')
    parser.add_argument('--tray', type=int, default=1, help='Zero-based AMS tray; 1 = A2 (default)')
    parser.add_argument('--plate', type=int, default=1)
    parser.add_argument('--start', action='store_true', help='Upload and submit one print after preflight')
    parser.add_argument('--repeat', action='store_true', help='Explicitly permit another attempt for this file after checking prior outcome')
    parser.add_argument('--sign-probe', action='store_true', help='Read-only calibration probe; does not prove print authorization')
    parser.add_argument('--no-timelapse', action='store_true', help='Skip the printer camera timelapse for this job')
    parser.add_argument('--report', type=Path, help='Save a sanitized verification report (no access codes, tokens, serial or host)')
    args = parser.parse_args(argv)
    if not 0 <= args.tray < 16:
        raise SendError('AMS tray must be between 0 and 15.')
    path = args.file.expanduser().resolve()
    info = inspect_file(path, args.plate)
    connection = load_connection(args.host, args.serial)
    # Serialize all runs for this printer, including the final fresh-status check.
    with printer_lock(connection) as (cache, printer_key):
        return run_locked(args, path, info, connection, cache, printer_key)

def run_locked(args, path, info, connection, cache, printer_key) -> int:
    report = {'file': info, 'ams_tray': args.tray, 'timelapse': not args.no_timelapse,
              'gui_used': False, 'helper_path_shim': True, 'started': False}
    print(f"File checked: {info['filename']} | {info['printer_model']} | {info['material']} | 0.4 mm | AMS tray {args.tray}", flush=True)
    events, rc = bridge_run(connection, 'status', {'expected_model': info['model_id']})
    if rc:
        raise SendError('Live printer connection failed; no print was submitted.')
    status = merged_status(events)
    preflight(status, info, args.tray)
    report['preflight'] = {k: status[k] for k in ('gcode_state', 'subtask_name', 'model_id', 'trays', 'nozzles', 'nozzle_diameter') if k in status}
    print(f"Live preflight passed: {status['gcode_state']}; AMS material and nozzle match.", flush=True)
    if args.sign_probe and not args.start:
        events, rc = bridge_run(connection, 'sign_probe')
        report['read_only_probe_passed'] = rc == 0 and any(e.get('event') == 'verification' and e.get('confirmed') for e in events)
        if not report['read_only_probe_passed']:
            raise SendError('Read-only calibration request failed; no print was submitted.')
        print('Read-only calibration request accepted; print authorization remains untested.', flush=True)
    if args.start:
        receipt_path = cache / f'{printer_key}-{info["md5"]}.json'
        if receipt_path.exists() and not args.repeat:
            raise SendError('This file already has a submission receipt. Check the printer; use --repeat only to intentionally submit it again.')
        print('Verifying printer storage over certificate-validated FTPS...', flush=True)
        report['upload'] = upload_verified(path, info, connection)
        print('Storage: ' + report['upload'] + '.', flush=True)
        sequence = str(20000 + time.time_ns() % 9998)
        payload = print_payload(info, args.tray, sequence, timelapse=report['timelapse'])
        job_name = payload['print']['subtask_name']
        # Persist BEFORE dispatch. Unknown outcomes must never silently resubmit.
        report.update(submission_attempted=True, submitted_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        write_private(receipt_path, report)
        print('Submitting once through the official networking plugin; waiting for matching PREPARE/RUNNING...', flush=True)
        events, rc = bridge_run(connection, 'start', {'sequence_id': sequence, 'job_name': job_name,
            'expected_model': info['model_id'], 'payload': json.dumps(payload, separators=(',', ':'))})
        report['events'] = events
        confirmation = verified_start(events, job_name)
        report['started'] = rc == 0 and confirmation is not None
        report['confirmation'] = confirmation
        report['adapter_exit'] = rc
        write_private(receipt_path, report)
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            write_private(args.report, report)
        if not report['started']:
            reasons = [{k: e[k] for k in ('event', 'message', 'rc', 'command', 'err_code', 'result', 'reason') if k in e} for e in events if e.get('event') in ('error', 'send_result', 'verification') or e.get('command') == 'project_file']
            print(json.dumps({'start_unconfirmed': reasons}), flush=True)
            raise SendError('Start was not confirmed. A receipt blocks automatic retry; inspect printer state before deciding what to do next.')
        print('CONFIRMED: ' + json.dumps(confirmation), flush=True)
    else:
        print('Check only; add --start to upload and print.', flush=True)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        write_private(args.report, report)
    return 0

if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SendError as e:
        print('ERROR: ' + str(e), file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print('Interrupted. If submission began, check printer state before retrying.', file=sys.stderr)
        raise SystemExit(130)
    except Exception as e:
        print(f'ERROR: {type(e).__name__}; details suppressed to protect credentials. Check printer state before retrying.', file=sys.stderr)
        raise SystemExit(2)
