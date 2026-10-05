import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.core.exceptions import ValidationError
from opportunities.ingest import import_reviewed


class Command(BaseCommand):
    help = '导入经人工核验的 JSON，事务更新、稳定去重、重复运行不改变事实'

    def add_arguments(self, parser):
        parser.add_argument('paths', nargs='+')

    def handle(self, *args, **options):
        for name in options['paths']:
            try:
                path = Path(name)
                if path.stat().st_size > 5 * 1024 * 1024:
                    raise ValueError('审核 JSON 超过 5 MiB')
                payload = json.loads(path.read_text(encoding='utf-8-sig'))
                counts = import_reviewed(payload, name)
                self.stdout.write(f'{path.name}: {json.dumps(counts, ensure_ascii=False)}')
            except (OSError, ValueError, ValidationError, KeyError) as error:
                raise CommandError(str(error)) from error
