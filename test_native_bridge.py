# SPDX-License-Identifier: AGPL-3.0-only
"""Exercise the compiled adapter against an offline fake plugin, never a printer."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import send_h2d_print as core


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('clang++'), 'macOS native test needs clang++')
class NativeBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='bambu-fake-plugin-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.plugin=Path(cls.temp.name)/'fake.dylib'
        cls.binary=core.ensure_bridge()
        subprocess.run(['clang++','-std=c++17','-dynamiclib','-I',str(core.HERE),
                        str(core.HERE/'tests/fake_network_plugin.cpp'),'-o',str(cls.plugin)],
                       check=True,capture_output=True)
    def run_bridge(self,scenario,control='pause',action='control'):
        command='project_file' if action=='start' else control
        cfg={'action':action,'control':control,'job_name':'fixture.gcode','sequence_id':'42',
             'scratch':scenario,'plugin':str(self.plugin),'serial':'TEST_PRINTER',
             'payload':json.dumps({'print':{'command':command,'sequence_id':'42','param':'','subtask_name':'fixture.gcode'}})}
        result=subprocess.run([str(self.binary)],input=json.dumps(cfg),capture_output=True,text=True,timeout=5)
        return result.returncode,[json.loads(line) for line in result.stdout.splitlines()]
    def test_pause_resume_and_cancel_confirmation(self):
        for scenario,command,state in [('pause','pause','PAUSE'),('resume','resume','RUNNING'),('cancel','stop','FAILED')]:
            with self.subTest(command=command):
                rc,events=self.run_bridge(scenario,command)
                self.assertEqual(rc,0)
                self.assertTrue(events[-1]['confirmed'])
                self.assertTrue(any(e.get('gcode_state')==state for e in events))
    def test_native_rechecks_job_and_state_after_certificate_exchange(self):
        for scenario in ['changed-job','changed-state']:
            with self.subTest(scenario=scenario):
                rc,events=self.run_bridge(scenario)
                self.assertEqual(rc,5)
                self.assertFalse(any(e.get('event')=='dispatching' for e in events))
    def test_native_refuses_resume_with_printer_error(self):
        rc,events=self.run_bridge('resume-error','resume')
        self.assertEqual(rc,5)
        self.assertFalse(any(e.get('event')=='dispatching' for e in events))
    def test_printer_rejection_is_not_success(self):
        rc,events=self.run_bridge('rejected')
        self.assertEqual(rc,7)
        self.assertFalse(events[-1]['confirmed'])
    def test_existing_print_path_still_confirms_matching_job(self):
        rc,events=self.run_bridge('start',action='start')
        self.assertEqual(rc,0)
        self.assertTrue(events[-1]['confirmed'])
    def test_arbitrary_control_commands_rejected_before_plugin_load(self):
        rc,events=self.run_bridge('pause','gcode_line')
        self.assertEqual(rc,3)
        self.assertEqual(events,[])
    def test_bed_temp_confirms_target_and_rejects_arbitrary_gcode(self):
        cfg={'action':'bed_temp','target_c':55,'job_name':'','sequence_id':'42',
             'scratch':'bed_temp','plugin':str(self.plugin),'serial':'TEST_PRINTER',
             'payload':json.dumps({'print':{'command':'gcode_line','sequence_id':'42','param':'M140 S55\n'}})}
        result=subprocess.run([str(self.binary)],input=json.dumps(cfg),capture_output=True,text=True,timeout=5)
        events=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(result.returncode,0)
        self.assertTrue(events[-1]['confirmed'])
        self.assertTrue(any(e.get('bed_target_temper')==55 for e in events))
        for bad in [cfg|{'target_c':150},cfg|{'payload':json.dumps({'print':{'command':'gcode_line','sequence_id':'42','param':'G28\n'}})}]:
            bad_res=subprocess.run([str(self.binary)],input=json.dumps(bad),capture_output=True,text=True,timeout=5)
            self.assertEqual(bad_res.returncode,3)


if __name__ == '__main__':
    unittest.main()
