import json
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from opportunities.discovery import collect
from datetime import date,timedelta
from django.utils import timezone

class Command(BaseCommand):
    help='本轮公开雇主范围的一次受限发现；持久断点；不启用长期自动采集'
    def add_arguments(self,p):
        p.add_argument('--source',required=True);p.add_argument('--manual-read',action='store_true')
        p.add_argument('--max-records',type=int,default=30);p.add_argument('--max-pages',type=int,default=3)
        p.add_argument('--apply-new',action='store_true')
        p.add_argument('--new-scan',action='store_true');p.add_argument('--retry-failed',action='store_true')
        p.add_argument('--approve-ids',type=str);p.add_argument('--review-note',default='')
        p.add_argument('--reviewed-records',type=str);p.add_argument('--reconsider-ids',type=str)
        p.add_argument('--legacy-file',type=str);p.add_argument('--max-requests',type=int,default=300);p.add_argument('--max-seconds',type=int,default=900)
        p.add_argument('--detail-ttl',type=int,default=86400)
    def handle(self,*args,**o):
        if not o['manual_read']:raise CommandError('须明确 --manual-read；没有长期自动授权')
        if not 1<=o['max_records']<=500 or not 1<=o['max_pages']<=20:raise CommandError('超出有限预算')
        rows=json.loads((settings.BASE_DIR/'data/demo/discovery_sources.json').read_text(encoding='utf8'))
        row=next((r for r in rows if r['key']==o['source']),None)
        if not row or not row['one_shot_admitted']:raise CommandError('来源未准入本轮范围')
        if timezone.localdate()>date.fromisoformat(row['checked_at'])+timedelta(days=30):raise CommandError('来源准入复核已过期；先更新条款/robots审核，不能继续读取')
        try:
            from pathlib import Path
            legacy=None
            if o['legacy_file']:
                old=json.loads(Path(o['legacy_file']).read_text(encoding='utf8'));legacy=next((r for r in old if r['source_id']==row['key']),None)
            approved=json.loads(Path(o['approve_ids']).read_text(encoding='utf8')) if o['approve_ids'] else None
            if not 1<=o['max_requests']<=1000 or not 1<=o['max_seconds']<=1800 or not 0<=o['detail_ttl']<=86400:raise ValueError('超出有限请求/时间/TTL预算')
            state=collect(row,o['max_records'],o['max_pages'],o['apply_new'],action='new' if o['new_scan'] else 'resume',retry_failed=o['retry_failed'],approve_ids=approved,
                review_note=o['review_note'],legacy=legacy,detail_ttl=o['detail_ttl'],max_requests=o['max_requests'],max_seconds=o['max_seconds'],reviewed_records=json.loads(Path(o['reviewed_records']).read_text(encoding='utf8')) if o['reviewed_records'] else None,reconsider_ids=json.loads(Path(o['reconsider_ids']).read_text(encoding='utf8')) if o['reconsider_ids'] else None)
            self.stdout.write(json.dumps({k:state[k] for k in ('source_id','scan_id','status','counts','remaining','unresolved','next_cursor','requests_this_run','list_traversal_complete','classification_resolved','publishable_applied')},ensure_ascii=False))
        except Exception as e:raise CommandError(str(e)) from e
