"""Manual due queue: checking a page is not re-verifying a fact."""
from datetime import timedelta
from django.utils import timezone
from django.conf import settings
from django.utils.dateparse import parse_datetime
from .models import Source, Opportunity, VisitPath, ResearchTarget, SourceAlias

def refresh_policy():
    import json
    defaults={'job_deadline':1,'visit_policy':30,'funding_support':30,'projects':30,'papers_awards':90,'alumni':180,'academic_lineage':None,'qs_ranking':None,
        'programme':30,'lab_profile':30,'host_receiving':30,'coverage_identity':180,'csc_channel':30,'yuanhang_channel':14,'priority20_scope':365}
    config=settings.DATA_DIR/'refresh_policy.json'
    if config.exists():
        override=json.loads(config.read_text(encoding='utf8'))
        if any(k not in defaults or (v is not None and (type(v) is not int or not 1<=v<=3650)) for k,v in override.items()):
            raise ValueError('refresh_policy.json 只接受已知类别及1—3650日/null')
        defaults.update(override)
    return defaults

def curated_due_queue(now=None, user=None, include_private=False):
    """Read-only field queue; authorized metadata is not a job refresh permission."""
    import hashlib, json
    from collections import Counter
    from django.db.models import Q
    from .models import UserRecord, TargetRecord, DecisionRecord
    from .curation import coverage_presence_counts
    now=now or timezone.now(); policy=refresh_policy(); rows=[]
    # CLI operators may aggregate local tracking; browser callers see only their own.
    tracked_opportunities=set();tracked_targets=set();tracked_paths=set()
    authenticated=bool(user and user.is_authenticated)
    if authenticated or include_private:
        records=UserRecord.objects.filter(Q(saved=True)|~Q(stage='none'))
        targets=TargetRecord.objects.filter(Q(saved=True)|~Q(stage='researching')|~Q(note=''))
        decisions=DecisionRecord.objects.all()
        if authenticated:
            records=records.filter(user=user);targets=targets.filter(user=user);decisions=decisions.filter(user=user)
        tracked_opportunities=set(records.values_list('opportunity_id',flat=True))
        tracked_targets=set(targets.values_list('target_id',flat=True))|set(decisions.exclude(target=None).values_list('target_id',flat=True))
        tracked_paths=set(decisions.exclude(path=None).values_list('path_id',flat=True))
    def parsed(value):
        try:
            stamp=parse_datetime(value) if isinstance(value,str) else value
            return timezone.make_aware(stamp) if stamp and timezone.is_naive(stamp) else stamp
        except (TypeError,ValueError): return None
    def add(kind,key,category,stamp,url,selected,source=None,entity=None,facts=None,global_unknown=False,interval='default'):
        period=policy[category] if interval=='default' else interval; value=parsed(stamp)
        due=bool(value and value>now) or period is not None and (not value or now>=value+timedelta(days=period))
        authorized=False
        if source and source.automation_allowed and source.policy_checked_at and now<source.policy_checked_at+timedelta(days=source.review_period_days) and source.authorizations.filter(allowed=True,scope=source.scope).exists():
            # Only the established alias + adapter scope can cover this object.
            ids=set(SourceAlias.objects.filter(source=source,**{kind:entity}).values_list('record_id',flat=True)) if entity is not None and kind in ('opportunity','target','path') else set()
            if source.adapter=='grants-gov-public-api' and kind=='opportunity':
                from urllib.parse import urlsplit
                address=urlsplit(source.url); scope=source.scope; body=scope.get('search_body',{})
                authorized=bool(source.policy_checked_at<=now and address.scheme=='https' and address.hostname=='api.grants.gov'
                    and address.hostname in scope.get('allowed_hosts',[]) and not address.query
                    and address.path=='/v1/api/search2' and {'/v1/api/search2','/v1/api/fetchOpportunity'}.issubset(scope.get('allowed_paths',[]))
                    and isinstance(body,dict) and type(body.get('rows')) is int and 0<body['rows']<=20 and any(i.isdigit() for i in ids))
        times={k:[] for k in ('fetched_at','source_published_at','content_observed_at','human_verified_at')}; urls=[]
        def scan(v):
            if isinstance(v,dict):
                for k in times:
                    value=v.get(k)
                    # Existing fact evidence called this clock observed_at.
                    # It remains content observation, never human verification.
                    if k=='content_observed_at' and not parsed(value): value=v.get('observed_at')
                    if k=='source_published_at' and not parsed(value): value=v.get('published_at')
                    if parsed(value): times[k].append(value)
                from urllib.parse import urlsplit
                for link in (v.get('source_url'),v.get('url')):
                    if not isinstance(link,str): continue
                    try: address=urlsplit(link)
                    except ValueError: continue
                    if address.scheme in ('http','https') and address.hostname and not address.username and not address.password and link not in urls:
                        urls.append(link)
                for child in v.values(): scan(child)
            elif isinstance(v,list):
                for child in v: scan(child)
        scan(facts or {})
        clocks={k:min(v,key=parsed) if v else None for k,v in times.items()}
        blocked=bool(source and (source.last_error or source.retry_after_at and now<source.retry_after_at))
        queue='blocked' if blocked else 'authorized_adapter' if authorized else 'human_review_due' if due else 'human_review_queue'
        fallback=(facts or {}).get('fallback_review',{}) if isinstance(facts,dict) else {}
        if blocked or fallback.get('source_mode')=='blocked':mode='blocked'
        elif authorized:mode='authorized_automatic_adapter'
        elif source and source.adapter or kind=='target' and not urls or global_unknown:mode='human_review_required'
        else:mode='manual_official_review'
        reasons=[];priority=3;deadline=None
        tracked=bool(entity and entity.pk in {'opportunity':tracked_opportunities,'target':tracked_targets,'path':tracked_paths}.get(kind,set()))
        tier=entity.curation.get('fit_tier') if kind in ('opportunity','target') and entity else None
        pending=bool(entity and getattr(entity,'pending_change',False))
        stale=bool(due or entity and getattr(entity,'curation',{}).get('support_review_state')=='source_changed')
        if tracked:priority=0;reasons.append('saved_contacted_applied_or_decision')
        if tracked and (pending or stale):priority=0;reasons.append('tracked_conflict_or_stale')
        was_contact=bool(kind=='target' and entity and (entity.actionability=='contact_candidate' or entity.classification_evidence.get('previous_actionability')=='contact_candidate'))
        contact_conflict=bool(was_contact and (pending or entity.classification_evidence.get('status')=='source_changed'))
        if contact_conflict:priority=0;reasons.append('contact_candidate_conflict')
        if selected:
            if kind=='opportunity':
                dates=[d.boundary for d in entity.deadlines.all() if d.status=='verified' and d.date and d.effect in ('hard_close','priority','prerequisite') and d.boundary>=now]
                deadline=min(dates).isoformat() if dates else None
                if dates and min(dates)<=now+timedelta(days=30):priority=0;reasons.append('deadline_within_30_days')
                if entity.identity_fit in ('directly_applicable','potentially_applicable') and entity.effective_status=='open':priority=0;reasons.append('current_identity_open_programme')
                elif tier=='A' and entity.identity_fit in ('directly_applicable','potentially_applicable'):
                    priority=min(priority,1);reasons.append('selected_current_identity_tier_A')
                if priority>1 and entity.status=='unknown':priority=1;reasons.append('selected_unknown_status')
                if priority>2:priority=2;reasons.append('selected_tier_B')
            elif kind=='target':
                contact=entity.effective_actionability=='contact_candidate'
                if contact and category in ('host_receiving','lab_profile'):
                    priority=0;reasons.append('actionable_contact_identity_and_host')
                elif category=='funding_support' and contact:
                    priority=min(priority,1);reasons.append('contact_candidate_host_receiving_support')
                elif entity.classification_evidence.get('high_priority_watchlist') and category in ('host_receiving','coverage_identity','funding_support'):
                    priority=min(priority,1);reasons.append('high_priority_watchlist_host_or_support')
                elif entity.effective_actionability=='research_watchlist' and not entity.classification_evidence.get('coverage_only') and category not in ('alumni','academic_lineage') or category in ('projects','papers_awards') and contact:
                    priority=min(priority,2);reasons.append('research_watchlist_or_contact_enrichment')
            elif kind=='path':
                if priority>1:priority=1
                reasons.append('selected_visit_policy')
            elif kind=='coverage' and (global_unknown or (entity.review or {}).get('fallback_review',{}).get('newly_discovered')):
                priority=1;reasons.append('QS_unknown_or_newly_discovered')
            elif category in ('projects','papers_awards'):
                if priority>2:priority=2
                reasons.append('selected_projects_or_awards')
        if not reasons:reasons.append('alumni_lineage_or_retained_history')
        entry_id=hashlib.sha256((kind+'\0'+key+'\0'+category).encode()).hexdigest()[:24]
        display_name=(entity.title_zh or entity.title) if kind=='opportunity' else entity.group.name if kind=='target' else entity.name if kind=='path' else entity.official_name if kind=='coverage' else key
        category_label={'job_deadline':'机会与截止','visit_policy':'正式访问制度','funding_support':'实际访问支持','projects':'项目与期限','papers_awards':'代表作与奖项','alumni':'离组首站','academic_lineage':'一层师承','qs_ranking':'QS与相关入口',
            'programme':'项目与申请轮次','lab_profile':'导师身份与研究连接','host_receiving':'Host与接收条件','coverage_identity':'覆盖库身份复核','csc_channel':'CSC年度规则','yuanhang_channel':'远航项目通知','priority20_scope':'Priority-20手动范围复核'}[category]
        rows.append({'entry_id':entry_id,'kind':kind,'key':key,'display_name':display_name,'category_label':category_label,'category':category,'priority':priority,'priority_reasons':reasons,'pool_priority':'selected' if selected else 'retained_history','due':bool(due),'period_days':period,
            'mode':mode,'user_tracked':tracked,'next_deadline':deadline,'action_required':bool(due or pending or global_unknown),
            'field_verified_at':stamp,'fact_verified_at':stamp,**clocks,
            'fetch_time':clocks['fetched_at'],'content_observed_time':clocks['content_observed_at'],
            'human_verified_time':clocks['human_verified_at'],'source_publication_time':clocks['source_published_at'],
            'source_url':urls[0] if urls else url,'source_urls':urls or ([url] if url else []),'source_key':source.key if source else None,
            'queue':queue,'queue_label':{'blocked':'读取受阻 / 冷却','authorized_adapter':'获准范围 · 可显式单次执行','human_review_due':'已到期 · 需人工复核','human_review_queue':'需人工复核 / 按变化触发'}[queue],
            'trigger':'when_changed_or_new_release' if period is None else 'due_interval'})
    from .priority20 import policy as discovery_policy, registry
    scope_active=discovery_policy() is not None
    for o in Opportunity.objects.filter(is_test=False,is_published=True).select_related('source').prefetch_related('deadlines'):
        facts={'fetched_at':o.fetched_at.isoformat() if o.fetched_at else None,
            'source_published_at':o.source_published_at.isoformat() if o.source_published_at else o.curation.get('source_publication_date'),
            'content_observed_at':o.curation.get('content_observed_at') or o.curation.get('observed_content_date'),
            'human_verified_at':o.curation.get('human_verified_at')}
        category='programme' if scope_active and o.type in ('doctoral_program','program_round','doctoral_notice','visiting_route') else 'job_deadline'
        add('opportunity',o.key,category,o.verified_at.isoformat() if o.verified_at else None,o.url,o.curation.get('pool')=='selected',o.source,o,facts)
    for t in ResearchTarget.objects.filter(is_published=True):
        p=t.details.get('mentor_profile',{}); selected=t.curation.get('pool')=='selected'
        contact=t.effective_actionability=='contact_candidate';watch=t.effective_actionability=='research_watchlist' and not t.classification_evidence.get('coverage_only')
        conflict=(t.actionability=='contact_candidate' or t.classification_evidence.get('previous_actionability')=='contact_candidate') and (t.pending_change or t.classification_evidence.get('status')=='source_changed')
        contact_due=contact or conflict
        if scope_active:
            add('target',t.key+':identity','lab_profile' if contact_due else 'coverage_identity',t.classification_evidence.get('role_verified_at') or p.get('identity',{}).get('last_verified') or t.verified_at.isoformat(),t.url,selected,entity=t,facts={'identity':p.get('identity',{}),'role_evidence':t.classification_evidence.get('role_evidence',[])},interval='default' if contact_due else 90 if watch else 180)
            if contact_due or t.classification_evidence.get('high_priority_watchlist'):
                add('target',t.key+':host_receiving','host_receiving',t.classification_evidence.get('host_verified_at'),t.url,selected,entity=t,facts=t.classification_evidence)
        for category,key in [('funding_support','support'),('projects','projects'),('papers_awards','works'),('papers_awards','awards'),('alumni','alumni'),('academic_lineage','lineage')]:
            facts=p.get(key,{}); stamps=[]
            def collect(v):
                if isinstance(v,dict):
                    stamp=v.get('field_verified_at') or v.get('last_verified')
                    if parsed(stamp): stamps.append(stamp)
                    for child in v.values():collect(child)
                elif isinstance(v,list):
                    for child in v:collect(child)
            collect(facts)
            # Missing coverage enrichment is on-change work, not a daily fetch debt.
            interval='default' if contact_due and (stamps or category=='funding_support') else 90 if watch and stamps and category in ('projects','papers_awards','funding_support') else None
            add('target',t.key+':'+key,category,min(stamps,key=parsed) if stamps else None,t.url,selected,entity=t,facts=facts,interval=interval)
    for p in VisitPath.objects.filter(is_published=True):
        add('path',p.key,'visit_policy',p.verified_at.isoformat(),p.url,True,entity=p,facts=p.details)
    from .models import CoverageInstitution
    presence=coverage_presence_counts()
    for r in CoverageInstitution.objects.all():
        r._global_opportunity_count=presence.get(r.pk,0)
        add('coverage',r.key,'qs_ranking',r.last_checked_at.isoformat() if r.last_checked_at else None,(r.evidence[-1].get('url') if r.evidence else ''),True,entity=r,facts=r.review,global_unknown=r.global_relevant_opportunity_presence=='unknown')
        if scope_active:
            rows[-1].update(discovery_allowed=False,maintenance_scope='existing_QS_audit_only')
    if scope_active:
        from .actionability import host_compatibility
        channel_path=settings.BASE_DIR/'data/demo/yuanhang_current_status.json'
        yuanhang=json.loads(channel_path.read_text('utf8')).get('records',{}) if channel_path.exists() else {}
        for r in registry()['institutions']:
            channel=r['channel']; category='csc_channel' if channel['priority_channel']=='CSC' else 'yuanhang_channel'
            review=host_compatibility(r['key']) if category=='csc_channel' else yuanhang.get(r['key'],{})
            add('priority20',r['key']+':channel',category,review.get('checked_at') or r.get('last_verified_at'),channel.get('channel_url'),True,facts=review or channel)
            rows[-1].update(display_name=r['official_name'],priority=0,priority_reasons=['current_CSC_or_Yuanhang_cycle_review'],discovery_allowed=True,scope_class='priority20_new',human_verified_at=None,human_verified_time=None)
            add('priority20',r['key']+':scope','priority20_scope',r.get('last_verified_at'),r['official_url'],True,facts={'evidence':r.get('identity_evidence',[])})
            rows[-1].update(display_name=r['official_name'],mode='human_review_required',discovery_allowed=False,scope_change_requires_explicit_user_instruction=True)
    rows.sort(key=lambda r:(r['priority'],not r['action_required'],r['next_deadline'] or '9999',r['category'],r['key']))
    modes=('authorized_automatic_adapter','manual_official_review','human_review_required','blocked')
    summary={'total_entries':len(rows),'due_total':sum(r['due'] for r in rows),'action_required':sum(r['action_required'] for r in rows),'by_priority':{str(p):sum(r['priority']==p for r in rows) for p in range(4)},'by_mode':{m:sum(r['mode']==m for r in rows) for m in modes},
        'due_by_priority':{str(p):sum(r['priority']==p and r['due'] for r in rows) for p in range(4)},'due_by_mode':{m:sum(r['mode']==m and r['due'] for r in rows) for m in modes},
        'by_priority_and_mode':{str(p):dict(Counter(r['mode'] for r in rows if r['priority']==p)) for p in range(4)},'estimated_actions_by_mode':{m:sum(r['mode']==m and r['action_required'] for r in rows) for m in modes},'action_unit':'One field entry; multiple URLs may still require separate bounded review. These counts are not automatic fetches.'}
    manual=[r for r in rows if r['action_required'] and r['mode'] in ('manual_official_review','human_review_required')]
    # A planning range, not measured labor or a claim that one page resolves all fields.
    urls={r['source_url'] for r in manual if r['source_url']}
    critical=[r for r in rows if r['kind']=='target' and 'actionable_contact_identity_and_host' in r['priority_reasons'] and r['action_required']]
    summary['manual_burden_estimate']={'action_field_entries':len(manual),'distinct_primary_urls':len(urls),
        'minutes_per_entry_assumption':[3,10],'field_review_hours_range':[round(len(manual)*3/60,1),round(len(manual)*10/60,1)],
        'url_count_is_lower_bound':True,'basis':'Planning estimate; independent identity, route and support checks can share a page but unresolved cases need further work.'}
    summary['production_gate']={'critical_contact_identity_host_due':len(critical),
        'p0_p1_due':sum(r['due'] and r['priority']<=1 for r in rows),'scheduler_enabled':False,
        'status':'MANUAL_REVIEW_REQUIRED' if critical else 'READY_FOR_BOUNDED_MANUAL_MAINTENANCE',
        'automatic_production_ready':False,'reason':'Source permissions and manual evidence queues remain; no scheduler installed.'}
    snapshot=hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()
    return {'generated_at':now.isoformat(),'policy':policy,'scheduler_installed':False,'network_requests':0,'entries':rows,'summary':summary,'snapshot':snapshot,'private_tracking_scope':'current_user' if authenticated else 'local_operator_aggregate' if include_private else 'none',
        'priority_rule_scope':'P0 tracked/application, actionable identity/host and conflicts, near deadline, current identity open, CSC/Yuanhang cycle; P1 support and high-priority watchlist identity/host; P2 substantive watchlist/projects/awards; P3 coverage-only and alumni/lineage enrichment'}


