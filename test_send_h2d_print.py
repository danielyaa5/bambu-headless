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
        self.cache = Path(self.temp.name) / ('.cache/h2d-headless' if sender.IS_LINUX else 'Library/Caches/h2d-headless')
        patcher = mock.patch.dict(sender.os.environ, {'XDG_CACHE_HOME': str(Path(self.temp.name) / '.cache')})
        patcher.start()
        self.addCleanup(patcher.stop)
    def fixture(self, mapping='2', checksum=None, material='PLA', model='Bambu Lab H2D', diameter='0.4', plate_model=None):
        gcode = b'; validation fixture only\n'
        with zipfile.ZipFile(self.path, 'w') as z:
            z.writestr('Metadata/project_settings.config', json.dumps({'printer_model':model,'curr_bed_type':'High Temp Plate','layer_height':'0.2'}))
            z.writestr('Metadata/plate_1.gcode', gcode)
            z.writestr('Metadata/plate_1.gcode.md5', checksum or hashlib.md5(gcode).hexdigest())
            model_meta=f'<metadata key="printer_model_id" value="{plate_model}"/>' if plate_model else ''
            z.writestr('Metadata/slice_info.config', f'<config><plate>{model_meta}<metadata key="index" value="1"/><metadata key="filament_maps" value="{mapping}"/><metadata key="prediction" value="10"/><metadata key="weight" value="1"/><filament id="1" type="{material}" nozzle_diameter="{diameter}" color="#FFFFFF"/></plate></config>')
        return sender.inspect_file(self.path)
    def status(self, state='FINISH', material='PLA', diameter=.4):
        return {'gcode_state':state,'model_id':'O1D','trays':[{'ams_id':'0','id':'1','tray_type':material}],'nozzles':[{'id':0,'diameter':diameter}]}
    def test_valid_right_nozzle_file_and_a2(self):
        sender.preflight(self.status(), self.fixture(), 1)
    def test_valid_left_nozzle_file_and_b2(self):
        info = self.fixture(mapping='1')
        status = {'gcode_state':'FINISH','model_id':'O1D',
                  'trays':[{'ams_id':'1','id':'1','tray_type':'PLA'}],
                  'nozzles':[{'id':0,'diameter':0.4},{'id':1,'diameter':0.4}]}
        sender.preflight(status, info, 5)
        payload = sender.print_payload(info, 5, '22000')['print']
        self.assertEqual(payload['ams_mapping2'], [{'ams_id':1,'slot_id':1}])
        self.assertEqual(payload['nozzle_mapping'], [1])
    def test_corrupted_embedded_gcode_rejected(self):
        with self.assertRaises(sender.SendError): self.fixture(checksum='0'*32)
    def test_invalid_or_mismatched_nozzle_rejected(self):
        for bad_mapping in ('3', '1 2', ''):
            with self.subTest(mapping=bad_mapping), self.assertRaises(sender.SendError):
                self.fixture(mapping=bad_mapping)
        with self.assertRaisesRegex(sender.SendError, 'different H2D nozzle'):
            sender.preflight(self.status(), self.fixture(mapping='2'), 5)
        with self.assertRaisesRegex(sender.SendError, 'different H2D nozzle'):
            sender.preflight(self.status(), self.fixture(mapping='1'), 1)
    def test_record_params_builds_studio_local_print_record_schema(self):
        cfg_3mf = Path(self.temp.name) / 'config.3mf'
        right_info = self.fixture(mapping='2')
        sender.create_config_3mf(self.path, cfg_3mf)
        with zipfile.ZipFile(cfg_3mf) as z:
            self.assertIn('Metadata/slice_info.config', z.namelist())
            self.assertNotIn('Metadata/plate_1.gcode', z.namelist())
        right = sender.record_print_params(self.path, cfg_3mf, right_info, self.status(), 1)
        self.assertEqual(json.loads(right['ams_mapping']), [1])
        self.assertEqual(json.loads(right['ams_mapping2']), [{'ams_id':0,'slot_id':1}])
        self.assertEqual(json.loads(right['ams_mapping_info'])[0]['nozzleId'], 0)
        self.assertEqual(json.loads(right['nozzle_mapping']), [0])
        left_info = self.fixture(mapping='1')
        left = sender.record_print_params(self.path, cfg_3mf, left_info, self.status(), 7)
        self.assertEqual(json.loads(left['ams_mapping']), [7])
        self.assertEqual(json.loads(left['ams_mapping2']), [{'ams_id':1,'slot_id':3}])
        self.assertEqual(json.loads(left['ams_mapping_info'])[0]['nozzleId'], 1)
        self.assertEqual(json.loads(left['nozzle_mapping']), [1])
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
    def test_payload_records_camera_timelapse_unless_disabled(self):
        info=self.fixture()
        self.assertIs(sender.print_payload(info,1,'22000')['print']['timelapse'],True)
        self.assertIs(sender.print_payload(info,1,'22000',timelapse=False)['print']['timelapse'],False)
    def test_p1s_slice_preflight_and_single_nozzle_payload(self):
        info=self.fixture(model='Bambu Lab P1S',mapping='1 1 1 1 1',plate_model='C12',material='PETG')
        status=self.status(material='PETG')|{'model_id':'C12','nozzle_diameter':'0.4'}
        status.pop('nozzles')
        sender.preflight(status,info,1)
        payload=sender.print_payload(info,1,'42')['print']
        self.assertEqual(payload['ams_mapping'],[1])
        self.assertEqual(payload['url'],'ftp:///fixture.gcode.3mf')
        self.assertEqual(payload['md5'],info['md5'])
        self.assertFalse(payload['layer_inspect'])
        for field in ('nozzle_mapping','nozzle_offset_cali','extrude_cali_flag','ams_mapping2'):
            self.assertNotIn(field,payload)
        for changes in ({'model_id':'O1D'},{'model_id':None},{'nozzle_diameter':None},
                        {'nozzle_diameter':'0.6'},{'nozzle_diameter':'invalid'},{'gcode_state':'RUNNING'}):
            with self.subTest(changes=changes),self.assertRaises(sender.SendError):
                sender.preflight(status|changes,info,1)
        with self.assertRaises(sender.SendError):
            sender.preflight(status|{'trays':[]},info,1)
    def test_p1s_rejects_wrong_model_mapping_nozzle_and_checksum(self):
        for changes in ({'model':'Bambu Lab A1'},{'mapping':'2'},{'mapping':'1 2'},{'mapping':''},
                        {'plate_model':'O1D'},{'diameter':'0.6'},{'checksum':'0'*32}):
            with self.subTest(changes=changes),self.assertRaises(sender.SendError):
                self.fixture(**({'model':'Bambu Lab P1S','mapping':'1'}|changes))

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
        receipts = list(self.cache.glob('*.json'))
        self.assertEqual(len(receipts), 1)
        self.assertTrue(json.loads(receipts[0].read_text())['submission_attempted'])
        with self.assertRaisesRegex(sender.SendError, 'submission receipt'):
            self.run_cli('--start')
        self.last_upload.assert_not_called()
        self.assertEqual([c.args[1] for c in self.last_bridge.call_args_list], ['status'])

    def test_timelapse_choice_reaches_payload_receipt_and_report(self):
        self.fixture()
        report = Path(self.temp.name)/'report.json'
        for flags, expected in [((), True), (('--no-timelapse',), False)]:
            with self.subTest(flags=flags):
                with self.assertRaisesRegex(sender.SendError, 'Start was not confirmed'):
                    self.run_cli('--start', '--repeat', '--report', str(report), *flags)
                job = self.last_bridge.call_args_list[-1].args[2]
                self.assertIs(json.loads(job['payload'])['print']['timelapse'], expected)
                receipt = next(self.cache.glob('*.json'))
                self.assertIs(json.loads(receipt.read_text())['timelapse'], expected)
                self.assertIs(json.loads(report.read_text())['timelapse'], expected)

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

    def test_legacy_print_upload_refuses_different_existing_file(self):
        info=self.fixture()
        for size in (info['bytes'],info['bytes']+1):
            with self.subTest(size=size),mock.patch.object(sender,'ImplicitFTPS') as client:
                ftp=client.return_value.__enter__.return_value
                ftp.size.return_value=size
                ftp.retrbinary.side_effect=lambda command,callback:callback(b'different')
                with self.assertRaisesRegex(sender.SendError,'different contents'):
                    sender.upload_verified(self.path,info,{'ip':'printer.example','serial':'TEST_PRINTER','access_code':'TEST_CODE'})
                ftp.storbinary.assert_not_called()

    def test_p1s_upload_finishes_when_tls_peer_omits_close_notify(self):
        class DataSocket:
            def __init__(self): self.data=b''; self.closed=False; self.unwrapped=False
            def __enter__(self): return self
            def __exit__(self,*args): self.closed=True
            def sendall(self,data): self.data+=data
            def unwrap(self):
                self.unwrapped=True
                if omit_notify: raise TimeoutError('peer did not send close_notify')
        for serial,omit_notify in [('01P_TEST',True),('H2D_TEST',False)]:
            with self.subTest(serial=serial):
                data=DataSocket()
                ftp=sender.ImplicitFTPS.__new__(sender.ImplicitFTPS)
                ftp.host=serial;ftp.voidcmd=mock.Mock();ftp.transfercmd=mock.Mock(return_value=data)
                def completed():
                    self.assertTrue(data.closed)
                    self.assertEqual(data.data,b'fixture bytes')
                    return '226 Transfer complete'
                ftp.voidresp=completed
                with mock.patch.object(sender.ftplib,'_SSLSocket',DataSocket):
                    self.assertEqual(ftp.storbinary('STOR fixture',io.BytesIO(b'fixture bytes'),4),'226 Transfer complete')
                self.assertEqual(data.unwrapped,not omit_notify)

if __name__ == '__main__': unittest.main()
