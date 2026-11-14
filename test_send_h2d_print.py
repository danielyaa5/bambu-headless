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
    def fixture(self, mapping='1', checksum=None, material='PLA', model='Bambu Lab H2D', diameter='0.4', plate_model=None):
        gcode = b'; validation fixture only\n'
        with zipfile.ZipFile(self.path, 'w') as z:
            z.writestr('Metadata/project_settings.config', json.dumps({'printer_model':model,'curr_bed_type':'High Temp Plate','layer_height':'0.2'}))
            z.writestr('Metadata/plate_1.gcode', gcode)
            z.writestr('Metadata/plate_1.gcode.md5', checksum or hashlib.md5(gcode).hexdigest())
            model_meta=f'<metadata key="printer_model_id" value="{plate_model}"/>' if plate_model is not None else ''
            z.writestr('Metadata/slice_info.config', f'<config><plate>{model_meta}<metadata key="index" value="1"/><metadata key="filament_maps" value="{mapping}"/><metadata key="prediction" value="10"/><metadata key="weight" value="1"/><filament id="1" type="{material}" nozzle_diameter="{diameter}" color="#FFFFFF"/></plate></config>')
        return sender.inspect_file(self.path)
    def status(self, state='FINISH', material='PLA', diameter=.4):
        return {'gcode_state':state,'model_id':'O1D',
                'trays':[{'ams_id':'0','id':'1','tray_type':material}],
                'nozzles':[{'id':0,'diameter':diameter},{'id':1,'diameter':diameter}]}
    def test_valid_left_nozzle_file_and_a2(self):
        sender.preflight(self.status(), self.fixture(mapping='1'), 1)
    def test_valid_right_nozzle_file_and_b2(self):
        info = self.fixture(mapping='2')
        status = {'gcode_state':'FINISH','model_id':'O1D',
                  'trays':[{'ams_id':'1','id':'1','tray_type':'PLA'}],
                  'nozzles':[{'id':0,'diameter':0.4},{'id':1,'diameter':0.4}]}
        sender.preflight(status, info, 5)
        payload = sender.print_payload(info, 5, '22000')['print']
        self.assertEqual(payload['ams_mapping2'], [{'ams_id':1,'slot_id':1}])
        self.assertEqual(payload['nozzle_mapping'], [0])
    def test_corrupted_embedded_gcode_rejected(self):
        with self.assertRaises(sender.SendError): self.fixture(checksum='0'*32)
    def test_invalid_or_mismatched_nozzle_rejected(self):
        for bad_mapping in ('3', '1 2', ''):
            with self.subTest(mapping=bad_mapping), self.assertRaises(sender.SendError):
                self.fixture(mapping=bad_mapping)
        with self.assertRaisesRegex(sender.SendError, 'different H2D nozzle'):
            sender.preflight(self.status(), self.fixture(mapping='2'), 1)
        b_status = {'gcode_state':'FINISH','model_id':'O1D',
                    'trays':[{'ams_id':'1','id':'1','tray_type':'PLA'}],
                    'nozzles':[{'id':0,'diameter':0.4},{'id':1,'diameter':0.4}]}
        with self.assertRaisesRegex(sender.SendError, 'different H2D nozzle'):
            sender.preflight(b_status, self.fixture(mapping='1'), 5)
    def test_record_params_builds_studio_local_print_record_schema(self):
        cfg_3mf = Path(self.temp.name) / 'config.3mf'
        right_info = self.fixture(mapping='2')
        sender.create_config_3mf(self.path, cfg_3mf)
        with zipfile.ZipFile(cfg_3mf) as z:
            self.assertIn('Metadata/slice_info.config', z.namelist())
            self.assertNotIn('Metadata/plate_1.gcode', z.namelist())
        b_status = {'gcode_state':'FINISH','model_id':'O1D',
                    'trays':[{'ams_id':'1','id':'1','tray_type':'PLA'}],
                    'nozzles':[{'id':0,'diameter':0.4},{'id':1,'diameter':0.4}]}
        right = sender.record_print_params(self.path, cfg_3mf, right_info, b_status, 5)
        self.assertEqual(json.loads(right['ams_mapping']), [5])
        self.assertEqual(json.loads(right['ams_mapping2']), [{'ams_id':1,'slot_id':1}])
        self.assertEqual(json.loads(right['ams_mapping_info'])[0]['nozzleId'], 0)
        self.assertEqual(json.loads(right['nozzle_mapping']), [0])
        left_info = self.fixture(mapping='1')
        left = sender.record_print_params(self.path, cfg_3mf, left_info, self.status(), 1)
        self.assertEqual(json.loads(left['ams_mapping']), [1])
        self.assertEqual(json.loads(left['ams_mapping2']), [{'ams_id':0,'slot_id':1}])
        self.assertEqual(json.loads(left['ams_mapping_info'])[0]['nozzleId'], 1)
        self.assertEqual(json.loads(left['nozzle_mapping']), [1])
        nozzles = json.loads(left['nozzles_info'])
        self.assertEqual([n['id'] for n in nozzles], [1, 0])
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
    def test_payload_uses_left_nozzle_and_a2(self):
        payload=sender.print_payload(self.fixture(),1,'22000')['print']
        self.assertEqual(payload['ams_mapping2'],[{'ams_id':0,'slot_id':1}])
        self.assertEqual(payload['nozzle_mapping'],[1])
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

    def test_external_spool_preflight_and_payload_h2d_right_nozzle(self):
        info = self.fixture(mapping='2', material='TPU')
        status = {'gcode_state':'FINISH','model_id':'O1D',
                  'trays':[{'id':'255','ams_id':'255','tray_type':'TPU','tray_color':'F98C36FF','tray_info_idx':'GFU99'}],
                  'nozzles':[{'id':0,'diameter':0.4},{'id':1,'diameter':0.4}]}
        sender.preflight(status, info, 255)
        payload = sender.print_payload(info, 255, '22000')['print']
        self.assertTrue(payload['use_ams'])
        self.assertEqual(payload['ams_mapping'], [255])
        self.assertEqual(payload['ams_mapping2'], [{'ams_id':255,'slot_id':0}])
        self.assertEqual(payload['nozzle_mapping'], [0])
        cfg_3mf = Path(self.temp.name) / 'config.3mf'
        sender.create_config_3mf(self.path, cfg_3mf)
        r = sender.record_print_params(self.path, cfg_3mf, info, status, 255)
        self.assertTrue(r['use_ams'])
        self.assertEqual(json.loads(r['ams_mapping']), [255])
        self.assertEqual(json.loads(r['ams_mapping2']), [{'ams_id':255,'slot_id':0}])
        mapping_info = json.loads(r['ams_mapping_info'])
        self.assertEqual(mapping_info[0]['ams'], 255)
        self.assertEqual(mapping_info[0]['nozzleId'], 0)
        self.assertEqual(mapping_info[0]['filamentType'], 'TPU')
        self.assertEqual(mapping_info[0]['filamentId'], 'GFU99')

    def test_external_spool_preflight_and_payload_h2d_left_nozzle(self):
        info = self.fixture(mapping='1', material='PETG')
        status = {'gcode_state':'FINISH','model_id':'O1D',
                  'trays':[{'id':'254','ams_id':'255','tray_type':'PETG','tray_color':'FFFFFFFF','tray_info_idx':'GFG99'}],
                  'nozzles':[{'id':0,'diameter':0.4},{'id':1,'diameter':0.4}]}
        sender.preflight(status, info, 254)
        payload = sender.print_payload(info, 254, '22000')['print']
        self.assertTrue(payload['use_ams'])
        self.assertEqual(payload['ams_mapping'], [254])
        self.assertEqual(payload['ams_mapping2'], [{'ams_id':254,'slot_id':0}])
        self.assertEqual(payload['nozzle_mapping'], [1])
        cfg_3mf = Path(self.temp.name) / 'config.3mf'
        sender.create_config_3mf(self.path, cfg_3mf)
        r = sender.record_print_params(self.path, cfg_3mf, info, status, 254)
        self.assertTrue(r['use_ams'])
        self.assertEqual(json.loads(r['ams_mapping']), [254])
        self.assertEqual(json.loads(r['ams_mapping2']), [{'ams_id':254,'slot_id':0}])
        mapping_info = json.loads(r['ams_mapping_info'])
        self.assertEqual(mapping_info[0]['ams'], 254)
        self.assertEqual(mapping_info[0]['nozzleId'], 1)

    def test_external_spool_rejects_wrong_nozzle_and_wrong_material(self):
        right_info = self.fixture(mapping='2', material='TPU')
        left_info = self.fixture(mapping='1', material='PETG')
        status = {'gcode_state':'FINISH','model_id':'O1D',
                  'trays':[{'id':'254','ams_id':'255','tray_type':'PETG'},{'id':'255','ams_id':'255','tray_type':'TPU'}],
                  'nozzles':[{'id':0,'diameter':0.4},{'id':1,'diameter':0.4}]}
        with self.assertRaisesRegex(sender.SendError, 'different H2D nozzle'):
            sender.preflight(status, right_info, 254)
        with self.assertRaisesRegex(sender.SendError, 'different H2D nozzle'):
            sender.preflight(status, left_info, 255)
        wrong_mat_status = {'gcode_state':'FINISH','model_id':'O1D',
                            'trays':[{'id':'255','ams_id':'255','tray_type':'PLA'}],
                            'nozzles':[{'id':0,'diameter':0.4},{'id':1,'diameter':0.4}]}
        with self.assertRaisesRegex(sender.SendError, 'Selected external spool is missing or its material differs'):
            sender.preflight(wrong_mat_status, right_info, 255)

    def test_external_spool_p1s(self):
        info = self.fixture(model='Bambu Lab P1S', mapping='1', plate_model='C12', material='PETG')
        status = {'gcode_state':'FINISH','model_id':'C12','nozzle_diameter':'0.4',
                  'trays':[{'id':'254','ams_id':'255','tray_type':'PETG'}]}
        sender.preflight(status, info, 254)
        payload = sender.print_payload(info, 254, '42')['print']
        self.assertFalse(payload['use_ams'])
        self.assertEqual(payload['ams_mapping'], [254])
        self.assertNotIn('ams_mapping2', payload)

    def test_parse_tray_spec_and_labels(self):
        self.assertEqual(sender.parse_tray_spec('ext', hw_nozzle_id=0, is_h2d=True), 255)
        self.assertEqual(sender.parse_tray_spec('external', hw_nozzle_id=1, is_h2d=True), 254)
        self.assertEqual(sender.parse_tray_spec('ext', is_h2d=False), 254)
        self.assertEqual(sender.parse_tray_spec('ext-r'), 255)
        self.assertEqual(sender.parse_tray_spec('ext-l'), 254)
        self.assertEqual(sender.parse_tray_spec('254'), 254)
        self.assertEqual(sender.parse_tray_spec('255'), 255)
        self.assertEqual(sender.parse_tray_spec('A2'), 1)
        self.assertEqual(sender.slot_label(255), 'Ext-R')
        self.assertEqual(sender.slot_label(254), 'Ext-L')
        self.assertEqual(sender.slot_label(254, is_h2d=False), 'Ext')
        with self.assertRaises(sender.SendError):
            sender.parse_tray_spec('invalid')

    def test_cli_no_ams_defaults_and_guards(self):
        self.fixture(mapping='2', material='TPU')
        connection = {'serial': 'TEST_PRINTER', 'ip': 'printer.example'}
        status = {'gcode_state':'FINISH','model_id':'O1D',
                  'trays':[{'id':'255','ams_id':'255','tray_type':'TPU','tray_color':'F98C36FF'}],
                  'nozzles':[{'id':0,'diameter':0.4},{'id':1,'diameter':0.4}],
                  'command':'push_status'}
        def fake_bridge(c, action, job=None):
            return [status], 0
        with mock.patch.object(sender, 'load_connection', return_value=connection), \
             mock.patch.object(sender, 'bridge_run', side_effect=fake_bridge), \
             mock.patch.object(sender.Path, 'home', return_value=Path(self.temp.name)), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(sender.main([str(self.path), '--host', 'printer.example', '--no-ams']), 0)
            self.assertEqual(sender.main([str(self.path), '--host', 'printer.example', '--external']), 0)
            self.assertEqual(sender.main([str(self.path), '--host', 'printer.example', '--tray', 'ext']), 0)
            with self.assertRaisesRegex(sender.SendError, '--no-ams was specified but tray'):
                sender.main([str(self.path), '--host', 'printer.example', '--no-ams', '--tray', '1'])

    # --- multi-filament plates (one --ams-map slot per filament) ---
    COLORS = ['#161616','#4D3324','#4F3F24','#847D48','#AE835B','#D3B7A7','#F72323','#E8DBB7']
    MAP = 'A3,A1,A2,B4,B2,B1,A4,B3'
    def multi_fixture(self, maps='1 1 1 2 2 2 1 2', ids=range(1, 9), diameter='0.40'):
        gcode = b'; validation fixture only\n'
        fil = ''.join(f'<filament id="{i}" type="PLA" nozzle_diameter="{diameter}" color="{self.COLORS[i-1]}" tray_info_idx="GFL99" used_g="{10+i}"/>' for i in ids)
        with zipfile.ZipFile(self.path, 'w') as z:
            z.writestr('Metadata/project_settings.config', json.dumps({'printer_model':'Bambu Lab H2D','curr_bed_type':'Textured PEI Plate','layer_height':'0.2'}))
            z.writestr('Metadata/plate_1.gcode', gcode)
            z.writestr('Metadata/plate_1.gcode.md5', hashlib.md5(gcode).hexdigest())
            z.writestr('Metadata/slice_info.config', f'<config><plate><metadata key="index" value="1"/><metadata key="filament_maps" value="{maps}"/><metadata key="prediction" value="10"/><metadata key="weight" value="1"/>{fil}</plate></config>')
        return sender.inspect_file(self.path)
    def multi_status(self, colors=None):
        colors = colors or dict(zip([2,0,1,7,5,4,3,6], [c[1:] for c in self.COLORS]))
        trays = [{'ams_id':str(t//4),'id':str(t%4),'tray_type':'PLA','tray_color':c+'FF','tray_info_idx':'GFL99'} for t,c in colors.items()]
        return {'gcode_state':'FINISH','model_id':'O1D','trays':trays,
                'nozzles':[{'id':0,'diameter':0.4,'type':'HS01'},{'id':1,'diameter':0.4,'type':'HS01'}]}
    def test_empty_plate_model_from_studio_cli_is_not_a_disagreement(self):
        self.assertEqual(self.fixture(plate_model='')['model_id'], 'O1D')
        with self.assertRaisesRegex(sender.SendError, 'disagree'):
            self.fixture(plate_model='C12')
    def test_multi_filament_inspect_maps_each_filament_to_its_nozzle(self):
        info = self.multi_fixture()
        self.assertEqual([f['hw_nozzle_id'] for f in info['filaments']], [1,1,1,0,0,0,1,0])
        self.assertEqual(info['filament_count'], 8)
        self.assertEqual(info['material'], 'MULTI')
        for bad in ('1 1 1 2 2 2 1', '1 1 1 2 2 2 1 3', ''):
            with self.subTest(maps=bad), self.assertRaises(sender.SendError):
                self.multi_fixture(maps=bad)
        with self.assertRaises(sender.SendError):
            self.multi_fixture(diameter='0.6')
    def test_parse_ams_map(self):
        self.assertEqual(sender.parse_ams_map(self.MAP), [2,0,1,7,5,4,3,6])
        self.assertEqual(sender.parse_ams_map(' 0, 15 '), [0,15])
        self.assertEqual([sender.slot_label(t) for t in sender.parse_ams_map(self.MAP)], self.MAP.split(','))
        for bad in ('E1', 'A5', '16', 'A1,A1', '', 'A1,,B2'):
            with self.subTest(bad=bad), self.assertRaises(sender.SendError):
                sender.parse_ams_map(bad)
    def test_multi_preflight_accepts_matching_slots(self):
        sender.preflight_multi(self.multi_status(), self.multi_fixture(), sender.parse_ams_map(self.MAP))
    def test_multi_preflight_refuses_wrong_side_colour_material_empty_count_and_busy(self):
        info, trays = self.multi_fixture(), sender.parse_ams_map(self.MAP)
        crossed = trays.copy(); crossed[3], crossed[6] = crossed[6], crossed[3]   # right-nozzle and left-nozzle filaments swapped
        with self.assertRaisesRegex(sender.SendError, 'feeds the'):
            sender.preflight_multi(self.multi_status(), info, crossed)
        browns = trays.copy(); browns[1], browns[2] = browns[2], browns[1]       # same nozzle, colours swapped
        with self.assertRaisesRegex(sender.SendError, 'reports'):
            sender.preflight_multi(self.multi_status(), info, browns)
        sender.preflight_multi(self.multi_status(), info, browns, allow_color_mismatch=True)
        status = self.multi_status(); status['trays'][0]['tray_type'] = 'PETG'    # A3, filament 1
        with self.assertRaisesRegex(sender.SendError, 'holds PETG'):
            sender.preflight_multi(status, info, trays)
        status = self.multi_status(); status['trays'] = status['trays'][1:]       # A3 empty
        with self.assertRaisesRegex(sender.SendError, 'empty'):
            sender.preflight_multi(status, info, trays)
        with self.assertRaisesRegex(sender.SendError, 'lists 7 slots'):
            sender.preflight_multi(self.multi_status(), info, trays[:7])
        for changes in ({'gcode_state':'RUNNING'}, {'model_id':'C12'}, {'nozzles':[{'id':0,'diameter':0.4}]}):
            with self.subTest(changes=changes), self.assertRaises(sender.SendError):
                sender.preflight_multi(self.multi_status() | changes, info, trays)
    def test_multi_payload_and_record_use_studio_per_filament_arrays(self):
        info, trays = self.multi_fixture(), sender.parse_ams_map(self.MAP)
        p = sender.print_payload(info, trays, '22000')['print']
        self.assertEqual(p['ams_mapping'], [2,0,1,7,5,4,3,6])
        self.assertEqual(p['ams_mapping2'][0], {'ams_id':0,'slot_id':2})
        self.assertEqual(p['ams_mapping2'][3], {'ams_id':1,'slot_id':3})
        self.assertEqual(p['nozzle_mapping'], [1,1,1,0,0,0,1,0])
        self.assertIs(p['timelapse'], True)
        cfg = Path(self.temp.name) / 'config.3mf'
        sender.create_config_3mf(self.path, cfg)
        r = sender.record_print_params(self.path, cfg, info, self.multi_status(), trays)
        self.assertEqual(json.loads(r['ams_mapping']), [2,0,1,7,5,4,3,6])
        infos = json.loads(r['ams_mapping_info'])
        self.assertEqual([i['nozzleId'] for i in infos], [1,1,1,0,0,0,1,0])
        self.assertEqual([i['ams'] for i in infos], [2,0,1,7,5,4,3,6])
        self.assertEqual((infos[0]['sourceColor'], infos[0]['targetColor'], infos[6]['sourceColor']), ('161616FF','161616FF','F72323FF'))
        self.assertEqual(json.loads(r['nozzle_mapping']), [1,1,1,0,0,0,1,0])
        self.assertEqual([n['id'] for n in json.loads(r['nozzles_info'])], [1,0])
    def test_unused_project_filament_gets_studio_placeholders(self):
        info = self.multi_fixture(maps='1 2 2', ids=[1, 3])
        self.assertEqual(info['filament_count'], 3)
        p = sender.print_payload(info, sender.parse_ams_map('A1,B1'), '22000')['print']
        self.assertEqual(p['ams_mapping'], [0,-1,4])
        self.assertEqual(p['ams_mapping2'][1], {'ams_id':255,'slot_id':255})
        self.assertEqual(p['nozzle_mapping'], [1,0,0])
    def test_multi_filament_needs_ams_map_before_any_connection(self):
        self.multi_fixture()
        with mock.patch.object(sender, 'load_connection') as conn:
            with self.assertRaisesRegex(sender.SendError, '--ams-map'):
                sender.main([str(self.path), '--host', 'printer.example'])
            with self.assertRaisesRegex(sender.SendError, 'repeats'):
                sender.main([str(self.path), '--host', 'printer.example', '--ams-map', 'B3,B3,B2,A4,A2,A1,B4,A3'])
        conn.assert_not_called()

    def test_multi_filament_with_external_spool(self):
        info = self.multi_fixture()
        trays = sender.parse_ams_map('ext-l,B1,B2,A4,A2,A1,B4,ext-r', filaments=info['filaments'])
        self.assertEqual(trays[0], 254)
        self.assertEqual(trays[7], 255)
        p = sender.print_payload(info, trays, '22000')['print']
        self.assertTrue(p['use_ams'])
        self.assertEqual(p['ams_mapping2'][0], {'ams_id':254,'slot_id':0})
        self.assertEqual(p['ams_mapping2'][7], {'ams_id':255,'slot_id':0})
        pure_ext_info = self.multi_fixture(maps='1 2', ids=[1, 2])
        pure_ext_trays = sender.parse_ams_map('ext-l,ext-r', filaments=pure_ext_info['filaments'])
        pure_p = sender.print_payload(pure_ext_info, pure_ext_trays, '22000')['print']
        self.assertTrue(pure_p['use_ams'])
        self.assertEqual(pure_p['ams_mapping'], [254, 255])
        self.assertEqual(pure_p['ams_mapping2'], [{'ams_id':254,'slot_id':0},{'ams_id':255,'slot_id':0}])

    def test_external_spool_ams_mapping2_never_collides_with_unmapped_placeholder(self):
        # Firmware packs (ams_id << 8) | slot_id: 254/0 = 65024 (Ext-L), 255/0 = 65280 (Ext-R), 255/255 = 65535 (unmapped).
        gcode = b'; dual tpu pla fixture\n'
        with zipfile.ZipFile(self.path, 'w') as z:
            z.writestr('Metadata/project_settings.config', json.dumps({'printer_model':'Bambu Lab H2D','curr_bed_type':'Textured PEI Plate','layer_height':'0.16'}))
            z.writestr('Metadata/plate_1.gcode', gcode)
            z.writestr('Metadata/plate_1.gcode.md5', hashlib.md5(gcode).hexdigest())
            z.writestr('Metadata/slice_info.config',
                       '<config><plate><metadata key="printer_model_id" value="O1D"/><metadata key="index" value="1"/>'
                       '<metadata key="filament_maps" value="2 1 1"/><metadata key="prediction" value="100"/><metadata key="weight" value="40"/>'
                       '<filament id="1" type="TPU" nozzle_diameter="0.40" color="#F98C36" tray_info_idx="GFU99" used_g="38"/>'
                       '<filament id="2" type="PLA" nozzle_diameter="0.40" color="#F72323" tray_info_idx="GFL99" used_g="2"/></plate></config>')
        info = sender.inspect_file(self.path)
        trays = sender.parse_ams_map('ext-r,A3', filaments=info['filaments'])
        self.assertEqual(trays, [255, 2])
        status = {'gcode_state':'FINISH','model_id':'O1D',
                  'ams_units':[{'id':'0','info':'51001103'}],
                  'trays':[{'ams_id':'0','id':'2','tray_type':'PLA','tray_color':'F72323FF','tray_info_idx':'GFL99'},
                           {'ams_id':'255','id':'255','tray_type':'TPU','tray_color':'F98C36FF','tray_info_idx':'GFU99'}],
                  'nozzles':[{'id':0,'diameter':0.4},{'id':1,'diameter':0.4}]}
        sender.preflight_multi(status, info, trays)
        p = sender.print_payload(info, trays, '22000')['print']
        self.assertEqual(p['ams_mapping'], [255, 2, -1])
        self.assertEqual(p['ams_mapping2'], [{'ams_id':255,'slot_id':0}, {'ams_id':0,'slot_id':2}, {'ams_id':255,'slot_id':255}])
        self.assertEqual(p['nozzle_mapping'], [0, 1, 1])

    def test_h2d_ams_info_dynamically_resolves_fed_nozzle(self):
        left_info = self.fixture(mapping='1', material='PLA')
        right_info = self.fixture(mapping='2', material='PLA')
        base_status = {'gcode_state':'FINISH','model_id':'O1D',
                       'trays':[{'ams_id':'0','id':'2','tray_type':'PLA','tray_color':'FFFFFFFF'}],
                       'nozzles':[{'id':0,'diameter':0.4},{'id':1,'diameter':0.4}]}
        sender.preflight(base_status | {'ams_units':[{'id':'0','info':'51001103'}]}, left_info, 2)
        with self.assertRaisesRegex(sender.SendError, 'different H2D nozzle'):
            sender.preflight(base_status | {'ams_units':[{'id':'0','info':'51001103'}]}, right_info, 2)
        sender.preflight(base_status | {'ams_units':[{'id':'0','info':'10001003'}]}, right_info, 2)
        with self.assertRaisesRegex(sender.SendError, 'different H2D nozzle'):
            sender.preflight(base_status | {'ams_units':[{'id':'0','info':'10001003'}]}, left_info, 2)

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

    def test_plate_telemetry_is_recorded_without_blocking_third_party_plates(self):
        self.fixture()
        report_path = Path(self.temp.name) / 'plate-report.json'
        status = dict(self.status(state='FINISH'), command='push_status',
                      plate={'base': 4, 'cali2d_id': '', 'cur_id': 'P0101', 'mat': 1, 'tar_id': ''},
                      buildplate_marker_detector=False)
        with mock.patch.object(sender, 'load_connection', return_value={'serial': 'TEST_PRINTER', 'ip': 'printer.example'}), \
             mock.patch.object(sender, 'bridge_run', return_value=([status], 0)), \
             mock.patch.object(sender.Path, 'home', return_value=Path(self.temp.name)), \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(sender.main([str(self.path), '--host', 'printer.example', '--report', str(report_path)]), 0)
        self.assertIn('Printer-reported plate: Textured PEI Plate (id P0101, mat 1, base 4); marker detector off', out.getvalue())
        self.assertIn('differs from reported textured_plate', out.getvalue())
        saved = json.loads(report_path.read_text())
        self.assertEqual(saved['preflight']['plate']['name'], 'Textured PEI Plate')
        self.assertIs(saved['preflight']['buildplate_marker_detector'], False)

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

    def test_load_connection_accepts_europe_and_other_global_regions(self):
        conf_dir = Path(self.temp.name) / 'config'
        conf_dir.mkdir(parents=True, exist_ok=True)
        conf_file = conf_dir / 'BambuStudio.conf'
        for region in ('Europe', 'North America', 'NorthAmerica', 'US', 'United States', 'Asia-Pacific', 'AsiaPacific', 'Other', ''):
            with self.subTest(region=region):
                conf_file.write_text(json.dumps({
                    'access_code': {'TEST_PRINTER': 'SECRET_123'},
                    'app': {'region': region, 'slicer_uuid': 'TEST_UUID'}
                }))
                with mock.patch.object(sender, 'CONFIG', conf_dir):
                    conn = sender.load_connection('printer.example', 'TEST_PRINTER')
                    self.assertEqual(conn['serial'], 'TEST_PRINTER')
                    self.assertEqual(conn['access_code'], 'SECRET_123')
                    self.assertEqual(conn['country'], 'US')
                    self.assertEqual(conn['slicer_uid'], 'TEST_UUID')

    def test_load_connection_rejects_unsupported_regions(self):
        conf_dir = Path(self.temp.name) / 'config'
        conf_dir.mkdir(parents=True, exist_ok=True)
        conf_file = conf_dir / 'BambuStudio.conf'
        for region in ('China', 'CN', 'Antarctica'):
            with self.subTest(region=region):
                conf_file.write_text(json.dumps({
                    'access_code': {'TEST_PRINTER': 'SECRET_123'},
                    'app': {'region': region, 'slicer_uuid': 'TEST_UUID'}
                }))
                with mock.patch.object(sender, 'CONFIG', conf_dir):
                    with self.assertRaisesRegex(sender.SendError, 'validated for North America and Europe cloud regions only'):
                        sender.load_connection('printer.example', 'TEST_PRINTER')

if __name__ == '__main__': unittest.main()
