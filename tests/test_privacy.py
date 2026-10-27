import subprocess
import tempfile
import unittest
from pathlib import Path

from config_loader import redact_env_lines


class PrivacyTests(unittest.TestCase):
    def test_redacted_export_removes_credentials_identifiers_and_private_paths(self):
        private = {
            'DEYE_APP_SECRET': 'synthetic-secret-123', 'DEYE_INVERTER_SN': 'SYNTHETIC-SERIAL-123',
            'DEYE_STATION_ID': '99999999', 'SITE_LATITUDE': '1.234567',
            'SITE_LONGITUDE': '2.345678', 'HOUSE_WEATHER_STATION': 'synthetic-station',
            'ANALYTICS_ALLOWED_HOSTS': 'private.example.invalid',
            'LOGGING_BUNDLE_DIR': '/tmp/private-test-export', 'CUSTOM_API_TOKEN': 'synthetic-token-123',
        }
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'deye.env'
            p.write_text('\n'.join(f'{k}="{v}"' for k,v in private.items())+'\nBATTERY_EFFECTIVE_KWH=15\n')
            redacted=redact_env_lines(p)
        for value in private.values():self.assertNotIn(value,redacted)
        self.assertIn('BATTERY_EFFECTIVE_KWH=15',redacted)

    def test_runtime_files_are_ignored_and_public_examples_remain_visible(self):
        root=Path(__file__).resolve().parents[1]
        names=['deye.env','deye.env.backup-20260101','private.env','config.toml','credentials/app-id.txt',
               'state.db-wal','state.db-shm','house-state.json','season-state.json','profile-state.json',
               'reserve-before-20260101.json','device-latest-now.json','config-redacted.env','private.csv']
        result=subprocess.run(['git','check-ignore','--no-index','--stdin'],input='\n'.join(names)+'\n',
                              text=True,capture_output=True,cwd=root,check=False)
        self.assertEqual(set(result.stdout.splitlines()),set(names))
        for example in ['.env.example','tools/demo.env']:
            result=subprocess.run(['git','check-ignore','--no-index','-q',example],cwd=root,check=False)
            self.assertEqual(result.returncode,1,example)
