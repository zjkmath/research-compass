"""Offline synthetic runtime lifecycle check. No private instance or live sources."""
import http.cookiejar, json, os, secrets, socket, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path
from contextlib import closing
from urllib.request import build_opener, HTTPCookieProcessor, ProxyHandler
from urllib.parse import urlencode

ROOT=Path(__file__).resolve().parents[1]

def main():
    with tempfile.TemporaryDirectory(prefix='compass-synthetic-') as temporary:
        base=Path(temporary);state=base/'state';restored=base/'restored'
        env={**os.environ,'PYTHONUTF8':'1'}
        for key in ('DATABASE_URL','COMPASS_DB_PATH','DJANGO_SECRET_KEY','COMPASS_INIT_STAGE','COMPASS_TAILSCALE_FQDN','COMPASS_PRIVATE_TUNNEL'):
            env.pop(key,None)
        def run(*args,extra=None):
            r=subprocess.run([sys.executable,*map(str,args)],cwd=ROOT,env={**env,**(extra or {})},capture_output=True,text=True,encoding='utf8')
            if r.returncode:raise RuntimeError(r.stdout+r.stderr)
            return r.stdout
        def cmd(directory,*args):return run(ROOT/'compass.py','--data-dir',directory,*args)
        run('manage.py','collectstatic','--noinput')
        cmd(state,'check');cmd(state,'init')
        password=secrets.token_urlsafe(32)
        run(ROOT/'compass.py','--data-dir',state,'manage','shell','-c',
            "import os;from django.contrib.auth import get_user_model;get_user_model().objects.create_user('synthetic_smoke',password=os.environ['OSS_SMOKE_PASSWORD'])",
            extra={'OSS_SMOKE_PASSWORD':password})
        key=(state/'dev_key').read_bytes()
        launchers={}
        def start(directory):
            with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
            log=(base/(directory.name+'-server.log')).open('ab')
            process=subprocess.Popen([sys.executable,str(ROOT/'compass.py'),'--data-dir',str(directory),
                '--port',str(port),'--no-browser','start'],cwd=ROOT,env=env,stdout=log,stderr=log)
            launchers[directory]=(process,log)
            probe=build_opener(ProxyHandler({}))
            for _ in range(100):
                try:
                    with probe.open(f'http://127.0.0.1:{port}/_health/',timeout=1) as response:
                        assert json.load(response)['pid']>0
                    return port
                except OSError:
                    if process.poll() is not None:break
                    time.sleep(.1)
            process.terminate();process.wait(timeout=10);log.close()
            raise RuntimeError('Synthetic launcher failed; inspect isolated server log')
        def stop(directory):
            process,log=launchers[directory]
            try:cmd(directory,'stop');process.wait(timeout=15)
            finally:
                if process.poll() is None:process.terminate();process.wait(timeout=10)
                log.close()
        jar=http.cookiejar.CookieJar();client=build_opener(ProxyHandler({}),HTTPCookieProcessor(jar))
        def get(port,path):
            with client.open(f'http://127.0.0.1:{port}'+path,timeout=15) as r:return r.read()
        def post(port,path,data):
            token=next(c.value for c in jar if c.name=='csrftoken')
            with client.open(f'http://127.0.0.1:{port}'+path,urlencode({**data,'csrfmiddlewaretoken':token}).encode(),timeout=15) as r:return r.read()
        def login(port):
            get(port,'/accounts/login/');post(port,'/accounts/login/',{'username':'synthetic_smoke','password':password})
            assert any(c.name=='sessionid' for c in jar)
        port=start(state)
        try:
            login(port)
            for path in ('/','/priority20/','/priority20/demo-01/','/targets/','/targets/1/','/opportunities/1/','/targets/1/budget/','/compare/'):
                assert b'Synthetic demo' in get(port,path),path
            assert b'--' in get(port,'/static/radar.css')
            marker='Synthetic lifecycle witness '+secrets.token_hex(8)
            post(port,'/targets/1/record/',{'stage':'researching','note':marker})
            post(port,'/filters/save/',{'name':'Synthetic smoke selection','return_to':'/targets/?fit_tier=A'})
            with closing(sqlite3.connect(state/'db.sqlite3')) as db:
                assert db.execute('select note from opportunities_targetrecord').fetchone()[0]==marker
                db.execute('PRAGMA journal_mode=WAL')
            dest=base/'backup.sqlite3';cmd(state,'backup',dest)
        finally:stop(state)
        cmd(state,'upgrade')
        with closing(sqlite3.connect(state/'db.sqlite3')) as db:assert db.execute('select note from opportunities_targetrecord').fetchone()[0]==marker
        assert (state/'dev_key').read_bytes()==key
        cmd(restored,'restore',dest,state/'dev_key')
        assert (restored/'dev_key').read_bytes()==key
        port=start(restored)
        try:
            login(port);assert marker.encode() in get(port,'/workspace/')
            second=marker+' restored HTTP write'
            post(port,'/targets/1/record/',{'stage':'paused','note':second})
            with closing(sqlite3.connect(restored/'db.sqlite3')) as db:assert db.execute('select note from opportunities_targetrecord').fetchone()[0]==second
        finally:stop(restored)
        port=start(restored)
        try:assert second.encode() in get(port,'/workspace/')
        finally:stop(restored)
        print(json.dumps({'synthetic_only':True,'clean_init':'PASS','schema_upgrade_preservation':'PASS','key':'PASS','WAL_backup':'PASS',
            'restored_HTTP_read_write':'PASS','restart_session':'PASS','Waitress_WhiteNoise':'PASS','live_requests':0,'own_services_stopped':True}))

if __name__=='__main__':main()
