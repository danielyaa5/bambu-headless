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


@unittest.skipUnless((core.IS_LINUX and shutil.which('g++')) or (sys.platform == 'darwin' and shutil.which('clang++')), 'Native test needs a C++ compiler')
class NativeBridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='bambu-fake-plugin-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.plugin=Path(cls.temp.name)/'fake.dylib'
        cls.binary=core.ensure_bridge()
        subprocess.run((['g++','-std=c++17','-shared','-fPIC'] if core.IS_LINUX else ['clang++','-std=c++17','-dynamiclib'])+['-I',str(core.HERE),
                        str(core.HERE/'tests/fake_network_plugin.cpp'),'-o',str(cls.plugin)],
                       check=True,capture_output=True)
    def run_bridge(self,scenario,control='pause',action='control',model=None,force=False):
        command='project_file' if action=='start' else control
        cfg={'action':action,'control':control,'job_name':'fixture.gcode','sequence_id':'42',
             'scratch':scenario,'plugin':str(self.plugin),'serial':'TEST_PRINTER',
             'force':force,
             'payload':json.dumps({'print':{'command':command,'sequence_id':'42','param':'','subtask_name':'fixture.gcode'}})}
        if model is not None or action=='start':
            cfg['expected_model']=model or 'O1D'
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
        rc,events=self.run_bridge('resume-error','resume',force=True)
        self.assertEqual(rc,0)
        self.assertTrue(events[-1]['confirmed'])
    def test_printer_rejection_is_not_success(self):
        rc,events=self.run_bridge('rejected')
        self.assertEqual(rc,7)
        self.assertFalse(events[-1]['confirmed'])
    def test_existing_print_path_still_confirms_matching_job(self):
        rc,events=self.run_bridge('start',action='start')
        self.assertEqual(rc,0)
        self.assertTrue(events[-1]['confirmed'])
    def test_status_preserves_nested_telemetry_and_filters_unlisted_fields(self):
        rc,events=self.run_bridge('status-details',action='status')
        self.assertEqual(rc,0)
        telemetry=next(e for e in events if e.get('event')=='telemetry')
        self.assertEqual(telemetry['gcode_state'],'RUNNING')
        self.assertEqual(telemetry['bed_temper'],60.5)
        self.assertEqual(telemetry['nozzle_temper'],215.25)
        self.assertEqual(telemetry['trays'],[{'ams_id':'0','id':'1','tray_type':'PLA',
                                            'tray_color':'FFFFFFFF','tray_info_idx':'GFA00'}])
        self.assertEqual(telemetry['nozzles'],[{'id':0,'diameter':0.4,'type':'hardened_steel'}])
        self.assertNotIn('private_field',telemetry)
        self.assertEqual(events[-1]['event'],'status_complete')
    def test_arbitrary_control_commands_rejected_before_plugin_load(self):
        rc,events=self.run_bridge('pause','gcode_line')
        self.assertEqual(rc,3)
        self.assertEqual(events,[])
    def test_model_detection_matches_the_configured_printer_before_dispatch(self):
        for scenario in ('start','wrong-serial','detection-failed'):
            with self.subTest(scenario=scenario):
                rc,events=self.run_bridge(scenario,action='start',model='C12' if scenario=='start' else 'O1D')
                self.assertEqual(rc,5)
                self.assertFalse(any(e.get('event')=='dispatching' for e in events))
        rc,events=self.run_bridge('p1s',action='status',model='C12')
        self.assertEqual(rc,0)
        self.assertIn({'event':'printer_identity','model_id':'C12'},events)
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
    def test_start_with_record_returns_cloud_job_id(self):
        cfg={'action':'start','control':'','job_name':'fixture.gcode','sequence_id':'42',
             'scratch':'start-record','plugin':str(self.plugin),'serial':'TEST_PRINTER','expected_model':'O1D',
             'payload':json.dumps({'print':{'command':'project_file','sequence_id':'42','param':'','subtask_name':'fixture.gcode'}}),
             'record_params':{'task_name':'fixture.gcode','project_name':'fixture','filename':'/tmp/a.3mf',
                              'config_filename':'/tmp/c.3mf','plate_index':1,'bed_type':'textured_plate',
                              'ams_mapping':'[7]','ams_mapping2':'[{"ams_id":1,"slot_id":3}]',
                              'ams_mapping_info':'[{"ams":7}]','nozzle_mapping':'[1]','nozzles_info':'[{"id":1}]'}}
        result=subprocess.run([str(self.binary)],input=json.dumps(cfg),capture_output=True,text=True,timeout=5)
        events=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(result.returncode,0)
        self.assertTrue(events[-1]['confirmed'])
        self.assertTrue(any(e.get('event')=='dispatching' and e.get('mode')=='cloud_record' for e in events))
        self.assertTrue(any(e.get('event')=='wait_printer' and e.get('job_id')==777001 for e in events))
    def test_start_with_record_respects_no_ams(self):
        cfg={'action':'start','control':'','job_name':'fixture.gcode','sequence_id':'42',
             'scratch':'no-ams-record','plugin':str(self.plugin),'serial':'TEST_PRINTER','expected_model':'O1D',
             'payload':json.dumps({'print':{'command':'project_file','sequence_id':'42','param':'','subtask_name':'fixture.gcode','use_ams':False}}),
             'record_params':{'task_name':'fixture.gcode','project_name':'fixture','filename':'/tmp/a.3mf',
                              'config_filename':'/tmp/c.3mf','plate_index':1,'bed_type':'textured_plate',
                              'use_ams':False,
                              'ams_mapping':'[255]','ams_mapping2':'[{"ams_id":255,"slot_id":0}]',
                              'ams_mapping_info':'[{"ams":255}]','nozzle_mapping':'[0]','nozzles_info':'[{"id":0}]'}}
        result=subprocess.run([str(self.binary)],input=json.dumps(cfg),capture_output=True,text=True,timeout=5)
        events=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(result.returncode,0)
        self.assertTrue(events[-1]['confirmed'])
    def test_status_preserves_vir_slot_and_vt_tray_telemetry(self):
        rc,events=self.run_bridge('status-vir-slot',action='status')
        self.assertEqual(rc,0)
        telemetry=next(e for e in events if e.get('event')=='telemetry')
        self.assertEqual(telemetry['trays'],[
            {'ams_id':'255','id':'254','tray_color':'FFFFFFFF','tray_info_idx':'GFG99','tray_type':'PETG'},
            {'ams_id':'255','id':'255','tray_color':'F98C36FF','tray_info_idx':'GFU99','tray_type':'TPU'}
        ])
        rc,events=self.run_bridge('status-vt-tray',action='status',model='C12')
        self.assertEqual(rc,0)
        telemetry=next(e for e in events if e.get('event')=='telemetry')
        self.assertEqual(telemetry['trays'],[
            {'ams_id':'255','id':'254','tray_color':'FFFFFFFF','tray_info_idx':'GFG99','tray_type':'PETG'}
        ])
    def test_history_returns_user_tasks_without_printer_connection(self):
        cfg={'action':'history','limit':5,'offset':0,'scratch':'history','plugin':str(self.plugin),'serial':'TEST_PRINTER'}
        result=subprocess.run([str(self.binary)],input=json.dumps(cfg),capture_output=True,text=True,timeout=5)
        events=[json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(result.returncode,0)
        self.assertEqual(events[-1]['event'],'history')
        self.assertEqual(events[-1]['total'],1)
        self.assertEqual(events[-1]['tasks'][0]['id'],9001)
        self.assertNotIn('secretToken',json.dumps(events[-1]))
        self.assertNotIn('extraSecret',json.dumps(events[-1]))


if __name__ == '__main__':
    unittest.main()
