"""Retained synthetic software regressions; real content ledgers are not distributed."""
import os,sys,subprocess,secrets

from pathlib import Path

from django.test import SimpleTestCase, TestCase, override_settings

from django.conf import settings

class PrivatePilotSettingsTests(SimpleTestCase):
    def settings_probe(self, tunnel, hosts, local=False):
        env={**os.environ,'PYTHONUTF8':'1','RADAR_DEBUG':'0','COMPASS_LOCAL_RELEASE':'1' if local else '0',
             'COMPASS_PRIVATE_TUNNEL':'1' if tunnel else '0','DJANGO_ALLOWED_HOSTS':hosts}
        for k in ('DATABASE_URL','COMPASS_DB_PATH','COMPASS_INIT_STAGE'):env.pop(k,None)
        if local:env.pop('DJANGO_SECRET_KEY',None)
        else:env['DJANGO_SECRET_KEY']=secrets.token_urlsafe(64)
        return subprocess.run([sys.executable,'-c','from radar import settings as s;import json;print(json.dumps([s.DEBUG,s.PRIVATE_SITE,s.ALLOW_REGISTRATION,s.SECURE_SSL_REDIRECT,s.SESSION_COOKIE_SECURE,s.CSRF_COOKIE_SECURE]))'],
                              cwd=settings.BASE_DIR,env=env,capture_output=True,text=True,encoding='utf8')
    def test_regular_production_retains_tls_and_registration_gate(self):
        r=self.settings_probe(False,'pilot.example.org')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('[false, true, false, true, true, true]',r.stdout)
    def test_ssh_private_mode_is_production_login_gate(self):
        r=self.settings_probe(True,'127.0.0.1,localhost')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('[false, true, false, false, false, false]',r.stdout)
    def test_non_loopback_and_local_release_refused(self):
        for hosts,local in [('0.0.0.0',False),('*',False),('pilot.example.org',False),('',False),('127.0.0.1',True)]:
            r=self.settings_probe(True,hosts,local)
            self.assertNotEqual(r.returncode,0)
            self.assertIn('loopback-only',r.stderr)

@override_settings(PRIVATE_SITE=True,ALLOW_REGISTRATION=False,ALLOWED_HOSTS=['testserver'])
class PrivatePilotGateTests(TestCase):
    def test_login_has_no_registration_links(self):
        r=self.client.get('/accounts/login/')
        self.assertEqual(r.status_code,200)
        self.assertNotContains(r,'href="/accounts/register/"')
        self.assertContains(r,'私有试运行')
    def test_anonymous_redirect_has_security_and_no_store(self):
        for path in ('/','/targets/','/workspace/','/accounts/register/'):
            r=self.client.get(path)
            self.assertEqual(r.status_code,302)
            self.assertEqual(r['Cache-Control'],'private, no-store')
            self.assertEqual(r['X-Robots-Tag'],'noindex, nofollow')
            self.assertIn("frame-ancestors 'none'",r['Content-Security-Policy'])
