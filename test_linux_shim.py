# SPDX-License-Identifier: AGPL-3.0-only
"""Verify process-local readlink substitution and unchanged unrelated links."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import send_h2d_print as core

@unittest.skipUnless(core.IS_LINUX, 'Linux readlink shim')
class LinuxShimTests(unittest.TestCase):
    def test_only_self_executable_is_redirected(self):
        core.ensure_bridge()
        with tempfile.TemporaryDirectory() as temp:
            link = Path(temp) / 'link'
            link.symlink_to('/unrelated/path')
            script = '''import ctypes,json,os,sys
lib=ctypes.CDLL(None, use_errno=True)
lib.readlink.argtypes=[ctypes.c_char_p,ctypes.c_void_p,ctypes.c_size_t]
lib.readlink.restype=ctypes.c_ssize_t
buf=ctypes.create_string_buffer(b'XXXXXXXX',9)
n=lib.readlink(b'/proc/self/exe',buf,4)
zero=lib.readlink(b'/proc/self/exe',buf,0)
print(json.dumps([os.readlink('/proc/self/exe'),os.readlink(sys.argv[1]),n,buf.raw.decode(),zero,ctypes.get_errno()]))
'''
            env = dict(os.environ, LD_PRELOAD=str(core.HERE/'h2d_host_identity.so'),
                       H2D_STUDIO_EXECUTABLE='/opt/bambu-studio/bin/bambu-studio')
            result = subprocess.run([sys.executable, '-c', script, str(link)],env=env,
                                    text=True,capture_output=True,check=True)
            values = json.loads(result.stdout)
            self.assertEqual(values[:3], [env['H2D_STUDIO_EXECUTABLE'],'/unrelated/path',4])
            self.assertEqual(values[3], '/optXXXX\x00')
            self.assertEqual(values[4:], [-1,22])
            env.pop('H2D_STUDIO_EXECUTABLE')
            result = subprocess.run([sys.executable,'-c',"import os; print(os.readlink('/proc/self/exe'))"],
                                    env=env,text=True,capture_output=True,check=True)
            self.assertEqual(Path(result.stdout.strip()).resolve(),Path(sys.executable).resolve())
