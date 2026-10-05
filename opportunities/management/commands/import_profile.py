import json
from pathlib import Path
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from opportunities.models import LocalProfile

class Command(BaseCommand):
    help = 'Explicitly bind a local private profile to one named user; no network requests.'
    def add_arguments(self, parser):
        parser.add_argument('username'); parser.add_argument('file')
    def handle(self, *args, **options):
        user = get_user_model().objects.filter(username=options['username']).first()
        if not user: raise CommandError('指定账户不存在；不会自动绑定其他账户')
        data = json.loads(Path(options['file']).read_text(encoding='utf-8'))
        if not isinstance(data, dict) or len(json.dumps(data)) > 16000: raise CommandError('资料须为不超过16KB的JSON对象')
        LocalProfile.objects.update_or_create(user=user, defaults={'data': data})
        self.stdout.write('本地私有资料已绑定；未发送至外部来源。')
