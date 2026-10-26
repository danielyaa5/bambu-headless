# SPDX-License-Identifier: AGPL-3.0-only
"""Certificate-validated file operations; no print commands are sent here."""
from __future__ import annotations

from contextlib import contextmanager
import ftplib
import hashlib
import os
from pathlib import Path, PurePosixPath
import tempfile

import send_h2d_print as core


def remote_path(value: str, *, directory: bool = False) -> str:
    """Reject FTP command injection, traversal and ambiguous wildcard names."""
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise core.SendError('Remote path is empty or contains control characters.')
    if any(c in value for c in ('\\', '*', '?', '[', ']')):
        raise core.SendError('Remote paths cannot contain backslashes or wildcards.')
    parts = value.split('/')
    if any(p in ('.', '..') for p in parts):
        raise core.SendError('Remote paths cannot contain dot or parent segments.')
    path = '/' + str(PurePosixPath(value)).lstrip('/')
    if not directory and (path == '/' or value.endswith('/')):
        raise core.SendError('Select a file, not a directory.')
    return path


def print_file_path(value: str) -> str:
    path = remote_path(value)
    if not path.lower().endswith('.gcode.3mf'):
        raise core.SendError('Upload, rename and delete are limited to sliced .gcode.3mf files.')
    return path


@contextmanager
def connect(connection: dict):
    try:
        with core.ImplicitFTPS(connection['ip'], connection['serial']) as ftp:
            ftp.login('bblp', connection['access_code'])
            ftp.prot_p()
            ftp.voidcmd('TYPE I')
            yield ftp
    except core.SendError:
        raise
    except Exception as exc:
        raise core.SendError(f'File operation failed ({type(exc).__name__}); inspect storage before retrying.') from None


def entries(ftp, directory: str = '/') -> list[dict]:
    directory = remote_path(directory, directory=True)
    ftp.cwd(directory)
    try:
        listing = list(ftp.mlsd())
        result = []
        for name, facts in listing:
            if facts.get('type') in ('cdir', 'pdir') or name in ('.', '..'):
                continue
            # Names come from a remote peer and are never reused as FTP commands
            # without validation. Refuse unexpected paths in a child listing.
            if '/' in name:
                raise core.SendError('Unexpected path in printer directory listing.')
            path = remote_path(directory.rstrip('/') + '/' + name)
            result.append({'name': name, 'path': path, 'type': facts.get('type', 'unknown'),
                           'bytes': int(facts['size']) if facts.get('size', '').isdigit() else None,
                           'modified_utc': facts.get('modify')})
        return sorted(result, key=lambda item: item['name'].casefold())
    except ftplib.error_perm as exc:
        if not str(exc).startswith(('500', '501', '502', '504')):
            raise
    # Some firmware versions have NLST but no MLSD. Do not guess file types
    # from a human-formatted LIST response.
    result = []
    names = ftp.nlst()
    ftp.voidcmd('TYPE I')
    for value in names:
        name = PurePosixPath(value).name
        if name in ('.', '..'):
            continue
        path = remote_path(directory.rstrip('/') + '/' + name)
        try:
            size = ftp.size(path)
            kind = 'file'
        except ftplib.error_perm as exc:
            if not str(exc).startswith('550'):
                raise
            size, kind = None, 'unknown'
        result.append({'name': name, 'path': path, 'type': kind, 'bytes': size, 'modified_utc': None})
    return sorted(result, key=lambda item: item['name'].casefold())


def stat(ftp, path: str) -> dict | None:
    path = remote_path(path)
    matches = [item for item in entries(ftp, str(PurePosixPath(path).parent)) if item['path'] == path]
    if len(matches) > 1:
        raise core.SendError('Ambiguous remote filename.')
    return matches[0] if matches else None


def require_file(ftp, path: str) -> dict:
    item = stat(ftp, path)
    if item is None or item['type'] != 'file':
        raise core.SendError('Remote file is missing or is not a regular file.')
    return item


