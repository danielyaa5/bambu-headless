# SPDX-License-Identifier: AGPL-3.0-only
"""Public CLI and transfer tests; synthetic data and no printer connections."""
from contextlib import contextmanager, nullcontext, redirect_stdout, redirect_stderr
import argparse
import ftplib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import bambu
import bambu_files as files
import send_h2d_print as core


class FakeFTP:
    def __init__(self, contents=None, *, mlsd=True):
        self.contents = dict(contents or {})
        self.directory = '/'
        self.supports_mlsd = mlsd
        self.transfers = []
        self.fail_download = False
        self.corrupt_upload = False
        self.binary = True
    def cwd(self, directory):
        self.directory = directory
    def voidcmd(self, command):
        self.binary = command == 'TYPE I'
    def mlsd(self):
        self.binary = False
        if not self.supports_mlsd:
            raise ftplib.error_perm('500 Unsupported')
        return [(Path(p).name, {'type':'file', 'size':str(len(v)), 'modify':'20260101000000'})
                for p,v in self.contents.items() if str(Path(p).parent) == self.directory]
    def nlst(self):
        self.binary = False
        return [p for p in self.contents if str(Path(p).parent) == self.directory]
    def size(self, path):
        if not self.binary:
            raise ftplib.error_perm('550 SIZE disallowed in ASCII mode')
        return len(self.contents[path])
    def retrbinary(self, command, callback):
        self.binary = True
        data = self.contents[command[5:]]
        callback(data[:2])
        if self.fail_download:
            raise ConnectionResetError()
        callback(data[2:])
    def storbinary(self, command, source):
        path = command[5:]
        self.transfers.append(path)
        self.contents[path] = source.read() + (b'corrupt' if self.corrupt_upload else b'')
    def rename(self, source, destination):
        self.contents[destination] = self.contents.pop(source)
    def delete(self, path):
        self.contents.pop(path)


class FileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'part.gcode.3mf'
        self.path.write_bytes(b'synthetic transfer fixture')
        self.info = {'bytes': self.path.stat().st_size, 'md5':hashlib.md5(self.path.read_bytes()).hexdigest()}
    @contextmanager
    def connection(self, ftp):
        with mock.patch.object(files, 'connect', return_value=nullcontext(ftp)):
            yield
    def test_paths_reject_injection_traversal_and_wildcards(self):
        for path in ['', '..', '/a/../b', '/a\r\nDELE /b', '/a\x00b', '/a\\b', '/a*', '/a?', '/[a]', '/']:
            with self.subTest(path=path), self.assertRaises(core.SendError):
                files.remote_path(path)
        self.assertEqual(files.remote_path('cache/a file.gcode.3mf'), '/cache/a file.gcode.3mf')
    def test_mutations_refuse_non_print_files(self):
        for path in ['/config.json', '/timelapse.mp4', '/folder', '/foo.gcode']:
            with self.assertRaises(core.SendError): files.print_file_path(path)
    def test_fallback_listing_restores_binary_mode_and_subdirectories(self):
        ftp = FakeFTP({'/cache/file.gcode.3mf':b'abc','/other.gcode.3mf':b'x'},mlsd=False)
        self.assertEqual(files.entries(ftp,'/cache'), [{'name':'file.gcode.3mf','path':'/cache/file.gcode.3mf','type':'file','bytes':3,'modified_utc':None}])
    def test_upload_reuses_identical_and_refuses_different_destination(self):
        ftp = FakeFTP({'/part.gcode.3mf':self.path.read_bytes()})
        with self.connection(ftp):
            self.assertTrue(files.upload({},self.path,self.info,'/part.gcode.3mf')['reused'])
            ftp.contents['/part.gcode.3mf'] = b'different'
            with self.assertRaisesRegex(core.SendError,'different contents'):
                files.upload({},self.path,self.info,'/part.gcode.3mf')
        self.assertEqual(ftp.transfers,[])
    def test_new_upload_verifies_full_readback(self):
        ftp = FakeFTP()
        with self.connection(ftp):
            result=files.upload({},self.path,self.info,'/part.gcode.3mf')
        self.assertTrue(result['verified'])
        self.assertFalse(result['reused'])
        self.assertEqual(ftp.contents['/part.gcode.3mf'],self.path.read_bytes())
    def test_changed_local_file_or_corrupt_upload_is_rejected(self):
        ftp=FakeFTP()
        with self.connection(ftp):
            self.path.write_bytes(b'changed')
            with self.assertRaisesRegex(core.SendError,'changed'):
                files.upload({},self.path,self.info,'/part.gcode.3mf')
            self.assertEqual(ftp.transfers,[])
            self.info={'bytes':7,'md5':hashlib.md5(b'changed').hexdigest()}
            ftp.corrupt_upload=True
            with self.assertRaisesRegex(core.SendError,'mismatch'):
                files.upload({},self.path,self.info,'/part.gcode.3mf')
    def test_download_publishes_complete_file_and_refuses_overwrite(self):
        ftp=FakeFTP({'/remote.gcode.3mf':b'abcdef'})
        destination=self.root/'download.gcode.3mf'
        with self.connection(ftp):
            result=files.download({},'/remote.gcode.3mf',destination)
            self.assertEqual(destination.read_bytes(),b'abcdef')
            self.assertEqual(result['bytes'],6)
            with self.assertRaisesRegex(core.SendError,'exists'):
                files.download({},'/remote.gcode.3mf',destination)
    def test_interrupted_download_leaves_no_partial_file(self):
        ftp=FakeFTP({'/remote.gcode.3mf':b'abcdef'})
        ftp.fail_download=True
        destination=self.root/'download.gcode.3mf'
        with self.connection(ftp), self.assertRaises(ConnectionResetError):
            files.download({},'/remote.gcode.3mf',destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob('.bambu-download-*')),[])
    def test_download_wont_replace_concurrently_created_destination(self):
        ftp=FakeFTP({'/remote.gcode.3mf':b'abcdef'})
        destination=self.root/'download.gcode.3mf'
        real_link=files.os.link
        def race(source,target):
            destination.write_bytes(b'keep me')
            return real_link(source,target)
        with self.connection(ftp), mock.patch.object(files.os,'link',side_effect=race), self.assertRaises(FileExistsError):
            files.download({},'/remote.gcode.3mf',destination)
        self.assertEqual(destination.read_bytes(),b'keep me')
    def test_rename_preserves_contents_refuses_overwrite_and_delete_verifies(self):
        ftp=FakeFTP({'/a.gcode.3mf':b'abc','/occupied.gcode.3mf':b'xyz'})
        with self.connection(ftp):
            with self.assertRaisesRegex(core.SendError,'already exists'):
                files.rename({},'/a.gcode.3mf','/occupied.gcode.3mf')
            files.rename({},'/a.gcode.3mf','/b.gcode.3mf')
            self.assertEqual(ftp.contents['/b.gcode.3mf'],b'abc')
            self.assertNotIn('/a.gcode.3mf',ftp.contents)
            files.delete({},'/b.gcode.3mf')
            self.assertEqual(ftp.contents,{'/occupied.gcode.3mf':b'xyz'})


class CLITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
    def status(self,state='RUNNING',job='fixture.gcode'):
        return {'gcode_state':state,'subtask_name':job,'print_error':0,'mc_percent':30}
    def args(self,**kwargs):
        defaults=dict(interval=1,timeout=60,count=0,job=None,until_done=False,json=True,report=None)
        return argparse.Namespace(**(defaults|kwargs))
    def test_saved_printers_unions_both_code_maps_without_leaking_values(self):
        root=Path(self.temp.name)
        (root/'BambuStudio.conf').write_text(json.dumps({'access_code':{'TEST_A':'SECRET_A','TEST_B':''},'user_access_code':{'TEST_B':'SECRET_B'}}))
        with mock.patch.object(core,'CONFIG',root):
            result=core.saved_printers()
            self.assertEqual([p['serial'] for p in result],['TEST_A','TEST_B'])
            self.assertNotIn('SECRET',json.dumps(result))
    def test_control_and_delete_require_explicit_yes_before_credentials(self):
        with mock.patch.object(core,'load_connection') as load:
            for argv in [['cancel','--job','fixture','--host','printer.example'],['files','delete','/part.gcode.3mf','--host','printer.example']]:
                with self.assertRaisesRegex(core.SendError,'--yes'): bambu.main(argv)
            load.assert_not_called()
    def test_storage_mutation_refuses_busy_printer(self):
        with mock.patch.object(core,'load_connection',return_value={}),mock.patch.object(core,'printer_lock',return_value=nullcontext()),mock.patch.object(bambu,'live_status',return_value=self.status()),mock.patch.object(files,'delete') as delete:
            with self.assertRaisesRegex(core.SendError,'busy'):
                bambu.main(['files','delete','/part.gcode.3mf','--yes','--host','printer.example'])
            delete.assert_not_called()
    def test_control_requires_matching_job_valid_state_and_resolved_errors(self):
        for command,state,job in [('pause','PAUSE','fixture.gcode'),('cancel','FINISH','fixture.gcode'),('resume','PAUSE','other')]:
            with self.assertRaises(core.SendError): bambu.control_preflight(self.status(state),command,job)
        for error in (123,None):
            with self.assertRaises(core.SendError): bambu.control_preflight(self.status('PAUSE')|{'print_error':error},'resume','fixture.gcode')
        bambu.control_preflight(self.status(),'pause','fixture.gcode')
    def test_control_needs_correlated_ack_and_post_dispatch_matching_state(self):
        ack={'command':'pause','sequence_id':'42','result':'SUCCESS'}
        change={'command':'push_status','gcode_state':'PAUSE','subtask_name':'fixture.gcode'}
        dispatch={'event':'dispatching'}
        for events in [[dispatch,ack],[change,dispatch,ack],[dispatch,ack,change|{'subtask_name':'other'}],[dispatch,ack|{'sequence_id':'43'},change],[dispatch,ack|{'err_code':123},change]]:
            self.assertFalse(bambu.confirmed_control(events,'pause','42','fixture.gcode'))
        self.assertTrue(bambu.confirmed_control([dispatch,change,ack],'pause','42','fixture.gcode'))
    def test_cancel_does_not_claim_natural_finish_as_confirmed_cancel(self):
        events=[{'event':'dispatching'},{'command':'stop','sequence_id':'42','result':'SUCCESS'},{'command':'push_status','gcode_state':'FINISH','subtask_name':'fixture.gcode'}]
        self.assertFalse(bambu.confirmed_control(events,'cancel','42','fixture.gcode'))
    def run_watch(self, statuses, **kwargs):
        with mock.patch.object(core,'printer_lock',side_effect=lambda _:nullcontext()),mock.patch.object(bambu,'live_status',side_effect=statuses),mock.patch.object(bambu.time,'sleep'),redirect_stdout(io.StringIO()) as out:
            rc=bambu.monitor(self.args(**kwargs),{})
        return rc,[json.loads(line) for line in out.getvalue().splitlines()]
    def test_watch_follows_one_job_to_completion(self):
        rc,events=self.run_watch([self.status(),self.status('FINISH')],until_done=True)
        self.assertEqual(rc,0)
        self.assertEqual(events[-1]['event'],'finished')
        self.assertTrue(events[-1]['success'])
    def test_watch_failed_job_returns_failure(self):
        rc,events=self.run_watch([self.status(),self.status('FAILED')],until_done=True)
        self.assertEqual(rc,1)
    def test_watch_does_not_accept_old_finish_or_replacement_job(self):
        with self.assertRaisesRegex(core.SendError,'No active job'):
            self.run_watch([self.status('FINISH')],until_done=True)
        with self.assertRaisesRegex(core.SendError,'job changed'):
            self.run_watch([self.status(),self.status(job='other')],until_done=True)
    def test_watch_count_limit_does_not_claim_completion(self):
        rc,events=self.run_watch([self.status()],until_done=True,count=1)
        self.assertEqual(rc,3)
        self.assertEqual(events[-1]['event'],'sample_limit')
    def test_watch_waits_for_another_local_command_instead_of_exiting(self):
        with mock.patch.object(core,'printer_lock',side_effect=[core.PrinterBusy(),nullcontext()]),mock.patch.object(bambu,'live_status',return_value=self.status()),mock.patch.object(bambu.time,'sleep'),redirect_stdout(io.StringIO()) as out:
            self.assertEqual(bambu.monitor(self.args(count=1),{}),0)
        events=[json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([event['event'] for event in events],['waiting','status'])
    def test_check_and_print_share_legacy_path(self):
        for verb,start in [('check',False),('print',True)]:
            with mock.patch.object(core,'main',return_value=0) as legacy:
                bambu.main([verb,'fixture.gcode.3mf','--host','printer.example'])
                self.assertEqual('--start' in legacy.call_args.args[0],start)
    def test_file_listing_json_is_parseable_and_terminal_text_escaped(self):
        listing=[{'path':'/file.gcode.3mf','name':'file.gcode.3mf','bytes':3,'type':'file'}]
        with mock.patch.object(core,'load_connection',return_value={}),mock.patch.object(core,'printer_lock',return_value=nullcontext()),mock.patch.object(files,'list_files',return_value=listing),redirect_stdout(io.StringIO()) as out:
            bambu.main(['files','list','--host','printer.example','--json'])
            self.assertEqual(json.loads(out.getvalue()),{'files':listing})
        self.assertNotIn('\x1b',bambu.status_line({'state':'RUNNING','job':'job\x1b[2J'}))


if __name__ == '__main__':
    unittest.main()
