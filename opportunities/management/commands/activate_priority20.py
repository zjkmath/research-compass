from django.core.management.base import BaseCommand
from opportunities.priority20 import activate


class Command(BaseCommand):
    help = 'Freeze existing local records as legacy; enforce the reviewed Priority-20 discovery scope.'

    def handle(self, *args, **options):
        state, created = activate()
        self.stdout.write('Priority-20 active; legacy boundary ' + ('created' if created else 'preserved'))
