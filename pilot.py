"""Loopback backend for private SSH or explicitly verified Tailscale Serve."""
import argparse,json,os,re,signal,sqlite3,sys
from pathlib import Path
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parent

def environment(config,state,port):
    if config.is_relative_to(ROOT):raise RuntimeError('Production config/secret must be outside application directory')
    if not config.is_file():raise RuntimeError('Missing private production configuration')
    if os.name!='nt':
        stat=config.stat()
        if stat.st_uid!=os.getuid() or stat.st_mode & 0o077:raise RuntimeError('Production configuration requires current owner and mode0600')
    value=json.loads(config.read_text('utf8'))
    transport=value.get('transport')
    if not isinstance(value.get('secret_key'),str) or len(value['secret_key'])<50 or transport not in ('ssh_loopback','tailscale_serve'):raise RuntimeError('Invalid private pilot configuration')
    fqdn=value.get('tailscale_fqdn','')
    if transport=='tailscale_serve' and (not isinstance(fqdn,str) or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ts\.net',fqdn) or value.get('verified_proxy_header')!='X-Forwarded-Proto:https'):
        raise RuntimeError('Tailscale mode requires exact FQDN and actual verified Serve proxy header')
    for key in ('COMPASS_LOCAL_RELEASE','COMPASS_INIT_STAGE','DATABASE_URL','DJANGO_SECRET_KEY','COMPASS_DB_PATH','COMPASS_PRIVATE_TUNNEL','COMPASS_TAILSCALE_FQDN','DJANGO_HSTS_SECONDS'):
        os.environ.pop(key,None)
    os.environ.update(DJANGO_SETTINGS_MODULE='radar.settings',RADAR_DEBUG='0',COMPASS_PRIVATE_TUNNEL='1' if transport=='ssh_loopback' else '0',
        COMPASS_DATA_DIR=str(state),COMPASS_DB_PATH=str(state/'db.sqlite3'),DJANGO_SECRET_KEY=value['secret_key'],
        DJANGO_ALLOWED_HOSTS='127.0.0.1,localhost,[::1]',DJANGO_CSRF_TRUSTED_ORIGINS=f'http://127.0.0.1:{port},http://localhost:{port}')
    if transport=='tailscale_serve':
        os.environ.update(COMPASS_TAILSCALE_FQDN=fqdn,DJANGO_ALLOWED_HOSTS='127.0.0.1,localhost,[::1],'+fqdn,
                          DJANGO_CSRF_TRUSTED_ORIGINS='https://'+fqdn)
    return value

def backup(state,destination):
    from compass import backup as consistent_backup
    destination.parent.mkdir(parents=True,exist_ok=True);consistent_backup(state/'db.sqlite3',destination)
    if os.name!='nt':destination.chmod(0o600)

def main():
    p=argparse.ArgumentParser(description='Private loopback backend; SSH or verified tailnet-only HTTPS, never public HTTP')
    p.add_argument('--config',type=Path,required=True);p.add_argument('--state',type=Path,required=True);p.add_argument('--port',type=int,default=8841)
    p.add_argument('command',choices=['configure','check','serve','manage','backup','restore']);p.add_argument('args',nargs=argparse.REMAINDER)
    o=p.parse_args();config=o.config.resolve();state=o.state.resolve()
    if not 1024<=o.port<=65535:p.error('Port outside1024–65535')
    if state==ROOT or state.is_relative_to(ROOT):raise RuntimeError('Private database must be outside application')
    if o.command=='configure':
        if config.is_relative_to(ROOT):raise RuntimeError('Secret outside application required')
        import secrets
        config.parent.mkdir(parents=True,exist_ok=True)
        if not config.exists():
            fd=os.open(str(config),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'w',encoding='utf8') as s:json.dump({'secret_key':secrets.token_urlsafe(64),'transport':'ssh_loopback'},s)
        environment(config,state,o.port);print('Private persistent configuration ready; secret not displayed');return
    value=environment(config,state,o.port)
    if not (state/'db.sqlite3').is_file():raise RuntimeError('Existing validated database required; initialize/adopt separately, never reseed here')
    if o.command=='backup':
        if len(o.args)!=1:p.error('backup requires a new destination')
        backup(state,Path(o.args[0]).resolve());print('Consistent backup integrity PASS');return
    if o.command=='restore':
        if len(o.args)!=1:p.error('restore requires an existing backup source')
        # Preserve current pilot DB for rollback, then restore under explicit lifecycle stop.
        import socket
        with socket.socket() as s:
            if s.connect_ex(('127.0.0.1',o.port))==0:raise RuntimeError('Stop own pilot before restore; occupied port refused')
        source=Path(o.args[0]).resolve()
        before=state/('before-restore-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.sqlite3')
        backup(state,before)
        from compass import backup as consistent_backup
        temp=state/'restore.pending.sqlite3';consistent_backup(source,temp)
        with sqlite3.connect(state/'db.sqlite3') as db:db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        os.replace(temp,state/'db.sqlite3');print('Restore completed with pre-restore rollback backup; HTTP validation still required');return
    import django;django.setup()
    from django.core.management import call_command
    if o.command=='check':
        call_command('check');call_command('check',deploy=True);call_command('makemigrations',check=True,dry_run=True)
        from django.conf import settings
        assert not settings.DEBUG and settings.PRIVATE_SITE and not settings.ALLOW_REGISTRATION
        print('Production login gate PASS; transport='+value['transport']+'; deploy warnings retained');return
    if o.command=='manage':
        if not o.args:p.error('manage requires Django command')
        # Cross-process overlap protection for live refresh commands, not a scheduler.
        if o.args[0] in ('update_sources','refresh_due') and ('--execute-source' in o.args or o.args[0]=='update_sources'):
            if os.name=='nt':raise RuntimeError('Use Linux pilot lock for live pilot refresh')
            import fcntl
            with (state/'update.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                backup(state,state/'backups'/('update-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')+'.sqlite3'))
                call_command(*o.args)
        else:call_command(*o.args)
        return
    from waitress import create_server
    from radar.wsgi import application
    def private_app(environ,start_response):
        if environ.get('REMOTE_ADDR') not in ('127.0.0.1','::1'):
            start_response('403 Forbidden',[('Content-Type','text/plain'),('Cache-Control','no-store')]);return [b'Private loopback transport required']
        if environ.get('PATH_INFO')=='/_health/':
            start_response('200 OK',[('Content-Type','application/json'),('Cache-Control','private, no-store')])
            return [json.dumps({'pid':os.getpid(),'version':(ROOT/'VERSION').read_text().strip(),'private_pilot':True}).encode()]
        return application(environ,start_response)
    proxy={}
    if value['transport']=='tailscale_serve':
        # Only the loopback Serve proxy is trusted; identity headers never authenticate Django users.
        proxy={'trusted_proxy':'127.0.0.1','trusted_proxy_count':1,'trusted_proxy_headers':{'x-forwarded-proto'}}
    server=create_server(private_app,host='127.0.0.1',port=o.port,threads=4,**proxy)
    def shutdown(*_):raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM,shutdown)
    try:server.run()
    except KeyboardInterrupt:pass
    finally:server.close()

if __name__=='__main__':main()
