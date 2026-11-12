#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-only
"""Upload and start an H2D/P1S sliced 3MF headlessly.

A single-material plate uses --tray. A plate with several filaments needs --ams-map,
one AMS slot per filament in filament order (for example B3,B1,B2,A4); every slot is
checked live for material, colour and the nozzle its AMS feeds before anything starts.

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
            multi = len(filaments) > 1
            if not filaments or (not multi and filaments[0].get('id') != '1'):
                raise SendError('This sender supports one physical filament (slot 1) per plate, or several with --ams-map.')
            model = settings.get('printer_model')
            if model not in MODEL_IDS:
                raise SendError('The sliced file must target Bambu Lab H2D or P1S.')
            # The Studio CLI writes an empty plate printer_model_id; only a recorded, different model is a disagreement.
            if (metadata.get('printer_model_id') or MODEL_IDS[model]) != MODEL_IDS[model]:
                raise SendError('Plate and project printer models disagree.')
            maps = (metadata.get('filament_maps') or '').split()
            if model == 'Bambu Lab H2D':
                fmap = metadata.get('filament_maps')
                if multi:
                    if not maps or any(m not in ('1', '2') for m in maps):
                        raise SendError('Every H2D filament must map to the left (1) or right (2) nozzle; recheck the sliced nozzle mapping.')
                    hw_nozzle_id = None
                elif fmap not in ('1', '2'):
                    raise SendError('This sender supports a single H2D nozzle (left=1 or right=2); recheck the sliced nozzle mapping.')
                else:
                    hw_nozzle_id = 0 if fmap == '2' else 1
            elif not maps or set(maps) != {'1'}:
                raise SendError('P1S slices must map all filament presets to the single extruder.')
            else:
                fmap = '1'
                hw_nozzle_id = 0
            filament_list = []
            for f in filaments:
                fid = int(f.get('id', ''))
                if float(f.get('nozzle_diameter', '')) != 0.4:
                    raise SendError('This sender supports a 0.4 mm nozzle only.')
                if not multi:
                    hw = hw_nozzle_id
                elif model == 'Bambu Lab H2D':
                    if not 1 <= fid <= len(maps):
                        raise SendError('A filament has no nozzle in the sliced filament map.')
                    hw = 0 if maps[fid - 1] == '2' else 1
                else:
                    hw = 0
                filament_list.append(dict(id=fid, type=f.get('type'), tray_info_idx=f.get('tray_info_idx'),
                                          color=f.get('color'), grams=float(f.get('used_g') or 0), hw_nozzle_id=hw))
            if len({f['id'] for f in filament_list}) != len(filament_list):
                raise SendError('The plate lists the same filament twice.')
            nozzle = filaments[0].get('nozzle_diameter', '')
            if float(nozzle) != 0.4:
                raise SendError('This sender supports a 0.4 mm nozzle only.')
            material = filaments[0].get('type') if not multi else 'MULTI'
            bed_type = settings.get('curr_bed_type')
            bed_types = {'High Temp Plate': 'hot_plate', 'Textured PEI Plate': 'textured_plate', 'Cool Plate': 'cool_plate', 'Engineering Plate': 'eng_plate', 'SuperTack Plate': 'supertack_plate'}
            if bed_type not in bed_types:
                raise SendError('Unsupported plate type; do not guess the printer plate code.')
            info = dict(filename=path.name, bytes=len(content), md5=hashlib.md5(content).hexdigest(),
                        gcode_md5=expected, plate=plate, param=gcode_path, material=material,
                        tray_info_idx=None if multi else filaments[0].get('tray_info_idx'),
                        color=None if multi else filaments[0].get('color'), printer_model=model, model_id=MODEL_IDS[model],
                        filament_maps=fmap, hw_nozzle_id=hw_nozzle_id, nozzle_mm=0.4,
                        bed_type=bed_types[bed_type], layer_height=settings.get('layer_height'),
                        estimated_seconds=int(metadata.get('prediction', 0)), grams=float(metadata.get('weight', 0)))
            if multi:
                # Mapping arrays cover every project filament (Studio: one entry per filament preset).
                info.update(filaments=filament_list, filament_count=max(len(maps), max(f['id'] for f in filament_list)),
                            filament_nozzles=[0 if m == '2' else 1 for m in maps] if model == 'Bambu Lab H2D' else [0] * len(maps))
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
                    slicer_uid=conf.get('app', {}).get('slicer_uuid', ''),
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
    if status.get('model_id') != info.get('model_id', 'O1D'):
        raise SendError('Live printer model is unknown or does not match the sliced file.')
    hw_nozzle_id = int(info.get('hw_nozzle_id', 0))
    if info.get('printer_model', 'Bambu Lab H2D') == 'Bambu Lab H2D':
        if tray == 255:
            expected_hw_nozzle = 0
        elif tray == 254:
            expected_hw_nozzle = 1
        else:
            expected_hw_nozzle = 0 if (tray // 4) == 0 else 1
        if hw_nozzle_id != expected_hw_nozzle:
            slot_desc = "External spool" if tray in (254, 255) else "Selected AMS tray"
            raise SendError(f'{slot_desc} feeds a different H2D nozzle than the sliced file.')
    matching = live_tray(status, tray)
    slot_desc = "external spool" if tray in (254, 255) else "AMS tray"
    if not matching or matching.get('tray_type') != info['material']:
        raise SendError(f'Selected {slot_desc} is missing or its material differs from the sliced file.')
    if info.get('printer_model') == 'Bambu Lab P1S':
        diameter = status.get('nozzle_diameter')
    else:
        nozzles = [n for n in status.get('nozzles', []) if n.get('id') == hw_nozzle_id]
        diameter = nozzles[0].get('diameter') if len(nozzles) == 1 else None
    try:
        matches = float(diameter) == float(info.get('nozzle_mm', info.get('right_nozzle_mm', 0.4)))
    except (TypeError, ValueError):
        matches = False
    if not matches:
        raise SendError('Live nozzle diameter is unknown or does not match the sliced file.')

def rgba_hex(color: str | None, fallback: str = 'FFFFFFFF') -> str:
    raw = (color or '').strip().lstrip('#').upper()
    if re.fullmatch(r'[0-9A-F]{6}', raw):
        return raw + 'FF'
    if re.fullmatch(r'[0-9A-F]{8}', raw):
        return raw
    return fallback

def create_config_3mf(source_3mf: Path, dst_3mf: Path) -> Path:
    """Create the lightweight config 3MF (thumbnails + slice metadata, no G-code or mesh objects)."""
    with zipfile.ZipFile(source_3mf, 'r') as src, zipfile.ZipFile(dst_3mf, 'w', compression=zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            name = item.filename
            if (name.startswith('3D/Objects/') or name.startswith('Auxiliaries/') or
                    (name.startswith('Metadata/') and (name.endswith('.gcode') or name.endswith('.gcode.md5')))):
                continue
            dst.writestr(item, src.read(name))
    return dst_3mf

EXTERNAL_SLOTS = {254, 255}

def parse_tray_spec(spec: str | int, hw_nozzle_id: int | None = None, is_h2d: bool = True) -> int:
    s = str(spec).strip().upper()
    if s in ('EXTERNAL', 'EXT', 'NO-AMS', 'NO_AMS'):
        if is_h2d:
            return 255 if hw_nozzle_id == 0 else 254
        return 254
    if s in ('EXT-L', 'EXT_L', 'EXT1', 'LEFT', 'EXT-LEFT'):
        return 254
    if s in ('EXT-R', 'EXT_R', 'EXT2', 'RIGHT', 'EXT-RIGHT'):
        return 255
    if re.fullmatch(r'[A-D][1-4]', s):
        return 'ABCD'.index(s[0]) * 4 + int(s[1]) - 1
    try:
        val = int(s)
        if 0 <= val < 16 or val in EXTERNAL_SLOTS:
            return val
    except ValueError:
        pass
    raise SendError(f'Invalid tray {spec!r}; use 0..15, A1..D4, or external (ext, ext-l/254, ext-r/255).')

def slot_label(tray: int, is_h2d: bool = True) -> str:
    if tray == 254:
        return 'Ext-L' if is_h2d else 'Ext'
    if tray == 255:
        return 'Ext-R'
    if 0 <= tray < 16:
        return f"{'ABCD'[tray // 4]}{tray % 4 + 1}"
    return f"Tray-{tray}"

def parse_ams_map(spec: str, filaments: list[dict] | None = None, is_h2d: bool = True) -> list[int]:
    """'B3,B1,A4' or '6,4,3' or 'ext,A1' -> tray IDs, one per plate filament in filament order."""
    if not (spec or '').strip():
        raise SendError('Invalid AMS slot in --ams-map; specification is empty.')
    trays = []
    for idx, token in enumerate((spec or '').split(',')):
        t = token.strip()
        hw = filaments[idx].get('hw_nozzle_id') if filaments and idx < len(filaments) else None
        try:
            tray = parse_tray_spec(t, hw_nozzle_id=hw, is_h2d=is_h2d)
            trays.append(tray)
        except SendError:
            raise SendError(f'Invalid AMS slot {token.strip()!r} in --ams-map; use A1..D4 or 0..15.') from None
    if len(set(trays)) != len(trays):
        raise SendError('--ams-map repeats a slot; each filament needs its own AMS slot.')
    return trays

def live_tray(status: dict, tray: int) -> dict:
    if tray in EXTERNAL_SLOTS:
        match = [t for t in status.get('trays', []) if str(t.get('id')) == str(tray)]
    else:
        match = [t for t in status.get('trays', []) if str(t.get('ams_id')) == str(tray // 4) and str(t.get('id')) == str(tray % 4)]
    return match[0] if len(match) == 1 else {}

def preflight_multi(status: dict, info: dict, trays: list[int], allow_color_mismatch: bool = False) -> None:
    """Every filament's slot must hold its material and colour, on the AMS that feeds its nozzle."""
    if status.get('gcode_state') not in IDLE_STATES:
        raise SendError(f'Printer is {status.get("gcode_state", "unknown")}; refusing to interrupt an active job.')
    if status.get('model_id') != info.get('model_id', 'O1D'):
        raise SendError('Live printer model is unknown or does not match the sliced file.')
    filaments = info['filaments']
    if len(trays) != len(filaments):
        raise SendError(f'--ams-map lists {len(trays)} slots but the plate uses {len(filaments)} filaments (give one per filament, in filament order).')
    side = {0: 'right', 1: 'left'}
    problems = []
    is_h2d = info.get('printer_model') == 'Bambu Lab H2D'
    for f, tray in zip(filaments, trays):
        label = slot_label(tray, is_h2d=is_h2d)
        if is_h2d:
            if tray == 255:
                feeds = 0
            elif tray == 254:
                feeds = 1
            else:
                feeds = 0 if tray // 4 == 0 else 1
            if f['hw_nozzle_id'] != feeds:
                problems.append(f"filament {f['id']} prints on the {side[f['hw_nozzle_id']]} nozzle but {label} feeds the {side[feeds]} one")
        live = live_tray(status, tray)
        if not live.get('tray_type'):
            problems.append(f'{label} is empty or unknown')
            continue
        if live['tray_type'] != f['type']:
            problems.append(f"filament {f['id']} is {f['type']} but {label} holds {live['tray_type']}")
        want, have = rgba_hex(f.get('color'))[:6], rgba_hex(live.get('tray_color'), fallback='')[:6]
        if want != have and not allow_color_mismatch:
            problems.append(f"filament {f['id']} is #{want} but {label} reports #{have or 'no colour'}")
    if problems:
        raise SendError('AMS map refused: ' + '; '.join(problems) + '.')
    for hw in sorted({f['hw_nozzle_id'] for f in filaments}):
        if info.get('printer_model') == 'Bambu Lab P1S':
            diameter = status.get('nozzle_diameter')
        else:
            nozzles = [n for n in status.get('nozzles', []) if n.get('id') == hw]
            diameter = nozzles[0].get('diameter') if len(nozzles) == 1 else None
        try:
            ok = float(diameter) == 0.4
        except (TypeError, ValueError):
            ok = False
        if not ok:
            raise SendError('Live nozzle diameter is unknown or does not match the sliced file.')

