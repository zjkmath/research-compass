"""Retained synthetic software regressions; real content ledgers are not distributed."""
import json,os,secrets,subprocess,sys,tempfile

from pathlib import Path

from unittest.mock import patch

from django.test import SimpleTestCase,TestCase,Client,override_settings

from django.conf import settings

from pilot import environment

FQDN='demo.example-tail.ts.net'

class TailscaleConfigurationTests(SimpleTestCase):
    def test_config_requires_observed_header_and_exact_host(self):
        (settings.BASE_DIR/'.local').mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.BASE_DIR/'.local') as d,patch.dict(os.environ,{},clear=False),patch('pilot.ROOT',Path(d)/'app'):
            config=Path(d)/'pilot.json';state=Path(d)/'state'
            value={'secret_key':secrets.token_urlsafe(64),'transport':'tailscale_serve','tailscale_fqdn':FQDN}
            def write():
                config.write_text(json.dumps(value),'utf8');config.chmod(0o600)
            write()
            with self.assertRaisesRegex(RuntimeError,'actual verified'):environment(config,state,8841)
            value['verified_proxy_header']='X-Forwarded-Proto:https'
            for host in ('*','.ts.net','https://'+FQDN,FQDN+':443','evil.example','a.b.c.ts.net'):
                value['tailscale_fqdn']=host;write()
                with self.assertRaises(RuntimeError):environment(config,state,8841)
            value['tailscale_fqdn']=FQDN;write();environment(config,state,8841)
            self.assertEqual(os.environ['DJANGO_CSRF_TRUSTED_ORIGINS'],'https://'+FQDN)
            self.assertEqual(os.environ['COMPASS_PRIVATE_TUNNEL'],'0')
            # Switching back clears all HTTPS proxy trust; persistent key remains identical.
            value['transport']='ssh_loopback';write();environment(config,state,8841)
            self.assertNotIn('COMPASS_TAILSCALE_FQDN',os.environ)
            self.assertEqual(os.environ['DJANGO_SECRET_KEY'],value['secret_key'])

    def test_settings_refuse_ambiguous_and_wildcard_modes(self):
        base={**os.environ,'RADAR_DEBUG':'0','COMPASS_LOCAL_RELEASE':'0','COMPASS_PRIVATE_TUNNEL':'0',
              'COMPASS_TAILSCALE_FQDN':FQDN,'DJANGO_SECRET_KEY':secrets.token_urlsafe(64),
              'DJANGO_ALLOWED_HOSTS':'127.0.0.1,localhost,[::1],'+FQDN,
              'DJANGO_CSRF_TRUSTED_ORIGINS':'https://'+FQDN}
        for key in ('DATABASE_URL','COMPASS_DB_PATH','COMPASS_INIT_STAGE'):base.pop(key,None)
        code='from radar import settings as s;assert s.PRIVATE_SITE and not s.DEBUG;assert s.SESSION_COOKIE_SECURE and s.CSRF_COOKIE_SECURE;assert not s.SECURE_SSL_REDIRECT;assert s.SECURE_PROXY_SSL_HEADER==(\"HTTP_X_FORWARDED_PROTO\",\"https\");assert s.SECURE_HSTS_SECONDS==0'
        def probe(extra):return subprocess.run([sys.executable,'-c',code],cwd=settings.BASE_DIR,env={**base,**extra},capture_output=True,text=True)
        ok=probe({});self.assertEqual(ok.returncode,0,ok.stderr)
        for extra in ({'RADAR_DEBUG':'1'},{'COMPASS_LOCAL_RELEASE':'1'},{'COMPASS_PRIVATE_TUNNEL':'1'},
                      {'DJANGO_ALLOWED_HOSTS':'*'},{'DJANGO_CSRF_TRUSTED_ORIGINS':'http://'+FQDN}):
            self.assertNotEqual(probe(extra).returncode,0)

@override_settings(DEBUG=False,PRIVATE_SITE=True,ALLOW_REGISTRATION=False,ALLOWED_HOSTS=[FQDN],
                   CSRF_TRUSTED_ORIGINS=['https://'+FQDN],SESSION_COOKIE_SECURE=True,CSRF_COOKIE_SECURE=True,
                   SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO','https'),SECURE_SSL_REDIRECT=False)
class TailscaleDjangoTests(TestCase):
    def test_secure_login_csrf_and_identity_not_sso(self):
        from django.contrib.auth import get_user_model
        password=secrets.token_urlsafe(24);get_user_model().objects.create_user('tailnet_probe',password=password)
        c=Client(enforce_csrf_checks=True);headers={'HTTP_HOST':FQDN,'HTTP_X_FORWARDED_PROTO':'https'}
        r=c.get('/',HTTP_TAILSCALE_USER_LOGIN='tailnet_probe',**headers)
        self.assertEqual(r.status_code,302);self.assertIn('/accounts/login/',r['Location'])
        r=c.get('/accounts/login/',**headers);self.assertTrue(r.wsgi_request.is_secure())
        self.assertTrue(r.cookies['csrftoken']['secure'])
        token=c.cookies['csrftoken'].value;data={'username':'tailnet_probe','password':password,'csrfmiddlewaretoken':token}
        r=c.post('/accounts/login/',data,HTTP_ORIGIN='https://evil.example',**headers);self.assertEqual(r.status_code,403)
        r=c.post('/accounts/login/',data,HTTP_ORIGIN='https://'+FQDN,**headers);self.assertEqual(r.status_code,302)
        self.assertTrue(r.cookies['sessionid']['secure']);self.assertTrue(r.cookies['sessionid']['httponly'])
        self.assertEqual(c.get('/workspace/',**headers).status_code,200)
        self.assertEqual(c.post('/accounts/logout/',{},**headers).status_code,403)
        r=c.get('/accounts/login/',HTTP_HOST='evil.example',HTTP_X_FORWARDED_PROTO='https');self.assertEqual(r.status_code,400)
