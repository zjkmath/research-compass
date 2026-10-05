"""Local browser release. Explicit stable private directory; never resets an existing DB."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from urllib.request import build_opener, ProxyHandler
import webbrowser

ROOT = Path(__file__).resolve().parent


def backup(source, destination):
    if destination.exists(): raise RuntimeError('备份目标已存在，不覆盖')
    with sqlite3.connect(f'file:{source.as_posix()}?mode=ro', uri=True) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok': raise RuntimeError('备份完整性失败')


def main():
    parser = argparse.ArgumentParser(description='研途本机发行；所有私人数据保存在显式指定目录')
    parser.add_argument('--data-dir', default=os.getenv('RESEARCH_COMPASS_DATA_DIR'))
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('command', choices=['check','init','start','serve','stop','status','upgrade','backup','restore','adopt','manage','refresh-due','recover-init'])
    parser.add_argument('args', nargs=argparse.REMAINDER)
    options = parser.parse_args()
    if not options.data_dir: parser.error('须指定稳定的 --data-dir，更新软件继续使用同一目录')
    for name in ('DATABASE_URL','COMPASS_DB_PATH','DJANGO_SECRET_KEY','COMPASS_INIT_STAGE'):
        if os.getenv(name): raise RuntimeError('本机发行拒绝继承 '+name+'；清除冲突配置后重试，原库和密钥未改动')
    data = Path(options.data_dir).resolve()
    if data == ROOT or data.is_relative_to(ROOT / 'static_collected'): raise RuntimeError('私人数据须与运行源码分离')
    data.mkdir(parents=True, exist_ok=True)
    os.environ.update(COMPASS_DATA_DIR=str(data), COMPASS_LOCAL_RELEASE='1', RADAR_DEBUG='0', DJANGO_SETTINGS_MODULE='radar.settings')
    db = data / 'db.sqlite3'; marker = data / 'instance.json'; stage = data / 'initializing.sqlite3'
    def manage(*args, initializing=False):
        env={**os.environ}
        if initializing: env['COMPASS_INIT_STAGE']='1'
        return subprocess.run([sys.executable, str(ROOT/'manage.py'), *args], cwd=ROOT, env=env, check=True)
    def instance():
        value=json.loads(marker.read_text(encoding='utf8'))
        if type(value.get('port')) is not int or not 1024<=value['port']<=65535 or type(value.get('pid')) is not int or value['pid']<=0: raise RuntimeError('实例文件无效')
        with build_opener(ProxyHandler({})).open(f"http://127.0.0.1:{value['port']}/_health/",timeout=2) as response:
            live=json.load(response)
        if live != {'pid':value['pid'],'instance':value['instance']}: raise RuntimeError('实例身份不一致，拒绝停止未知进程')
        return value
    if options.command=='check':
        for line in (ROOT/'requirements.lock').read_text().splitlines():
            name,version=line.split('==')
            if importlib.metadata.version(name)!=version: raise RuntimeError('依赖不匹配：'+name)
        if sys.version_info < (3,10): raise RuntimeError('需要 Python 3.10+；实际验收为 3.13.11')
        manifest=ROOT/'MANIFEST_SHA256.json'
        if manifest.exists():
            for row in json.loads(manifest.read_text(encoding='utf8'))['files']:
                path=ROOT/row['path']
                if not path.resolve().is_relative_to(ROOT) or hashlib.sha256(path.read_bytes()).hexdigest()!=row['sha256']: raise RuntimeError('发行文件校验失败：'+row['path'])
        print('依赖、发行清单及数据路径检查通过；不下载服务或依赖'); return
    if options.command in ('init','recover-init'):
        if db.exists(): raise RuntimeError('已有数据库，拒绝重载种子或覆盖；软件更新用 upgrade')
        if stage.exists() and options.command!='recover-init': raise RuntimeError('发现中断初始化；请核查后用 recover-init，不隐式删除')
        manage('migrate','--noinput',initializing=True)
        # loaddata is atomic; rerunning only allowed in the isolated unfinished new DB.
        manage('loaddata','data/demo/synthetic_demo.json',initializing=True)
        manage('activate_priority20',initializing=True)
        with sqlite3.connect(stage) as connection:
            if connection.execute('PRAGMA integrity_check').fetchone()[0]!='ok': raise RuntimeError('初始化完整性失败')
            if connection.execute('SELECT count(*) FROM auth_user').fetchone()[0]: raise RuntimeError('公开种子含账户，拒绝发布')
        connection.close()
        stage.replace(db); print('已初始化公开数据；创建本人账号：manage createsuperuser 或浏览器注册'); return
    if options.command=='adopt':
        if db.exists() or (data/'dev_key').exists(): raise RuntimeError('采用目标已有库或密钥，拒绝覆盖')
        old=Path(options.args[0]).resolve()
        if old==data: raise RuntimeError('采用来源与目标不能相同')
        source_key=old/'.local/dev_key' if (old/'.local/dev_key').is_file() else old/'dev_key'
        if not source_key.is_file(): raise RuntimeError('旧密钥不存在；禁止静默换钥')
        backup(old/'db.sqlite3',db)
        (data/'dev_key').write_bytes(source_key.read_bytes())
        print('已一致复制原库和原密钥；原工程未覆盖；下一步 upgrade'); return
    if options.command=='restore':
        if db.exists() or stage.exists(): raise RuntimeError('恢复只允许空的隔离数据目录；原库不覆盖')
        key=Path(options.args[1]).resolve()
        if not key.is_file() or (data/'dev_key').exists(): raise RuntimeError('恢复来源密钥缺失或目标密钥已存在，不覆盖')
        backup(Path(options.args[0]).resolve(),db)
        (data/'dev_key').write_bytes(key.read_bytes()); print('隔离恢复通过；仍须运行 check 和启动核查'); return
    if not db.exists(): raise RuntimeError('尚未初始化/采用数据；禁止启动时静默造空库')
    if not (data/'dev_key').is_file(): raise RuntimeError('持久密钥缺失；请恢复原 dev_key，禁止为已有库静默换钥')
    if options.command=='backup':
        dest=Path(options.args[0]).resolve() if options.args else data/('backup-'+time.strftime('%Y%m%d-%H%M%S')+'.sqlite3')
        backup(db,dest); print(str(dest)); print('恢复时须同时保留原 dev_key；密钥不进发行包'); return
    if options.command=='upgrade':
        if marker.exists(): raise RuntimeError('请先停止本实例再升级，保留迁移一致性')
        backup(db,data/('before-upgrade-'+time.strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(3)+'.sqlite3'))
        manage('migrate','--noinput')
        manage('activate_priority20')
        print('Schema upgrade complete; no reseed or automatic fact replacement.'); return
    if options.command=='manage': manage(*options.args); return
    if options.command=='refresh-due': manage('refresh_due',*options.args); return
    if options.command=='status':
        try: print(json.dumps(instance()))
        except (OSError,ValueError,RuntimeError): print('未验证到本发行管理的运行实例')
        return
    if options.command=='stop':
        value=instance(); os.kill(value['pid'],signal.SIGTERM)
        for _ in range(30):
            try: instance()
            except (OSError,ValueError,RuntimeError): marker.unlink(missing_ok=True); print('本实例已停止'); return
            time.sleep(.1)
        raise RuntimeError('停止未确认；未操作其他进程')
    if options.command=='start':
        if not 1024 <= options.port <= 65535: raise RuntimeError('端口须在1024—65535')
        with socket.socket() as sock:
            try: sock.bind(('127.0.0.1',options.port))
            except OSError: raise RuntimeError('端口已占用；不停止占用者，请选择另一端口')
        if not (ROOT/'static_collected/radar.css').exists(): raise RuntimeError('发行静态资源缺失；维护者须 collectstatic，不在日常启动写入安装目录')
        token=secrets.token_hex(24)
        child_env={**os.environ,'COMPASS_INSTANCE_TOKEN':token}
        child=subprocess.Popen([sys.executable,str(ROOT/'compass.py'),'--data-dir',str(data),'--port',str(options.port),'serve'],cwd=ROOT,env=child_env)
        try:
            for _ in range(80):
                try:
                    value=instance()
                    if value['instance']==token: break
                except (OSError,ValueError,RuntimeError): pass
                if child.poll() is not None: raise RuntimeError('服务启动失败')
                time.sleep(.1)
            else: raise RuntimeError('健康检查超时')
            print(f'本机发行已启动 http://127.0.0.1:{options.port}/priority20/；关闭本窗口或另开 stop 停止',flush=True)
            if not options.no_browser: webbrowser.open(f'http://127.0.0.1:{options.port}/priority20/')
            child.wait()
        finally:
            if child.poll() is None:
                try:
                    owned=instance()
                    if owned['instance']==token: os.kill(owned['pid'],signal.SIGTERM)
                except (OSError,ValueError,RuntimeError): pass
                child.terminate(); child.wait(timeout=10)
            if marker.exists() and json.loads(marker.read_text())['instance']==token: marker.unlink()
        return
    if options.command=='serve':
        import django
        django.setup()
        from opportunities.priority20 import require_active
        require_active()
        from radar.wsgi import application
        from waitress import serve
        value={'pid':os.getpid(),'port':options.port,'instance':os.getenv('COMPASS_INSTANCE_TOKEN') or secrets.token_hex(24)}
        def app(environ,start_response):
            if environ.get('PATH_INFO')=='/_health/':
                body=json.dumps({'pid':value['pid'],'instance':value['instance']}).encode()
                start_response('200 OK',[('Content-Type','application/json'),('Cache-Control','no-store'),('Content-Length',str(len(body)))])
                return [body]
            return application(environ,start_response)
        marker.write_text(json.dumps(value),encoding='utf8')
        try: serve(app,host='127.0.0.1',port=options.port,threads=4,clear_untrusted_proxy_headers=True)
        finally: marker.unlink(missing_ok=True)


if __name__=='__main__':
    try: main()
    except (RuntimeError,OSError,IndexError,subprocess.CalledProcessError) as error:
        print('未完成：'+str(error),file=sys.stderr); sys.exit(1)
