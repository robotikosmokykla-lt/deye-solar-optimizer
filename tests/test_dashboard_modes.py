import http.client
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from http.server import ThreadingHTTPServer

from dashboard_modes import ModeController, mode_status
from dashboard_server import Handler


class QuietHandler(Handler):
    def log_message(self,*args):pass


class DashboardModeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.raw={'site':{'timezone':'UTC'},'logging':{'state_db':str(Path(self.temp.name)/'state.db')},
                  'analytics':{'mode_control_enabled':True,'allowed_hosts':[]},
                  'control':{'dry_run':False},'charge_temperature':{'enabled':True},
                  'house':{'mode':'occupied','storage_soc_pct':60}}
        self.write_confirmed('occupied')
        self.server=ThreadingHTTPServer(('127.0.0.1',0),QuietHandler)
        self.server.raw=self.raw;self.server.csrf_token='test-csrf';self.server.env_path=None
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.addCleanup(self.close_server)

    def close_server(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)

    def write_confirmed(self,mode):
        state={'applied':json.dumps({'house_mode':mode}),'confirmed_at':'2026-10-27T00:00:00Z',
               'verified':{'gridChargeAmpere':10,'timeUseSettingItems':[
                   {'time':'07:00','soc':50},{'time':'21:00','soc':50}]}}
        (Path(self.temp.name)/'season-state.json').write_text(json.dumps(state))

    def request(self,path='/api/mode',method='GET',body=None,headers=None):
        conn=http.client.HTTPConnection(*self.server.server_address,timeout=3)
        base={'Host':f'127.0.0.1:{self.server.server_port}'}
        if method=='POST':base.update({'X-CSRF-Token':'test-csrf','Content-Type':'application/json'})
        base.update(headers or {})
        conn.request(method,path,body,base);response=conn.getresponse()
        status=response.status;data=json.loads(response.read());conn.close();return status,data

    def remote(self,payload):
        if payload['op']=='status':return {**mode_status(self.raw),'job':None}
        return {'status':202,'job':{'mode':payload['mode'],'state':'applying','id':'job1'}}

    @patch('dashboard_server.broker_request')
    def test_get_exposes_confirmed_mode_and_csrf_without_mutation(self,broker):
        broker.side_effect=self.remote
        status,data=self.request()
        self.assertEqual(status,200);self.assertEqual(data['confirmed_mode'],'occupied')
        self.assertEqual(data['csrf_token'],'test-csrf');self.assertTrue(data['available'])
        broker.assert_called_once_with({'op':'status'})

    @patch('dashboard_server.broker_request')
    def test_switch_queues_fixed_mode_and_keeps_old_confirmation(self,broker):
        broker.side_effect=self.remote
        status,data=self.request(method='POST',body=json.dumps({'mode':'winter-away'}))
        self.assertEqual(status,202);self.assertEqual(data['job']['state'],'applying')
        self.assertEqual(broker.call_args.args[0],{'op':'switch','mode':'winter-away'})
        self.assertEqual(mode_status(self.raw)['confirmed_mode'],'occupied')

    @patch('dashboard_server.broker_request')
    def test_cross_origin_null_origin_and_dns_rebinding_requests_are_rejected(self,broker):
        for headers in [{'Origin':'https://attacker.invalid'}, {'Origin':'null'},
                        {'Host':'attacker.invalid'}, {'Sec-Fetch-Site':'cross-site'},
                        {'X-CSRF-Token':''}, {'X-CSRF-Token':'wrong'}]:
            status,_=self.request(method='POST',body='{"mode":"winter-away"}',headers=headers)
            self.assertEqual(status,403)
        broker.assert_not_called()

    @patch('dashboard_server.broker_request')
    def test_invalid_payloads_cannot_reach_broker(self,broker):
        for body in ['{"mode":"save"}','{"mode":"occupied","env":"/tmp/evil"}',
                     '{"mode":["occupied"]}','[]','not json','x'*257]:
            status,_=self.request(method='POST',body=body);self.assertEqual(status,400)
        broker.assert_not_called()

    @patch('dashboard_server.broker_request')
    def test_disabled_dry_run_and_unprotected_modes_cannot_switch(self,broker):
        self.raw['analytics']['mode_control_enabled']=False
        status,_=self.request(method='POST',body='{"mode":"occupied"}');self.assertEqual(status,503)
        self.raw['analytics']['mode_control_enabled']=True;self.raw['control']['dry_run']=True
        status,_=self.request(method='POST',body='{"mode":"occupied"}');self.assertEqual(status,503)
        self.raw['control']['dry_run']=False;self.raw['charge_temperature']['enabled']=False
        status,_=self.request(method='POST',body='{"mode":"occupied"}');self.assertEqual(status,503)
        broker.assert_not_called()

    @patch('dashboard_server.broker_request',side_effect=OSError('offline'))
    def test_unavailable_broker_disables_controls_without_hiding_current_mode(self,broker):
        status,data=self.request();self.assertEqual(status,200)
        self.assertFalse(data['available']);self.assertEqual(data['confirmed_mode'],'occupied')

    @patch('dashboard_server.broker_request')
    def test_updated_config_is_loaded_on_each_request(self,broker):
        broker.side_effect=self.remote;self.server.env_path='test.env'
        with patch('dashboard_server.load_config',return_value=self.raw) as load:
            self.request();self.raw['house']['mode']='winter-away'
            status,data=self.request()
        self.assertEqual(load.call_count,2)
        self.assertEqual(data['requested_mode'],'winter-away')
        self.assertEqual(data['confirmed_mode'],'occupied')

    @patch('dashboard_server.broker_request')
    def test_explicit_private_proxy_hostname_is_supported(self,broker):
        broker.side_effect=self.remote;self.raw['analytics']['allowed_hosts']=['solar.private.example']
        status,_=self.request(method='POST',body='{"mode":"winter-away"}',
                             headers={'Host':'solar.private.example','Origin':'https://solar.private.example'})
        self.assertEqual(status,202)

    @patch('dashboard_modes.load_config')
    @patch('dashboard_modes.threading.Thread')
    def test_broker_coalesces_duplicates_and_refuses_concurrent_other_mode(self,thread,load):
        load.return_value=self.raw;controller=ModeController()
        first=controller.start('winter-away');second=controller.start('winter-away')
        self.assertEqual(first['job']['id'],second['job']['id']);self.assertEqual(thread.call_count,1)
        self.assertEqual(controller.start('occupied')['status'],409)

    @patch('dashboard_modes.load_config')
    @patch('dashboard_modes.threading.Thread')
    def test_already_confirmed_mode_is_a_noop(self,thread,load):
        load.return_value=self.raw
        self.assertEqual(ModeController().start('occupied')['status'],200)
        thread.assert_not_called()

    @patch('dashboard_modes.load_config')
    def test_broker_only_accepts_two_modes(self,load):
        controller=ModeController()
        for mode in ['save','occupied; reboot','../other.py','--env']:
            self.assertEqual(controller.start(mode)['status'],400)
        load.assert_not_called()

    @patch('dashboard_modes.load_config')
    @patch('dashboard_modes.subprocess.run')
    def test_worker_requires_successful_exit_and_matching_native_confirmation(self,run,load):
        load.return_value=self.raw;run.return_value=subprocess.CompletedProcess([],0,'','')
        controller=ModeController('fixed.env');controller.job={'id':'job1','mode':'winter-away','state':'applying'}
        controller.run('job1','winter-away')
        self.assertEqual(controller.job['state'],'failed')
        argv=run.call_args.args[0]
        self.assertEqual(argv[-3:],['winter-away','--env','fixed.env'])
        self.assertNotIn('shell',run.call_args.kwargs)
        self.write_confirmed('winter-away');controller.run('job1','winter-away')
        self.assertEqual(controller.job['state'],'confirmed')

    @patch('dashboard_modes.load_config')
    @patch('dashboard_modes.subprocess.run',side_effect=subprocess.TimeoutExpired('mode',480))
    def test_timeout_is_never_reported_as_confirmed(self,run,load):
        load.return_value=self.raw;controller=ModeController()
        controller.job={'id':'job1','mode':'winter-away','state':'applying'}
        controller.run('job1','winter-away');self.assertEqual(controller.job['state'],'failed')
