# SPDX-License-Identifier: AGPL-3.0-only
"""Focused safety checks; no network calls or physical printer commands."""
import contextlib
import io
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import zipfile

spec = importlib.util.spec_from_file_location('sender', Path(__file__).with_name('send_h2d_print.py'))
sender = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sender)

class SenderSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'fixture.gcode.3mf'
    def fixture(self, mapping='2', checksum=None, material='PLA'):
        gcode = b'; validation fixture only\n'
        with zipfile.ZipFile(self.path, 'w') as z:
            z.writestr('Metadata/project_settings.config', json.dumps({'printer_model':'Bambu Lab H2D','curr_bed_type':'High Temp Plate','layer_height':'0.2'}))
            z.writestr('Metadata/plate_1.gcode', gcode)
            z.writestr('Metadata/plate_1.gcode.md5', checksum or hashlib.md5(gcode).hexdigest())
            z.writestr('Metadata/slice_info.config', f'<config><plate><metadata key="index" value="1"/><metadata key="filament_maps" value="{mapping}"/><metadata key="prediction" value="10"/><metadata key="weight" value="1"/><filament id="1" type="{material}" nozzle_diameter="0.4" color="#FFFFFF"/></plate></config>')
        return sender.inspect_file(self.path)
    def status(self, state='FINISH', material='PLA', diameter=.4):
        return {'gcode_state':state,'trays':[{'ams_id':'0','id':'1','tray_type':material}],'nozzles':[{'id':0,'diameter':diameter}]}
    def test_valid_right_nozzle_file_and_a2(self):
        sender.preflight(self.status(), self.fixture(), 1)
    def test_corrupted_embedded_gcode_rejected(self):
        with self.assertRaises(sender.SendError): self.fixture(checksum='0'*32)
    def test_left_nozzle_rejected(self):
        with self.assertRaises(sender.SendError): self.fixture(mapping='1')
    def test_busy_and_paused_states_rejected(self):
        info = self.fixture()
        for state in ['PREPARE','RUNNING','PAUSE','UNKNOWN','']:
            with self.subTest(state=state), self.assertRaises(sender.SendError):
                sender.preflight(self.status(state=state), info, 1)
    def test_wrong_material_empty_tray_and_wrong_nozzle_rejected(self):
        info = self.fixture()
        for status, tray in [(self.status(material='ASA'),1),(self.status(),2),(self.status(diameter=.6),1)]:
            with self.subTest(status=status, tray=tray), self.assertRaises(sender.SendError):
                sender.preflight(status, info, tray)
    def test_previous_job_running_does_not_confirm_new_job(self):
        events=[{'command':'push_status','gcode_state':'RUNNING','subtask_name':'old-job'}]
        self.assertIsNone(sender.verified_start(events,'new-job'))
    def test_matching_job_confirmed_across_delta_updates(self):
        events=[{'command':'push_status','subtask_name':'new-job','gcode_state':'IDLE'}, {'command':'push_status','gcode_state':'PREPARE'}]
        self.assertEqual(sender.verified_start(events,'new-job')['gcode_state'],'PREPARE')
    def test_publish_ack_alone_is_not_start_confirmation(self):
        self.assertIsNone(sender.verified_start([{'command':'project_file','result':'SUCCESS'}],'new-job'))
    def test_payload_uses_right_nozzle_and_a2(self):
        payload=sender.print_payload(self.fixture(),1,'22000')['print']
        self.assertEqual(payload['ams_mapping2'],[{'ams_id':0,'slot_id':1}])
        self.assertEqual(payload['nozzle_mapping'],[0])
        self.assertTrue(payload['url'].startswith('ftp://fixture.'))
        self.assertNotIn('gcode',payload)

    def run_cli(self, *flags, outcome=None, state='FINISH'):
        connection = {'serial': 'TEST_PRINTER', 'ip': 'printer.example'}
        status = dict(self.status(state=state), command='push_status')
        def fake_bridge(connection, action, job=None):
            if action == 'status':
                return [status], 0
            self.assertEqual(action, 'start')
            return outcome or ([], 7)
        with mock.patch.object(sender, 'load_connection', return_value=connection), \
             mock.patch.object(sender, 'bridge_run', side_effect=fake_bridge) as bridge, \
             mock.patch.object(sender, 'upload_verified', return_value='mock upload') as upload, \
             mock.patch.object(sender.Path, 'home', return_value=Path(self.temp.name)), \
             contextlib.redirect_stdout(io.StringIO()):
            self.last_upload = upload
            self.last_bridge = bridge
            return sender.main([str(self.path), '--host', 'printer.example', *flags])

    def test_default_cli_neither_uploads_nor_starts(self):
        self.fixture()
        self.assertEqual(self.run_cli(), 0)
        self.last_upload.assert_not_called()
        self.assertEqual([c.args[1] for c in self.last_bridge.call_args_list], ['status'])

    def test_uncertain_dispatch_receipt_blocks_next_start(self):
        self.fixture()
        with self.assertRaisesRegex(sender.SendError, 'Start was not confirmed'):
            self.run_cli('--start')
        self.last_upload.assert_called_once()
        receipts = list((Path(self.temp.name)/'Library/Caches/h2d-headless').glob('*.json'))
        self.assertEqual(len(receipts), 1)
        self.assertTrue(json.loads(receipts[0].read_text())['submission_attempted'])
        with self.assertRaisesRegex(sender.SendError, 'submission receipt'):
            self.run_cli('--start')
        self.last_upload.assert_not_called()
        self.assertEqual([c.args[1] for c in self.last_bridge.call_args_list], ['status'])

    def test_repeat_still_refuses_running_printer(self):
        self.fixture()
        with self.assertRaisesRegex(sender.SendError, 'refusing to interrupt'):
            self.run_cli('--start', '--repeat', state='RUNNING')
        self.last_upload.assert_not_called()

    def test_existing_report_permissions_are_restricted(self):
        report = Path(self.temp.name)/'report.json'
        report.write_text('{}')
        report.chmod(0o644)
        sender.write_private(report, {'started': False})
        self.assertEqual(report.stat().st_mode & 0o777, 0o600)

if __name__ == '__main__': unittest.main()
