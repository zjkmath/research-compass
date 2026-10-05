"""Disposable local browser-test identities, never shipped in a review bundle."""
import json
from secrets import token_urlsafe
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = '生成本机浏览器验证账户，随机口令仅写入 .local/browser_accounts.json'

    def handle(self, *args, **options):
        if not settings.DEBUG:
            raise CommandError('只允许 DEBUG 本机验证环境')
        path = settings.BASE_DIR / '.local' / 'browser_accounts.json'
        if path.exists():
            self.stdout.write('已存在本机验证账户；无需重复创建。')
            return
        accounts = []
        for username, staff in [('g1_browser_admin', True), ('g1_browser_other', False)]:
            if get_user_model().objects.filter(username=username).exists():
                raise CommandError('同名账户已存在；不修改既有密码')
            password = token_urlsafe(22)
            get_user_model().objects.create_user(username, password=password, is_staff=staff, is_superuser=staff)
            accounts.append({'username': username, 'password': password})
        path.write_text(json.dumps(accounts), encoding='utf-8')
        self.stdout.write('已创建 2 个独立验证账户；口令未输出、未进版本控制。')
