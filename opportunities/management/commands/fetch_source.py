import json
from django.core.management.base import BaseCommand, CommandError
from opportunities.models import Source
from opportunities.ingest import fetch_source


class Command(BaseCommand):
    help = '仅访问经后台条款核准的正式 feed；G1 人工来源默认禁用'

    def add_arguments(self, parser):
        parser.add_argument('key')

    def handle(self, *args, **options):
        try:
            source = Source.objects.get(key=options['key'])
            self.stdout.write(json.dumps(fetch_source(source), ensure_ascii=False))
        except Exception as error:
            raise CommandError(str(error)) from error
