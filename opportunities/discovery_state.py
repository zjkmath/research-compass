"""One source lock, atomic versioned scan JSON, existing database proposal review."""
import json,os,time,uuid
from collections import Counter
from datetime import timedelta
from pathlib import Path
from urllib.error import HTTPError
from django.conf import settings
from django.utils import timezone
from opportunities.ingest import digest
from opportunities.models import Source,Opportunity,UpdateRun,UpdateProposal,SourceAlias
from opportunities.updates import stage,apply_proposal,semantic_facts,entity_fingerprint,applied_is_current,refresh_pending

SCHEMA=3
def upgrade(old):
    from .discovery import RULE_VERSION
    if old.get('schema')==SCHEMA:return old
    original=json.loads(json.dumps(old));rows={}
    for identifier,item in old['discovered'].items():
        prior=old.get('processed',{}).get(identifier,{})
        status=prior.get('status','unprocessed')
        rows[identifier]={**prior,'list_title':item.get('title'),'legacy':prior,'fetch':'success' if prior.get('record') else 'pending',
            'review':'pending' if status in ('pending','excluded','failed') else 'approved' if status=='imported' else 'unjudged',
            'application':'associated' if status=='imported' else 'not_applied','attempts':0,'rule_version':'rc2','legacy_exclusion_requires_review':status=='excluded'}
    return {**old,'schema':SCHEMA,'scan_id':'legacy-'+uuid.uuid4().hex,'parser_version':'rc2','rule_version':'rc2','legacy_original':original,'processed':rows,'list_done':old.get('endpoint_reached',False),'legacy_migrated_at':timezone.now().isoformat()}

def summary(state):
    slots=Counter();pending=[]
    for identifier in state['discovered']:
        row=state['processed'].get(identifier,{})
        slot='excluded' if row.get('scope_excluded') else 'blocked' if row.get('fetch')=='blocked' else 'retryable_failed' if row.get('fetch')=='retryable_failed' else 'unfetched' if row.get('fetch') in (None,'pending') else \
            'excluded' if row.get('review')=='excluded' else 'conflict' if row.get('review')=='conflict' else 'rejected' if row.get('review')=='rejected' else 'applied' if row.get('application') in ('applied','associated') else 'pending_review'
        row['final_slot']=slot;slots[slot]+=1
        row['status']={'applied':'imported','retryable_failed':'failed','blocked':'blocked','excluded':'excluded'}.get(slot,'pending')
        if slot in ('unfetched','retryable_failed','blocked'):pending.append(identifier)
    state.update(counts=dict(slots),remaining=len(pending),next_cursor=pending[0] if pending else None,
        unresolved=sum(v for k,v in slots.items() if k not in ('excluded','applied')),list_traversal_complete=state.get('list_done',False) and state.get('discovery_mode')!='legacy_maintenance_only',
        classification_resolved=not any(v for k,v in slots.items() if k in ('unfetched','retryable_failed','blocked','pending_review','conflict')),
        publishable_applied=not slots.get('pending_review',0) and not slots.get('conflict',0),
        status='RESOLVED_APPLIED' if state.get('list_done') and all(k in ('excluded','applied') for k in slots) else 'PARTIAL')
    if state.get('discovery_mode')=='legacy_maintenance_only':
        state['status']='MAINTENANCE_RESOLVED' if not state['unresolved'] else 'MAINTENANCE_PARTIAL'
        state['endpoint_reached']=False
    assert sum(slots.values())==len(state['discovered'])
    return state