def fact_rows(obj,now=None):
    now=now or timezone.now();periods=settings.FACT_REVIEW_DAYS;facts=[]
    def add(field,stamp,category):
        try: stamp=parse_datetime(stamp) if isinstance(stamp,str) else stamp
        except ValueError: stamp=None
        period=obj.details.get('review_period_days',periods[category]) if category in ('visit_policy','research') and hasattr(obj,'details') else periods[category]
        if not stamp or timezone.is_naive(stamp):
            facts.append({'field':field,'verified_at':None,'period_days':period,'due_at':None,'stale':True});return
        facts.append({'field':field,'verified_at':stamp.isoformat(),'period_days':period,'due_at':(stamp+timedelta(days=period)).isoformat(),'stale':stamp>now or now>=stamp+timedelta(days=period)})
    if isinstance(obj,Opportunity):
        for e in obj.evidence.all():
            category='deadline' if any(k in e.field.lower() for k in ('deadline','status','date')) else 'finance' if any(k in e.field.lower() for k in ('finance','fund','fee','salary','cash')) else 'research'
            add(e.field,e.verified_at,category)
    else:
        add('formal_visit_policy' if isinstance(obj,VisitPath) else 'research_and_identity',obj.verified_at,'visit_policy' if isinstance(obj,VisitPath) else 'research')
        for cost in obj.details.get('costs',[]):add('cost:'+cost.get('key','fee'),cost.get('verified_at') or obj.verified_at,'finance')
        proof=obj.details.get('minimum_funds') or {}
        if proof:add('minimum_funds',proof.get('verified_at') or obj.verified_at,'finance')
        for cash in obj.details.get('cash_facts',[]):add('cash:'+cash.get('key','cash'),cash.get('verified_at') or obj.verified_at,'finance')
        for action in obj.details.get('actions',[]):
            if action.get('date'):add('action:'+action.get('step_id','deadline'),action.get('verified_at') or obj.verified_at,'deadline')
        for anchor in obj.details.get('dossier',{}).get('research_anchors',[]):add('research:'+anchor.get('title','content'),anchor.get('verified_at') or obj.verified_at,'research')
    return facts

