import json
from django.core.management.base import BaseCommand, CommandError
from opportunities.models import Source, UpdateProposal
from opportunities.updates import configure_sources, run_source, apply_proposal

class Command(BaseCommand):
    help = 'One-shot scoped public API update, preview by default. No schedules or credentials.'
    def add_arguments(self, parser):
        parser.add_argument('--configure', action='store_true')
        parser.add_argument('--source', action='append')
        parser.add_argument('--apply', nargs='*', type=int)
        parser.add_argument('--review-note', default='')
        parser.add_argument('--manual-read', action='store_true', help='明确触发一次已核准正式访问政策读取，不授权循环采集')
    def handle(self, *args, **options):
        if options['configure']: configure_sources()
        if options['apply'] is not None:
            for pk in options['apply']:
                proposal = UpdateProposal.objects.get(pk=pk)
                try: self.stdout.write(f'{pk}: {apply_proposal(proposal, options["review_note"])}')
                except Exception as error:
                    proposal.status = 'conflict'; proposal.review_note = str(error)[:500]; proposal.save()
                    self.stderr.write(f'{pk}: CONFLICT {type(error).__name__}')
            return
        for source in Source.objects.exclude(adapter='').filter(**({'key__in': options['source']} if options['source'] else {})):
            if source.access_method == 'manual' and not options['manual_read']:
                self.stdout.write(f'{source.key}: SKIP manual one-shot source; use --manual-read explicitly'); continue
            run = run_source(source,manual_read=options['manual_read'])
            self.stdout.write(json.dumps({'source': source.key, 'run': run.pk, 'status': run.status,
                'parsed': run.parsed, 'proposed': run.proposed, 'unchanged': run.unchanged, 'error': run.error}))