def mapping_lists(info: dict, trays: list[int]) -> tuple[list, list, list]:
    """Studio's per-filament arrays: tray ids, {ams_id, slot_id} and nozzle ids; unused filaments get -1 / 255."""
    by_id = {f['id']: (f, t) for f, t in zip(info['filaments'], trays)}
    nozzles = info.get('filament_nozzles') or []
    v0, v1, nozzle_map = [], [], []
    for fid in range(1, info['filament_count'] + 1):
        if fid in by_id:
            f, t = by_id[fid]
            v0.append(t)
            if t in EXTERNAL_SLOTS:
                v1.append({'ams_id': 255, 'slot_id': t})
            else:
                v1.append({'ams_id': t // 4, 'slot_id': t % 4})
            nozzle_map.append(f['hw_nozzle_id'])
        else:
            v0.append(-1); v1.append({'ams_id': 255, 'slot_id': 255})
            nozzle_map.append(nozzles[fid - 1] if fid - 1 < len(nozzles) else 0)
    return v0, v1, nozzle_map

def record_print_params(path: Path, config_3mf: Path, info: dict, status: dict, tray, timelapse: bool = True) -> dict:
    if info.get('filaments'):
        return record_print_params_multi(path, config_3mf, info, status, list(tray), timelapse)
    ams_id, slot_id = (255, tray) if tray in EXTERNAL_SLOTS else (tray // 4, tray % 4)
    hw_nozzle_id = int(info.get('hw_nozzle_id', 0))
    tray_info = live_tray(status, tray)
    source_color = rgba_hex(info.get('color'))
    target_color = rgba_hex(tray_info.get('tray_color'), fallback=source_color)
    filament_id = tray_info.get('tray_info_idx') or info.get('tray_info_idx') or 'GFL99'
    nozzle_by_id = {int(n['id']): n for n in status.get('nozzles', []) if isinstance(n, dict) and 'id' in n}
    nozzle_ids = (0,) if info.get('printer_model') == 'Bambu Lab P1S' else (1, 0)
    nozzles_info = []
    for nid in nozzle_ids:
        live = nozzle_by_id.get(nid, {})
        nozzles_info.append({
            'id': nid,
            'type': live.get('type') or '',
            'flowSize': 'standard_flow',
            'diameter': float(live.get('diameter', 0.4)),
        })
    project_stem = info['filename'][:-10] if info['filename'].endswith('.gcode.3mf') else info['filename'][:-4]
    use_ams = tray < 16
    return {
        'task_name': info['filename'][:-4],
        'project_name': project_stem,
        'preset_name': '',
        'filename': str(path),
        'config_filename': str(config_3mf),
        'plate_index': int(info['plate']),
        'print_type': 'from_sdcard_view',
        'bed_type': info['bed_type'],
        'timelapse': bool(timelapse),
        'use_ams': use_ams,
        'ams_mapping': json.dumps([tray], separators=(',', ':')),
        'ams_mapping2': json.dumps([{'ams_id': ams_id, 'slot_id': slot_id}], separators=(',', ':')),
        'ams_mapping_info': json.dumps([{
            'ams': tray,
            'sourceColor': source_color,
            'targetColor': target_color,
            'filamentId': filament_id,
            'filamentType': info['material'],
            'targetFilamentType': '',
            'weight': float(info.get('grams', 0.0)),
            'nozzleId': hw_nozzle_id,
        }], separators=(',', ':')),
        'nozzle_mapping': json.dumps([hw_nozzle_id], separators=(',', ':')),
        'nozzles_info': json.dumps(nozzles_info, separators=(',', ':')),
    }

def record_print_params_multi(path: Path, config_3mf: Path, info: dict, status: dict, trays: list[int], timelapse: bool = True) -> dict:
    """Studio's local print record for a multi-filament plate (SelectMachineDialog::get_ams_mapping_result)."""
    v0, v1, nozzle_map = mapping_lists(info, trays)
    by_id = {f['id']: (f, t) for f, t in zip(info['filaments'], trays)}
    mapping_info = []
    for fid in range(1, info['filament_count'] + 1):
        if fid not in by_id:
            mapping_info.append({'ams': -1, 'targetColor': '', 'filamentId': '', 'filamentType': ''})
            continue
        f, t = by_id[fid]
        live = live_tray(status, t)
        source = rgba_hex(f.get('color'))
        mapping_info.append({'ams': t, 'sourceColor': source, 'targetColor': rgba_hex(live.get('tray_color'), fallback=source),
                             'filamentId': f.get('tray_info_idx') or live.get('tray_info_idx') or 'GFL99',
                             'filamentType': f['type'], 'targetFilamentType': '', 'weight': float(f.get('grams', 0.0)),
                             'nozzleId': f['hw_nozzle_id']})
    nozzle_by_id = {int(n['id']): n for n in status.get('nozzles', []) if isinstance(n, dict) and 'id' in n}
    nozzle_ids = (0,) if info.get('printer_model') == 'Bambu Lab P1S' else (1, 0)
    nozzles_info = [{'id': nid, 'type': nozzle_by_id.get(nid, {}).get('type') or '', 'flowSize': 'standard_flow',
                     'diameter': float(nozzle_by_id.get(nid, {}).get('diameter', 0.4))} for nid in nozzle_ids]
    project_stem = info['filename'][:-10] if info['filename'].endswith('.gcode.3mf') else info['filename'][:-4]
    compact = dict(separators=(',', ':'))
    use_ams = any(t < 16 for t in trays)
    return {
        'task_name': info['filename'][:-4],
        'project_name': project_stem,
        'preset_name': '',
        'filename': str(path),
        'config_filename': str(config_3mf),
        'plate_index': int(info['plate']),
        'print_type': 'from_sdcard_view',
        'bed_type': info['bed_type'],
        'timelapse': bool(timelapse),
        'use_ams': use_ams,
        'ams_mapping': json.dumps(v0, **compact),
        'ams_mapping2': json.dumps(v1, **compact),
        'ams_mapping_info': json.dumps(mapping_info, **compact),
        'nozzle_mapping': json.dumps(nozzle_map, **compact),
        'nozzles_info': json.dumps(nozzles_info, **compact),
    }

def print_payload(info: dict, tray, sequence: str, timelapse: bool = True) -> dict:
    if info.get('filaments'):
        trays = list(tray)
        ams_mapping, ams_mapping2, nozzle_mapping = mapping_lists(info, trays)
        use_ams = any(t < 16 for t in trays)
    else:
        hw_nozzle_id = int(info.get('hw_nozzle_id', 0))
        ams_id, slot_id = (255, tray) if tray in EXTERNAL_SLOTS else (tray // 4, tray % 4)
        ams_mapping, ams_mapping2, nozzle_mapping = [tray], [{'ams_id': ams_id, 'slot_id': slot_id}], [hw_nozzle_id]
        use_ams = tray < 16
    payload = {'print': dict(command='project_file', sequence_id=sequence, param=info['param'],
                          project_id='0', profile_id='0', task_id='0', subtask_id='0',
                          subtask_name=info['filename'][:-4], url='ftp://' + info['filename'],
                          md5='from_sd_card', timelapse=timelapse, bed_type=info['bed_type'],
                          bed_leveling=True, auto_bed_leveling=2, flow_cali=False,
                          vibration_cali=True, layer_inspect=True, use_ams=use_ams,
                          ams_mapping=ams_mapping, ams_mapping2=ams_mapping2,
                          nozzle_mapping=nozzle_mapping, nozzle_offset_cali=0, extrude_cali_flag=0,
                          extrude_cali_manual_mode=-1)}
    if info.get('printer_model') == 'Bambu Lab P1S':
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
    alt_name = name[:-6] if name.endswith('.gcode') else None
    cloud_job_id = next((e['job_id'] for e in events if e.get('event') == 'wait_printer' and 'job_id' in e), None)
    for e in events:
        if e.get('command') != 'push_status':
            continue
        job = e.get('subtask_name', job)
        state = e.get('gcode_state', state)
        if job in (name, alt_name) and job is not None and state in {'PREPARE', 'RUNNING'}:
            out = {k: e[k] for k in ('gcode_state', 'stg_cur', 'layer_num', 'mc_percent', 'mc_remaining_time') if k in e} | {'job': job, 'gcode_state': state}
            if cloud_job_id is not None:
                out['cloud_job_id'] = cloud_job_id
            return out
    return None

def cloud_history(connection: dict, limit: int = 20, offset: int = 0) -> dict:
    if not (1 <= limit <= 100 and offset >= 0):
        raise SendError('History limit must be 1..100 and offset must be nonnegative.')
    events, rc = bridge_run(connection, 'history', {'limit': limit, 'offset': offset})
    if rc:
        raise SendError('Cloud history query failed; ensure Bambu Studio has an active signed-in session.')
    for event in events:
        if event.get('event') == 'history':
            return {'total': int(event.get('total', 0)), 'tasks': list(event.get('tasks', []))}
    raise SendError('No cloud history response received.')

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
    parser.add_argument('--tray', help='Zero-based AMS tray (0..15, A1..D4), or external spool (external/ext, ext-l/254, ext-r/255); 1 = A2 (default)')
    parser.add_argument('--no-ams', action='store_true', help='Print from external spool / bypass AMS')
    parser.add_argument('--external', action='store_true', dest='no_ams', help='Print from external spool / bypass AMS')
    parser.add_argument('--ams-map', help='Multi-filament plates: one AMS slot per filament in filament order, e.g. B3,B1,B2,A4 (A1..D4 or 0..15)')
    parser.add_argument('--allow-color-mismatch', action='store_true', help='Accept --ams-map slots whose reported colour differs from the sliced colour')
    parser.add_argument('--plate', type=int, default=1)
    parser.add_argument('--start', action='store_true', help='Upload and submit one print after preflight')
    parser.add_argument('--repeat', action='store_true', help='Explicitly permit another attempt for this file after checking prior outcome')
    parser.add_argument('--sign-probe', action='store_true', help='Read-only calibration probe; does not prove print authorization')
    parser.add_argument('--no-timelapse', action='store_true', help='Skip the printer camera timelapse for this job')
    parser.add_argument('--no-cloud-record', action='store_true', help='Start via raw LAN MQTT without registering a Bambu Handy cloud history task')
    parser.add_argument('--report', type=Path, help='Save a sanitized verification report (no access codes, tokens, serial or host)')
    args = parser.parse_args(argv)
    path = args.file.expanduser().resolve()
    info = inspect_file(path, args.plate)
    is_h2d = info.get('printer_model') == 'Bambu Lab H2D'
    hw_nozzle = info.get('hw_nozzle_id')

    if info.get('filaments'):
        if not args.ams_map:
            raise SendError(f"This plate uses {len(info['filaments'])} filaments; pass --ams-map with one AMS slot per filament in filament order (for example A1,B2,...).")
        args.trays = parse_ams_map(args.ams_map, filaments=info['filaments'], is_h2d=is_h2d)
        if args.no_ams and any(t < 16 for t in args.trays):
            raise SendError('--no-ams was specified but --ams-map contains AMS slots.')
    elif args.ams_map:
        args.trays = parse_ams_map(args.ams_map, filaments=[dict(hw_nozzle_id=hw_nozzle)], is_h2d=is_h2d)
        if len(args.trays) != 1:
            raise SendError('This plate uses one filament; --ams-map must name exactly one slot.')
        args.tray = args.trays[0]
        if args.no_ams and args.tray < 16:
            raise SendError('--no-ams was specified but --ams-map names an AMS slot.')
    else:
        if args.tray is None:
            if args.no_ams:
                args.tray = 255 if (is_h2d and hw_nozzle == 0) else 254
            else:
                args.tray = 1
        else:
            args.tray = parse_tray_spec(args.tray, hw_nozzle_id=hw_nozzle, is_h2d=is_h2d)
            if args.no_ams and args.tray < 16:
                raise SendError(f'--no-ams was specified but tray {args.tray} is an AMS slot; choose an external slot or remove --no-ams.')
        args.trays = [args.tray]
    connection = load_connection(args.host, args.serial)
    # Serialize all runs for this printer, including the final fresh-status check.
    with printer_lock(connection) as (cache, printer_key):
        return run_locked(args, path, info, connection, cache, printer_key)

def run_locked(args, path, info, connection, cache, printer_key) -> int:
    cloud_record = not getattr(args, 'no_cloud_record', False)
    multi = bool(info.get('filaments'))
    trays = getattr(args, 'trays', None) or [args.tray]
    selection = trays if multi else args.tray
    is_h2d = info.get('printer_model') == 'Bambu Lab H2D'
    report = {'file': info, 'ams_tray': None if (multi or args.tray in EXTERNAL_SLOTS) else args.tray,
              'external_tray': args.tray if (not multi and args.tray in EXTERNAL_SLOTS) else None,
              'timelapse': not args.no_timelapse,
              'cloud_record': cloud_record, 'gui_used': False, 'helper_path_shim': True, 'started': False}
    if multi:
        report['ams_map'] = [{'filament': f['id'], 'slot': slot_label(t, is_h2d=is_h2d), 'tray': t, 'type': f['type'], 'color': f.get('color'),
                              'nozzle': 'left' if f['hw_nozzle_id'] else 'right'} for f, t in zip(info['filaments'], trays)]
        print(f"File checked: {info['filename']} (Plate {info['plate']}) | {info['printer_model']} | {len(info['filaments'])} filaments | 0.4 mm | "
              f"AMS map {','.join(slot_label(t, is_h2d=is_h2d) for t in trays)}", flush=True)
    else:
        slot_desc = f"external spool {slot_label(args.tray, is_h2d=is_h2d)}" if args.tray in EXTERNAL_SLOTS else f"AMS tray {args.tray}"
        print(f"File checked: {info['filename']} (Plate {info['plate']}) | {info['printer_model']} | {info['material']} | 0.4 mm | {slot_desc}", flush=True)
    events, rc = bridge_run(connection, 'status', {'expected_model': info['model_id']})
    if rc:
        raise SendError('Live printer connection failed; no print was submitted.')
    status = merged_status(events)
    if multi:
        preflight_multi(status, info, trays, getattr(args, 'allow_color_mismatch', False))
        for row in report['ams_map']:
            live = live_tray(status, row['tray'])
            print(f"  filament {row['filament']} {row['type']} {row['color']} -> {row['slot']} ({live.get('tray_type')} "
                  f"#{rgba_hex(live.get('tray_color'), fallback='')[:6]}), {row['nozzle']} nozzle", flush=True)
    else:
        preflight(status, info, args.tray)
    report['preflight'] = {k: status[k] for k in ('gcode_state', 'subtask_name', 'model_id', 'trays', 'nozzles', 'nozzle_diameter') if k in status}
    source_name = "external spool" if (not multi and args.tray in EXTERNAL_SLOTS) or (multi and all(t in EXTERNAL_SLOTS for t in trays)) else "AMS"
    print(f"Live preflight passed: {status['gcode_state']}; {source_name} material and nozzle match.", flush=True)
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
        payload = print_payload(info, selection, sequence, timelapse=report['timelapse'])
        job_name = payload['print']['subtask_name']
        # Persist BEFORE dispatch. Unknown outcomes must never silently resubmit.
        report.update(submission_attempted=True, submitted_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        write_private(receipt_path, report)
        print('Submitting once through the official networking plugin; waiting for matching PREPARE/RUNNING...', flush=True)
        job_req = {'sequence_id': sequence, 'job_name': job_name,
                   'expected_model': info['model_id'], 'payload': json.dumps(payload, separators=(',', ':'))}
        if cloud_record:
            with tempfile.TemporaryDirectory(prefix='h2d-config-') as cfg_dir:
                cfg_3mf = create_config_3mf(path, Path(cfg_dir) / 'config.3mf')
                job_req['record_params'] = record_print_params(path, cfg_3mf, info, status, selection, timelapse=report['timelapse'])
                events, rc = bridge_run(connection, 'start', job_req)
        else:
            events, rc = bridge_run(connection, 'start', job_req)
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