def maintenance_coverage(now=None):
    now=now or timezone.now(); rows=[]
    periods=settings.FACT_REVIEW_DAYS
    for kind, objects in [('opportunity',Opportunity.objects.filter(is_test=False)),('path',VisitPath.objects.filter(is_published=True)),('target',ResearchTarget.objects.filter(is_published=True))]:
        for obj in objects:
            links=SourceAlias.objects.filter(**{kind:obj})
            checked=max([l.checked_at for l in links if l.checked_at] or [None],key=lambda v:v.timestamp() if v else 0)
            facts=fact_rows(obj,now)
            rows.append({'kind':kind,'key':obj.key,'facts':facts,'checked_at':checked.isoformat() if checked else None,
                'method':'scoped_one_shot_recheck' if links.exists() else 'manual_review_queue',
                'sources':[l.source.key for l in links], 'due':any(f['stale'] for f in facts),
                'unknown_coverage':not facts,'note':'未被发现列表返回不代表关闭；取回未变不刷新事实核验时间。'})
    policies=[{'key':s.key,'automation_allowed':s.automation_allowed,'policy_checked_at':s.policy_checked_at.isoformat() if s.policy_checked_at else None,
        'policy_expired':not s.policy_checked_at or now>=s.policy_checked_at+timedelta(days=s.review_period_days),
        'last_error':s.last_error,'cooldown':s.retry_after_at.isoformat() if s.retry_after_at else None} for s in Source.objects.all()]
    return {'generated_at':now.isoformat(),'entities':rows,'source_policy':policies,'schedules_enabled':False,
        'bounded_grants_maintenance':'每次最多20详情，发现最多3；已纳管按checked_at最旧先轮转，超限留待下次手动调用。'}