def fingerprint(ftp, path: str) -> dict:
    digest, length = hashlib.md5(), 0
    def consume(chunk):
        nonlocal length
        digest.update(chunk)
        length += len(chunk)
    ftp.retrbinary('RETR ' + remote_path(path), consume)
    return {'bytes': length, 'md5': digest.hexdigest()}


def list_files(connection: dict, directory: str = '/') -> list[dict]:
    with connect(connection) as ftp:
        return entries(ftp, directory)


def upload(connection: dict, local: Path, info: dict, destination: str) -> dict:
    destination = print_file_path(destination)
    expected = {'bytes': info['bytes'], 'md5': info['md5']}
    with connect(connection) as ftp:
        existing = stat(ftp, destination)
        if existing:
            if existing['type'] == 'file' and existing['bytes'] == expected['bytes'] and fingerprint(ftp, destination) == expected:
                return dict(path=destination, reused=True, verified=True, **expected)
            raise core.SendError('Destination exists with different contents; use a new remote name.')
        # The same opened descriptor is hashed, rewound and uploaded. A changed
        # source is caught before upload or by the full read-back check.
        with local.open('rb') as source:
            digest, length = hashlib.md5(), 0
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
                length += len(chunk)
            if {'bytes': length, 'md5': digest.hexdigest()} != expected:
                raise core.SendError('Local file changed after validation; upload refused.')
            source.seek(0)
            ftp.storbinary('STOR ' + destination, source)
        if fingerprint(ftp, destination) != expected:
            raise core.SendError('Upload read-back mismatch; no print command was sent.')
        return dict(path=destination, reused=False, verified=True, **expected)


def download(connection: dict, source: str, destination: Path) -> dict:
    source = remote_path(source)
    if destination.exists() or destination.is_symlink():
        raise core.SendError('Local destination exists; choose a new path.')
    if not destination.parent.is_dir():
        raise core.SendError('Local destination directory does not exist.')
    with connect(connection) as ftp:
        before = require_file(ftp, source)
        fd, name = tempfile.mkstemp(prefix='.bambu-download-', dir=destination.parent)
        temporary = Path(name)
        digest, length = hashlib.md5(), 0
        try:
            with os.fdopen(fd, 'wb') as out:
                def consume(chunk):
                    nonlocal length
                    out.write(chunk)
                    digest.update(chunk)
                    length += len(chunk)
                ftp.retrbinary('RETR ' + source, consume)
                out.flush()
                os.fsync(out.fileno())
            after = require_file(ftp, source)
            if before != after or before['bytes'] != length:
                raise core.SendError('Remote file changed or download length mismatched; local output was not installed.')
            # Atomic no-clobber publication, on the same filesystem. Unlike
            # replace(), this also refuses a destination created concurrently.
            os.link(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    return {'path': source, 'bytes': length, 'md5': digest.hexdigest(), 'downloaded': True}


def rename(connection: dict, source: str, destination: str) -> dict:
    source, destination = print_file_path(source), print_file_path(destination)
    if source == destination:
        raise core.SendError('Source and destination must differ.')
    with connect(connection) as ftp:
        require_file(ftp, source)
        if stat(ftp, destination) is not None:
            raise core.SendError('Rename destination already exists; overwrite refused.')
        before = fingerprint(ftp, source)
        ftp.rename(source, destination)
        if stat(ftp, source) is not None or fingerprint(ftp, destination) != before:
            raise core.SendError('Rename could not be verified; inspect storage before retrying.')
    return {'source': source, 'destination': destination, 'renamed': True, **before}


def delete(connection: dict, source: str) -> dict:
    source = print_file_path(source)
    with connect(connection) as ftp:
        require_file(ftp, source)
        ftp.delete(source)
        if stat(ftp, source) is not None:
            raise core.SendError('Delete could not be verified; inspect storage before retrying.')
    return {'path': source, 'deleted': True}
