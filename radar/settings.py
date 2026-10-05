import os,re
from pathlib import Path
from urllib.parse import urlparse, unquote

BASE_DIR = Path(__file__).resolve().parent.parent
DEBUG = os.getenv('RADAR_DEBUG', '1') == '1'
LOCAL_RELEASE = os.getenv('COMPASS_LOCAL_RELEASE') == '1'
DATA_DIR = Path(os.getenv('COMPASS_DATA_DIR', str(BASE_DIR))).resolve()
CACHE_DIR = DATA_DIR / 'cache'
PRIVATE_SITE = not DEBUG and not LOCAL_RELEASE
ALLOW_REGISTRATION = DEBUG or LOCAL_RELEASE
PRIVATE_TUNNEL = os.getenv('COMPASS_PRIVATE_TUNNEL') == '1'
TAILSCALE_FQDN = os.getenv('COMPASS_TAILSCALE_FQDN','')
if TAILSCALE_FQDN and (DEBUG or LOCAL_RELEASE or PRIVATE_TUNNEL or not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.ts\.net',TAILSCALE_FQDN)):
    raise RuntimeError('Tailscale requires production mode, an exact ts.net FQDN and a separate transport configuration')
if PRIVATE_TUNNEL and (DEBUG or LOCAL_RELEASE):
    raise RuntimeError('SSH private pilot requires production mode and loopback-only allowed hosts')
SECRET_KEY = os.getenv('DJANGO_SECRET_KEY', '')
if LOCAL_RELEASE and any(os.getenv(k) for k in ('DJANGO_SECRET_KEY','COMPASS_DB_PATH','DATABASE_URL')):
    raise RuntimeError('本机发行不接受外部库或密钥覆盖')
if not SECRET_KEY:
    if not DEBUG and not LOCAL_RELEASE:
        raise RuntimeError('生产环境须配置 DJANGO_SECRET_KEY')
    # Local-only key; never valid for a deployed environment.
    local_dir = DATA_DIR if LOCAL_RELEASE else BASE_DIR / '.local'
    local_dir.mkdir(parents=True,exist_ok=True)
    key_file = local_dir / 'dev_key'
    if not key_file.exists():
        if LOCAL_RELEASE and os.getenv('COMPASS_INIT_STAGE') != '1':
            raise RuntimeError('已有本机数据必须使用原持久密钥；请恢复 dev_key')
        from secrets import token_urlsafe
        with key_file.open('x',encoding='utf-8') as stream: stream.write(token_urlsafe(50))
        key_file.chmod(0o600)
    SECRET_KEY = key_file.read_text(encoding='utf-8')
ALLOWED_HOSTS = ['127.0.0.1', 'localhost', '[::1]'] if LOCAL_RELEASE or DEBUG else [v.strip() for v in os.getenv('DJANGO_ALLOWED_HOSTS','').split(',') if v.strip()]
CSRF_TRUSTED_ORIGINS = [v for v in os.getenv('DJANGO_CSRF_TRUSTED_ORIGINS','').split(',') if v]
if TAILSCALE_FQDN:
    if set(ALLOWED_HOSTS)!={'127.0.0.1','localhost','[::1]',TAILSCALE_FQDN} or CSRF_TRUSTED_ORIGINS!=['https://'+TAILSCALE_FQDN]:
        raise RuntimeError('Tailscale requires exact loopback/FQDN hosts and HTTPS CSRF origin')
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO','https')
if PRIVATE_TUNNEL and (DEBUG or LOCAL_RELEASE or not ALLOWED_HOSTS or any(v not in ('127.0.0.1','localhost','[::1]') for v in ALLOWED_HOSTS)):
    raise RuntimeError('SSH private pilot requires production mode and loopback-only allowed hosts')
INSTALLED_APPS = [
    'django.contrib.admin', 'django.contrib.auth', 'django.contrib.contenttypes',
    'django.contrib.sessions', 'django.contrib.messages', 'django.contrib.staticfiles',
    'opportunities',
]
MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware', 'whitenoise.middleware.WhiteNoiseMiddleware', 'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware', 'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware', 'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware', 'opportunities.middleware.LocalSecurityHeaders',
]
ROOT_URLCONF = 'radar.urls'
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
              'DIRS': [BASE_DIR / 'templates'], 'APP_DIRS': True,
              'OPTIONS': {'context_processors': ['django.template.context_processors.request',
                  'django.contrib.auth.context_processors.auth', 'django.contrib.messages.context_processors.messages']}}]
WSGI_APPLICATION = 'radar.wsgi.application'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': Path(os.getenv('COMPASS_DB_PATH', str(DATA_DIR / 'db.sqlite3'))),
                         'OPTIONS': {'timeout': 20}}}
if LOCAL_RELEASE:
    DATABASES['default']['NAME'] = DATA_DIR / ('initializing.sqlite3' if os.getenv('COMPASS_INIT_STAGE') == '1' else 'db.sqlite3')
if os.getenv('DATABASE_URL'):
    if LOCAL_RELEASE: raise RuntimeError('本机发行拒绝 DATABASE_URL，避免误连远程私人库')
    db = urlparse(os.environ['DATABASE_URL'])
    if db.scheme not in ('postgres', 'postgresql'):
        raise RuntimeError('DATABASE_URL 只接受 PostgreSQL')
    DATABASES = {'default': {'ENGINE': 'django.db.backends.postgresql',
        'NAME': unquote(db.path.lstrip('/')), 'USER': unquote(db.username or ''),
        'PASSWORD': unquote(db.password or ''), 'HOST': db.hostname, 'PORT': db.port or 5432,
        'OPTIONS': {'sslmode': os.getenv('PGSSLMODE', 'require')}}}
AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
LANGUAGE_CODE = 'zh-hans'
TIME_ZONE = 'Asia/Shanghai'
USE_I18N = True
USE_TZ = True
STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'static_collected'
WHITENOISE_USE_FINDERS = False
WHITENOISE_AUTOREFRESH = False
STATICFILES_DIRS = [BASE_DIR / 'static']
DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
FACT_REVIEW_DAYS = {'deadline':1, 'finance':30, 'research':90, 'visit_policy':30}
LOGIN_URL = '/accounts/login/'
LOGIN_REDIRECT_URL = '/workspace/'
LOGOUT_REDIRECT_URL = '/'
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'
CSRF_COOKIE_SAMESITE = 'Lax'
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = 'DENY'
if not DEBUG and not LOCAL_RELEASE:
    SECURE_HSTS_SECONDS = 0 if PRIVATE_TUNNEL or TAILSCALE_FQDN else int(os.getenv('DJANGO_HSTS_SECONDS','0'))
    SESSION_COOKIE_SECURE = not PRIVATE_TUNNEL
    CSRF_COOKIE_SECURE = not PRIVATE_TUNNEL
    SECURE_SSL_REDIRECT = not PRIVATE_TUNNEL and not TAILSCALE_FQDN