def collect(config,max_records,max_pages,apply_new=False,*,action='resume',retry_failed=False,approve_ids=None,review_note='',legacy=None,detail_ttl=86400,max_requests=300,max_seconds=900,reviewed_records=None,reconsider_ids=None):
    from .discovery import Reader,lists,record,candidate,RULE_VERSION,atomic_json
    if apply_new:raise ValueError('--apply-new removed: preview, then --approve-ids FILE --review-note; approval is explicit and bounded')
    if (approve_ids or reconsider_ids or reviewed_records) and len(review_note.strip())<20:raise ValueError('Approval requires source/evidence/version review note of at least20 characters')
    work=settings.DATA_DIR/'discovery'/config['key'];work.mkdir(parents=True,exist_ok=True);path=work/'scan.json';lock=work/'scan.lock'
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise ValueError('Same-source scan already locked; inspect owner and stop it before removing only this lock')
    os.write(fd,str(os.getpid()).encode());os.close(fd)
    try:
        state=json.loads(path.read_text(encoding='utf8')) if path.exists() else legacy
        if state and state.get('schema')!=SCHEMA:
            atomic_json(work/'scan.rc2.json',state);state=upgrade(state);atomic_json(path,state)
        if state and action=='new':
            archive=work/'history';archive.mkdir(exist_ok=True);atomic_json(archive/(state['scan_id']+'.json'),state);state=None
        if not state:
            state={'schema':SCHEMA,'scan_id':uuid.uuid4().hex,'source_id':config['key'],'scope':config['scope'],'parser_version':'public-list-v3',
                'rule_version':RULE_VERSION,'started_at':timezone.now().isoformat(),'discovered':{},'processed':{},'pages':[],'page_receipts':[],
                'next_url':config['url'],'list_done':False,'reported_total':None,'budget':{'details':max_records,'pages':max_pages,'requests':max_requests,'seconds':max_seconds}}
        reader=Reader(settings.CACHE_DIR/'discovery',config['allowed_hosts']);started=time.monotonic()
        source,_=Source.objects.get_or_create(key=config['key'],defaults={'name':config['institution']+' 官方招聘','url':config['url'],'region':'欧洲',
            'policy_note':config['permission_note'],'terms_url':config['terms_url'],'robots_url':'https://'+__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(config['url']).hostname+'/robots.txt','access_method':'manual','rate_seconds':2})
        from .priority20 import policy, registry, normalized
        allowed_names={normalized(n) for r in registry()['institutions'] for n in [r['official_name'],*r.get('aliases',[])]}
        maintenance_only=bool(policy() and normalized(config['institution']) not in allowed_names)
        if maintenance_only:
            # Preserve old ledgers, but spend no new list/detail budget outside the frozen scope.
            state.update(discovery_mode='legacy_maintenance_only',scope_gate='priority20-v1',next_url=None,list_done=True)
            managed_urls=set(Opportunity.objects.filter(source=source,is_test=False).values_list('url',flat=True))
            for identifier,item in state['discovered'].items():
                if item['url'] not in managed_urls:
                    prior=state['processed'].get(identifier,{})
                    state['processed'][identifier]={**prior,'scope_previous_review':prior.get('scope_previous_review',prior.get('review')),
                        'fetch':prior.get('fetch') or 'not_requested','review':'excluded','application':'not_applied',
                        'reason':'outside_priority20_new_discovery_scope','scope_excluded':True}
            # A resumed pre-P20 scan must still enqueue all retained managed objects.
            state['managed_recheck_enqueued']=False
        run=UpdateRun.objects.create(source=source,url=config['url'],method='bounded_manual_scan_v3')
        # Scan JSON records historical work; only the current DB can confirm application.
        for row in state['processed'].values():
            proposal=UpdateProposal.objects.filter(pk=row.get('proposal_id')).first()
            if proposal and proposal.status in ('pending','conflict','rejected'):
                row.update(review=proposal.status,application='not_applied')
            elif proposal and applied_is_current(proposal):
                row.update(review='approved',application='associated')
            elif row.get('application') in ('applied','associated'):
                row.update(review='conflict',application='not_applied',application_error='Current reviewed entity differs from historical scan application; rescan or explicit review required')
        def save():summary(state);atomic_json(path,state)
        def budget():return reader.requests<max_requests and time.monotonic()-started<max_seconds
        for _ in range(max_pages):
            if not state.get('next_url') or not budget():break
            url=state['next_url']
            try:
                html=reader.read(url,refresh=True);items,nxt=lists(config['parser'],html,url)
                if url in state['pages']:raise ValueError('Repeated page/cursor refused')
                if nxt and nxt in state['pages']+[url]:raise ValueError('Cyclic next cursor refused')
                state['pages'].append(url);state.setdefault('page_receipts',[]).append({'url':url,'ids':[x['id'] for x in items],'next':nxt,'observation':dict(reader.last_meta)})
                for item in items:state['discovered'][item['id']]=item
                state['next_url']=nxt;state['list_done']=not nxt;state['list_error']=None;save()
            except Exception as e:state['list_error']=str(e);save();break
        # Managed entities that vanished from discovery are still reviewed; absence is never closure.
        if state['list_done'] and not state.get('managed_recheck_enqueued'):
            managed=Opportunity.objects.filter(source=source,is_test=False)
            for obj in managed:
                same_url=[item for item in state['discovered'].values() if item['url']==obj.url]
                unique_host_page=managed.filter(url=obj.url).count()==1
                matched=next((item for item in same_url if item.get('managed_key')==obj.key or
                    item.get('official_id') and item['official_id']==obj.official_id or
                    not item.get('official_id') and unique_host_page),None)
                if matched:
                    matched.update(managed_key=obj.key,managed_type=obj.type,official_id=obj.official_id)
                    if not matched.get('purpose'):matched['purpose']='managed_present_recheck'
                    continue
                identifier=next((a.record_id for a in SourceAlias.objects.filter(source=source,opportunity=obj) if not a.record_id.startswith(('key:','official:'))),obj.key.removeprefix(config['key']+'-'))
                if identifier in state['discovered']:identifier='managed-'+obj.key
                if identifier not in state['discovered']:state['discovered'][identifier]={'id':identifier,'title':obj.title,'url':obj.url,'purpose':'managed_absent_recheck','managed_key':obj.key,'managed_type':obj.type,'official_id':obj.official_id}
            state['managed_recheck_enqueued']=True
            save()
        done=0
        for identifier in reconsider_ids or []:
            if identifier not in state['processed']:raise ValueError('Reconsider ID outside scan')
            state['processed'][identifier].update(fetch='pending',reconsider_note=review_note)
        for identifier,item in state['discovered'].items():
            row=state['processed'].setdefault(identifier,{'list_title':item.get('title'),'url':item['url'],'fetch':'pending','review':'unjudged','application':'not_applied','attempts':0})
            if maintenance_only and row.get('scope_excluded'):continue
            if item.get('managed_key') and item.get('managed_type') not in (None,'position','program_round'):
                row.update(fetch='not_requested',review='pending',application='not_applied',
                    reason='retained_non_doctoral_requires_matching_manual_review',
                    maintenance_supported=False,managed_key=item.get('managed_key'))
                continue
            needs=row['fetch']=='pending' or retry_failed and row['fetch']=='retryable_failed' or row.get('legacy_exclusion_requires_review')
            if not needs:continue
            if done>=max_records or not budget():break
            if row.get('next_retry_at') and timezone.now().isoformat()<row['next_retry_at']:continue
            if row.get('attempts',0)>=3:row.update(fetch='blocked',reason='retry budget exhausted');save();continue
            routing=candidate(item.get('title',''));row.update(candidate=routing,rule_version=RULE_VERSION,list_title=item.get('title'),url=item['url'])
            if routing=='non_doctoral' and not item.get('managed_key') and item.get('purpose')!='managed_absent_recheck':
                row.update(fetch='success',review='excluded',reason='explicit_non_doctoral_title',type_evidence={'quote':item.get('title'),'url':item['url'],'scope':'list title only; current or legacy timestamp retained'},legacy_exclusion_requires_review=False);save();continue
            done+=1;row['attempts']=row.get('attempts',0)+1
            try:
                html=reader.read(item['url'],refresh=action=='new' or row['fetch']=='pending' or retry_failed or row.get('legacy_exclusion_requires_review') or identifier in (reconsider_ids or []),ttl=detail_ttl)
                stamp=reader.last_meta.get('observed_content_at')
                if not stamp:raise ValueError('Reader did not provide content observation provenance')
                row.update(observation=dict(reader.last_meta),content_checked_at=stamp,fetch_checked_at=timezone.now().isoformat())
                payload=record(config,item,html,stamp)
                identity_conflict=False
                if item.get('managed_key'):
                    payload['key']=item['managed_key']
                    current=Opportunity.objects.get(key=item['managed_key'],source=source)
                    identity_conflict=bool(payload.get('official_id') and current.official_id and payload['official_id']!=current.official_id)
                    if current.identity_discriminator:
                        payload['official_id']=payload.get('official_id') or current.official_id
                        payload['identity_discriminator']=payload['official_id']
                detail_title=payload.pop('_detail_title',item['title']);project_quote=payload.pop('_project_quote','')
                matches=Opportunity.objects.filter(url=payload['url'],source=source)
                if payload.get('official_id'):
                    matches=matches.filter(official_id=payload['official_id'])
                if matches.count()>1:
                    raise ValueError('ambiguous shared page identity; explicit official identifier review required')
                existing=matches.first()
                if existing:payload['key']=existing.key
                version=digest(semantic_facts(payload));observed=timezone.now()
                # Reuse identical rejected/conflict proposals; repeated network success is not a new approval.
                fp=digest(semantic_facts({'record':payload}));same=None
                for proposed in UpdateProposal.objects.filter(source=source,record_id=identifier).order_by('-pk'):
                    if digest(semantic_facts(proposed.payload))==fp and (
                        applied_is_current(proposed,existing) or proposed.status in ('pending','conflict','rejected') and proposed.base_fingerprint==entity_fingerprint(existing)):
                        same=proposed;break
                if same and same.status=='rejected' and identifier in (reconsider_ids or []):
                    same=UpdateProposal.objects.create(run=run,source=source,record_id=identifier,kind='opportunity',payload={'record':payload},fingerprint=fp,status='pending',observed_at=observed,source_revision=version,revision_type='opaque',base_fingerprint=entity_fingerprint(existing),diff={'reconsidered':same.pk,'reason':review_note})
                    refresh_pending(existing)
                proposal=same or stage(run,identifier,'opportunity',{'record':payload},version,observed)
                if identity_conflict:
                    proposal.status='conflict'
                    proposal.review_note='Official reference changed on a managed page; retain old entity until explicit identity correction evidence is reviewed.'
                    proposal.save(update_fields=['status','review_note'])
                    refresh_pending(current)
                row.update(fetch='success',review=proposal.status if proposal and proposal.status in ('pending','conflict','rejected') else 'approved',
                    application='associated' if proposal and proposal.status=='applied' else 'not_applied',proposal_id=proposal.pk if proposal else None,
                    record=payload,detail_title=detail_title,detail_evidence=project_quote,observation=dict(reader.last_meta),
                    checked_at=stamp,business_fingerprint=version,legacy_exclusion_requires_review=False,reason=None)
                if row['application']=='associated' and existing:SourceAlias.objects.filter(source=source,opportunity=existing).update(checked_at=observed)
            except Exception as e:
                reason=str(e)
                if reason.startswith('non_doctoral_detail:'):
                    row.update(fetch='success',review='excluded',reason='non_doctoral_detail',type_evidence={'quote':reason.split(':',1)[1][:400],'url':item['url']},legacy_exclusion_requires_review=False)
                elif reason.startswith(('uncertain_type:','ambiguous_reference:')):
                    row.update(fetch='success',review='pending',reason=reason,detail_evidence=reason.split(':',1)[1][:400])
                    if reason.startswith('ambiguous_reference:') and item.get('managed_key'):
                        Opportunity.objects.filter(key=item['managed_key'],source=source).update(pending_change=True,translation_status='stale')
                else:
                    policy=isinstance(e,HTTPError) and e.code in (401,403) or 'robots' in reason.lower() or 'scope' in reason.lower()
                    delay=0 if isinstance(e,TimeoutError) else 30*(2**(row['attempts']-1))
                    if isinstance(e,HTTPError) and e.code==429:
                        try:delay=max(delay,int(e.headers.get('Retry-After','30')))
                        except ValueError:delay=max(delay,60)
                    row.update(fetch='blocked' if policy else 'retryable_failed',reason=reason,next_retry_at=(timezone.now()+timedelta(seconds=delay)).isoformat())
            save()
        for reviewed in reviewed_records or []:
            identifier=str(reviewed['id']);row=state['processed'].get(identifier)
            payload=reviewed['record']
            if not row or not row.get('record') or row['fetch']!='success' or identifier not in (approve_ids or []):raise ValueError('Reviewed amendment must name a successfully fetched explicit approval ID')
            if any(payload.get(k)!=row['record'].get(k) for k in ('key','url','source_key')):raise ValueError('Reviewed amendment cannot change source identity or URL')
            if len(reviewed.get('note','').strip())<20:raise ValueError('Individual amendment review note missing')
            version=digest(semantic_facts(payload))
            stage(run,identifier,'opportunity',{'record':payload},version,timezone.now())
            proposal=UpdateProposal.objects.filter(source=source,record_id=identifier).order_by('-pk').first()
            row.update(record=payload,proposal_id=proposal.pk,individual_review_note=reviewed['note'])
            save()
        for identifier in approve_ids or []:
            if identifier not in state['processed']:raise ValueError('Approval ID outside this scan: '+identifier)
            row=state['processed'][identifier];proposal=UpdateProposal.objects.filter(pk=row.get('proposal_id')).first()
            if not proposal:row['application_error']='No proposal; explicit approval not applied';save();continue
            if proposal.status=='rejected':row['application_error']='Rejected proposal requires fresh reconsidered evidence; retained';save();continue
            try:
                apply_proposal(proposal,row.get('individual_review_note',review_note),adjudicate=proposal.status=='conflict');row.update(review='approved',application='applied',application_error=None,approval={'note':row.get('individual_review_note',review_note),'method':'explicit_codex_assisted_source_review','at':timezone.now().isoformat()})
            except Exception as e:row.update(review='conflict',application='not_applied',application_error=str(e))
            save()
        state.update(finished_at=timezone.now().isoformat(),requests_this_run=reader.requests,elapsed_this_run_seconds=round(time.monotonic()-started,2),endpoint_reached=state['list_done'])
        save();run.status=state['status'];run.parsed=done;run.fetched_at=timezone.now();run.save();return state
    finally:lock.unlink(missing_ok=True)
