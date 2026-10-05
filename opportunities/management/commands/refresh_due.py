import json
from django.core.management.base import BaseCommand,CommandError
from django.utils import timezone
from opportunities.maintenance import curated_due_queue
from opportunities.models import Source
from opportunities.updates import run_source

class Command(BaseCommand):
    help='One-shot due queue for an external scheduler; default is read-only with zero requests.'
    def add_arguments(self,p):
        p.add_argument('--execute-source',help='Explicitly run one already-authorized supported adapter; never approves changes')
        p.add_argument('--dry-run',action='store_true',help='Read-only prioritized page, no fetching or approval')
        p.add_argument('--limit',type=int,help='Page size 1–100; dry-run defaults to25')
        p.add_argument('--resume',help='JSON file containing the previous next_cursor; stale snapshots are refused')
        p.add_argument('--scheduler-template',action='store_true',help='Print a command template only; never register an OS task')
    def handle(self,*args,**options):
        from pathlib import Path
        from django.conf import settings
        import subprocess,sys
        if options.get('dry_run') and options.get('execute_source'):
            raise CommandError('--dry-run不允许网络执行')
        if options.get('resume') and options.get('execute_source'):
            raise CommandError('--resume仅用于只读审核队列，不隐式执行来源')
        limit=options.get('limit')
        if limit is not None and not 1<=limit<=100:raise CommandError('--limit须为1—100')
        queue=curated_due_queue(include_private=True)
        key=options.get('execute_source')
        if key:
            source=Source.objects.filter(key=key).first()
            if not source or not source.automation_allowed or not source.policy_checked_at or source.policy_checked_at>timezone.now() or timezone.now()>=source.policy_checked_at+timezone.timedelta(days=source.review_period_days) or not source.authorizations.filter(allowed=True,scope=source.scope).exists():
                raise CommandError('未获有效自动读取许可，留在 human_review_queue')
            if source.adapter not in ('grants-gov-public-api','wikidata-entity-json','ror-v2-organizations'):
                raise CommandError('此来源需既有受限范围采集命令或人工读取；本调度命令不猜测采集路径')
            run=run_source(source);queue['network_requests']=None;queue['network_count_note']='已显式调用适配器；底层网络尝试次数未统计，不能报告零请求';queue['executed']={'source':key,'run_id':run.pk,'status':run.status,'changes_auto_approved':False}
        rows=queue['entries'];start=0
        if options.get('resume'):
            path=Path(options['resume'])
            try:
                if path.stat().st_size>4096:raise ValueError('cursor too large')
                cursor=json.loads(path.read_text('utf-8-sig'))
                if not isinstance(cursor,dict) or set(cursor)!={'after','snapshot'} or cursor['snapshot']!=queue['snapshot']:raise ValueError('snapshot changed')
                ids=[r['entry_id'] for r in rows];start=ids.index(cursor['after'])+1
            except (OSError,ValueError,KeyError,TypeError) as exc:raise CommandError('队列已变或续跑游标无效；重新dry-run，未写库/未发请求') from exc
        if options.get('dry_run') and limit is None:limit=25
        queue['entries']=rows[start:start+limit] if limit else rows[start:]
        end=start+len(queue['entries'])
        queue.update(dry_run=not bool(key),page={'start':start,'returned':len(queue['entries']),'remaining':len(rows)-end},next_cursor={'after':queue['entries'][-1]['entry_id'],'snapshot':queue['snapshot']} if end<len(rows) and queue['entries'] else None)
        if options.get('scheduler_template'):
            queue['scheduler_template']={'execute':sys.executable,'arguments':subprocess.list2cmdline([str(settings.BASE_DIR/'compass.py'),'--data-dir',str(settings.DATA_DIR),'refresh-due','--dry-run','--limit','25']),
                'working_directory':str(settings.BASE_DIR),'registration_performed':False,'note':'Only a read-only queue preview. Manual/human-review rows remain manual; install a daily Windows task only after explicit user authorization.'}
        self.stdout.write(json.dumps(queue,ensure_ascii=False))
