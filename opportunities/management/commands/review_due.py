import json
from django.core.management.base import BaseCommand
from opportunities.maintenance import maintenance_coverage

class Command(BaseCommand):
    help='Print public fact review queue only; does not fetch, close or schedule anything.'
    def handle(self,*args,**options):
        self.stdout.write(json.dumps(maintenance_coverage(),ensure_ascii=False,indent=2))
