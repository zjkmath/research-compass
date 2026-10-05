from datetime import date
import json
from urllib.parse import urlsplit, urlunsplit, urlencode
from django.contrib import messages
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.paginator import Paginator
from django.db.models import Q
from django.http import HttpResponseBadRequest, HttpResponse
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.urls import reverse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST
from django.conf import settings
from .forms import FilterForm, RecordForm, RegistrationForm, TargetRecordForm, ProfileForm, BudgetForm, ContactForm
from .models import (Opportunity, Organization, Person, Program, Source, UserRecord, ResearchTarget,
    TargetRecord, SavedFilter, LocalProfile, UpdateRun, UpdateProposal, VisitPath, DecisionRecord)
from .decisions import (next_action, path_views, matches_path, profile_data, research_order, versions,
    calculate_budget, contact_template, decision_current, path_steps, budget_versions)
from .money import attach_comparison
from .updates import NOTICE, apply_proposal, reject_proposal
from .curation import selected_filter, selection_sort, attach_selection, support_key, mentor_context, dossier_summary, coverage_presence_counts
from .actionability import layer_counts, attach_actionability, contact_sort, action_filters

THEMES = [('neural_dynamics','神经动力学'),('control','控制与反馈'),('physiology','生理机制'),
    ('world_models','世界模型'),('embodied','具身与运动'),('bci','脑机接口'),('population_decoding','群体解码'),('applied_math','应用数学 / 生物数学')]

def safe_return(request, value, default='/targets/'):
    if value and len(value) < 3000 and url_has_allowed_host_and_scheme(value, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        parts = urlsplit(value)
        if parts.path in ('/', '/targets/', '/workspace/') or parts.path.startswith(('/targets/', '/opportunities/')):
            return urlunsplit(('', '', parts.path, parts.query, parts.fragment))
    return default

def public_opportunities():
    return Opportunity.objects.filter(is_test=False, is_published=True, review_status='verified', pending_change=False).select_related(
        'institution__coverage','source','program').prefetch_related('deadlines','financials','mentors')

def published_opportunities():
    # A pending revision does not erase an already reviewed public version.
    return Opportunity.objects.filter(is_test=False, is_published=True, review_status='verified').select_related('institution__coverage', 'source', 'program').prefetch_related('deadlines', 'financials', 'mentors')

def finish_list(request, records, form, saved_ids, template, extras=None):
    page = Paginator(records, 12).get_page(request.GET.get('page'))
    for item in page:
        path = 'targets' if template.endswith('targets.html') else 'opportunities'
        anchor = 'target' if path == 'targets' else 'opportunity'
        item.detail_url = f'/{path}/{item.pk}/?' + urlencode({'return_to': request.get_full_path() + f'#{anchor}-{item.pk}'})
        if path == 'targets': item.theme_labels = [dict(THEMES).get(code, code) for code in item.themes]
    query = request.GET.copy(); query.pop('page', None)
    context = {'form': form,'page': page,'result_count':len(records),'saved_ids':saved_ids,'query':query.urlencode(),
        'return_url':request.get_full_path(),'is_targets':template.endswith('targets.html'),
        'saved_filters':SavedFilter.objects.filter(user=request.user) if request.user.is_authenticated else []}
    active = []
    if form.is_bound and not form.errors:
        for key,value in form.cleaned_data.items():
            if not value or key in ('sort','basis','page'): continue
            field = form.fields[key]; options = dict(getattr(field,'choices',[]))
            for v in value if isinstance(value,list) else [value]:
                removed=request.GET.copy();removed.pop('page',None)
                remaining=[x for x in removed.getlist(key) if x!=str(v)] if isinstance(value,list) else []
                if remaining:removed.setlist(key,remaining)
                else:removed.pop(key,None)
                active.append({'label':field.label,'value':options.get(v, '已启用' if v is True else v),'remove_url':request.path+'?'+removed.urlencode()})
    context['active_filters'] = active
    context.update(extras or {}); return render(request,template,context)

def opportunity_list(request):
    base = published_opportunities()
    choices = {name:[(x,x) for x in base.order_by(name).values_list(name,flat=True).distinct()] for name in ('region','country','discipline')}
    choices.update(theme=THEMES,type=Opportunity.TYPES, funding=Opportunity._meta.get_field('funding_status').choices,
        source=[(s.key,s.name) for s in Source.objects.filter(opportunity__in=base).distinct()])
    form=FilterForm(request.GET or None,choices=choices)
    all_records=list(base)
    stats={'positions':sum(o.type=='position' and o.effective_status=='open' for o in all_records),
        'rounds':sum(o.type=='program_round' and o.effective_status in ('open','upcoming') for o in all_records),
        'scholarships':sum(o.type=='scholarship' for o in all_records),
        'total':len(all_records),'regions':len(set(o.region for o in all_records))}
    stats.update(open_total=sum(o.effective_status=='open' and not o.pending_change for o in all_records),
        identity_direct=sum(o.identity_fit=='directly_applicable' for o in all_records),
        identity_potential=sum(o.identity_fit=='potentially_applicable' for o in all_records),
        identity_open=sum(o.effective_status=='open' and not o.pending_change and o.identity_fit in ('directly_applicable','potentially_applicable') for o in all_records),
        identity_potential_open=sum(o.effective_status=='open' and not o.pending_change and o.identity_fit=='potentially_applicable' for o in all_records),
        identity_direct_open=sum(o.effective_status=='open' and not o.pending_change and o.identity_fit=='directly_applicable' for o in all_records))
    data=form.cleaned_data if form.is_bound and form.is_valid() else {}
    items=base.none() if form.is_bound and form.errors else base
    for term in data.get('q','').split():
        items=items.filter(Q(title__icontains=term)|Q(title_zh__icontains=term)|Q(summary_zh__icontains=term)|
            Q(institution__name__icontains=term)|Q(discipline__icontains=term)|Q(mentors__name__icontains=term))
    for field in ('region','country','discipline','type'):
        if data.get(field): items=items.filter(**{field+'__in':data[field]})
    if data.get('funding'): items=items.filter(funding_status__in=data['funding'])
    if data.get('source'): items=items.filter(source__key__in=data['source'])
    statuses=data.get('status',[])
    records=[o for o in items.distinct() if ('all' in statuses or data.get('history') or o.effective_status in statuses or not statuses and o.effective_status!='closed')]
    records=selected_filter(records,data)
    records=action_filters(records,data)
    if data.get('identity_fit'):records=[o for o in records if o.identity_fit in data['identity_fit']]
    # Salary/future-stage browsing remains available through an explicit all-identity view.
    identity_scope=data.get('identity_scope') or 'phd_enrolled'
    if identity_scope=='phd_enrolled':records=[o for o in records if o.identity_fit in ('directly_applicable','potentially_applicable')]
    personal={r.opportunity_id:r for r in UserRecord.objects.filter(user=request.user)} if request.user.is_authenticated else {}
    for obj in records: obj.action_view = next_action(obj,personal.get(obj.pk))
    for field,compare in [('deadline_from',lambda a,b:a>=b),('deadline_to',lambda a,b:a<=b)]:
        if data.get(field): records=[o for o in records if o.action_view['date'] and compare(o.action_view['date'],data[field])]
    if data.get('known_date'): records=[o for o in records if o.action_view['date']]
    fx=attach_comparison(records,data.get('basis') or 'gross_salary')
    if data.get('cash_only'): records=[o for o in records if o.comparison]
    if data.get('sort')=='deadline': records.sort(key=lambda o:(o.action_view['date'] or date.max,o.key))
    elif data.get('sort')=='changed': records.sort(key=lambda o:(-o.updated_at.timestamp(),o.key))
    elif data.get('sort') in ('reviewed',''): records.sort(key=lambda o:(-(o.verified_at.timestamp() if o.verified_at else 0),o.key))
    selection_sort(records,data.get('sort'),data.get('theme') or profile_data(request.user).get('themes') or [])
    if data.get('sort') in ('salary','support'): records.sort(key=lambda o:support_key(o.comparison,o.key))
    saved_ids=set(UserRecord.objects.filter(user=request.user,saved=True).values_list('opportunity_id',flat=True)) if request.user.is_authenticated else set()
    return finish_list(request,records,form,saved_ids,'opportunities/list.html',{'stats':stats,'fx':fx,'basis':data.get('basis') or 'gross_salary',
        'coverage':json.loads((settings.BASE_DIR/'data/demo/coverage.json').read_text(encoding='utf8')) if (settings.BASE_DIR/'data/demo/coverage.json').exists() else None,
        'basis_label':dict(form.fields['basis'].choices).get(data.get('basis') or 'gross_salary'),
        'identity_scope':identity_scope,
        'heading_title':'适合当前博士身份的机会' if identity_scope=='phd_enrolled' else {'all':'全部保留机会','candidate':'候选机会 · 条件待补证','historical':'历史机会'}.get(data.get('pool'),'精选机会')})

def target_list(request):
    targets=list(ResearchTarget.objects.filter(is_published=True).select_related('institution__coverage','group').prefetch_related('mentors','paths'))
    evidence_summary=dossier_summary([t for t in targets if t.curation.get('pool')=='selected'])
    layers=layer_counts([t for t in targets if t.curation.get('pool')=='selected'])
    candidate_count=sum(t.curation.get('pool')=='candidate' for t in targets)
    choices={name:sorted({(getattr(t,name),getattr(t,name)) for t in targets}) for name in ('region','country')}
    choices.update(theme=THEMES,receiving=ResearchTarget.RECEIVING,type=[('visiting_student','访问学生'),('visiting_researcher','访问研究人员')],
        funding=[('unknown','资金未知'),('external','外部资金条件'),('paid','已核验现金支持'),('self_funded','自备资金')])
    form=FilterForm(request.GET or None,choices=choices,targets=True)
    data=form.cleaned_data if form.is_bound and form.is_valid() else {}
    if form.is_bound and form.errors: targets=[]
    targets=selected_filter(targets,data)
    targets=action_filters(targets,data)
    layer=data.get('layer') or 'contact'
    chosen={'contact':'contact_candidate','watchlist':'research_watchlist','reference':'reference_only'}
    if layer in chosen:targets=[t for t in targets if t.effective_actionability==chosen[layer]]
    elif layer=='deep':targets=[t for t in targets if t.details.get('dossier',{}).get('level')=='deep']
    for field in ('region','country'):
        if data.get(field): targets=[t for t in targets if getattr(t,field) in data[field]]
    if data.get('theme'): targets=[t for t in targets if set(t.themes)&set(data['theme'])]
    for term in data.get('q','').split():
        aliases=[code for code,label in THEMES if term.lower() in (code.lower(),label)]
        targets=[t for t in targets if term.casefold() in (t.institution.name+' '+t.group.name+' '+
            ' '.join(p.name for p in t.mentors.all())+' '+json.dumps(t.details,ensure_ascii=False)).casefold() or set(aliases)&set(t.themes)]
    if not data.get('history'): targets=[t for t in targets if t.receiving_status not in ('closed','historical')]
    if data.get('physiology'): targets=[t for t in targets if t.details.get('physiology_basis') and 'physiology' in t.themes]
    personal={r.target_id:r for r in TargetRecord.objects.filter(user=request.user)} if request.user.is_authenticated else {}
    kept=[]
    for target in targets:
        attach_actionability(target)
        available_paths=path_views(target,request.user,personal.get(target.pk),data.get('basis') or 'gross_salary')
        target.path_views=[row for row in available_paths if matches_path(row,data)]
        path_filters=('type','funding','external_proof','cash_only','eligibility_verified','receiving_verified','known_date','deadline_from','deadline_to')
        if not target.path_views and (data.get('receiving') or any(data.get(k) for k in path_filters)):
            # An unknown L1 with no formal route is still a valid unknown result.
            # Never borrow target-level facts when real paths fail a joint filter.
            if available_paths or any(data.get(k) for k in path_filters) or target.receiving_status not in data.get('receiving',[]):
                continue
        if data.get('sort')=='deadline': target.path_views.sort(key=lambda r:(r['action']['date'] or date.max,r['path'].key))
        if data.get('sort') in ('salary','support'): target.path_views.sort(key=lambda r:support_key(r['comparison'],r['path'].key))
        target.selected_path=target.path_views[0] if target.path_views else None
        target.compare_url=reverse('compare'); target.budget_url=reverse('budget',args=[target.pk]); target.contact_url=reverse('contact',args=[target.pk])
        kept.append(target)
    targets=kept
    themes=data.get('theme') or profile_data(request.user).get('themes') or []
    order=data.get('sort')
    if order=='deadline': targets.sort(key=lambda t:((t.selected_path['action']['date'] if t.selected_path else None) or date.max,t.key))
    elif order=='changed': targets.sort(key=lambda t:(-t.updated_at.timestamp(),t.key))
    elif order in ('salary','support'): targets.sort(key=lambda t:support_key(t.selected_path['comparison'] if t.selected_path else None,t.key))
    else: targets.sort(key=lambda t:(-t.verified_at.timestamp(),t.key))
    selection_sort(targets,order,themes)
    if layer=='contact' and order in ('','match','actionable'):contact_sort(targets)
    ids=set(TargetRecord.objects.filter(user=request.user,saved=True).values_list('target_id',flat=True)) if request.user.is_authenticated else set()
    return finish_list(request,targets,form,ids,'opportunities/targets.html',{'evidence_summary':evidence_summary,'candidate_count':candidate_count,'layers':layers,'layer':layer,
        'heading_title':{'contact':'精选可联系导师','watchlist':'研究观察名单','reference':'参考科研人员','coverage':'相关科研人员','deep':'重点深档'}[layer],
        'sort_note':{'support':'同路径周期现金；按年度、税口径和原币分组，同组金额降序，未知排后。','salary':'只比较同路径已核验周期现金；按年度、税口径分组，同组金额从高到低，未知排后。','match':'优先直接神经生理机制，再比较研究连接；不代表资格或录取概率。','completeness':'按六个已证档案维度的数量排序，不是导师实力评分。','deadline':'同一匹配路径下一未完成行动，未知日期排后。','changed':'最近实体事实变化在前。','reviewed':'最近事实核验在前。'}.get(data.get('sort'),'直接神经 / 生理匹配优先；方法延伸单独标注，目录不代表接收名额。')})

def record_steps(opportunity):
    return [(d.completion_key,d.label) for d in opportunity.deadlines.all() if d.effect=='prerequisite']

def target_record_steps(target):
    return [(d.completion_key,p.name+' · '+d.label) for p in target.paths.filter(is_published=True) for d in path_steps(p) if d.actor=='applicant' and d.effect in ('prerequisite','informational')]

def detail(request,pk,record_form=None):
    obj=get_object_or_404(published_opportunities().prefetch_related('evidence','related_funding','funding_relations__award'),pk=pk)
    raw=obj.curation.get('source_review',{}).get('evidence',[])+obj.curation.get('research_evidence',[])
    date_only={(e.get('url'),e.get('short_quote') or e.get('quote','')) for e in raw
        if e.get('timestamp_precision')=='day' or len(str(e.get('observed_at','')))==10}
    for e in obj.evidence.all():
        e.observed_date_only=(e.url,e.quote) in date_only
    obj.record_observed_date_only=any((e.get('url'),e.get('short_quote') or e.get('quote','')) in date_only
        and str(e.get('observed_at') or e.get('verified_at',''))[:10]==obj.verified_at.date().isoformat() for e in raw)
    attach_selection(obj)
    from .priority20 import scope_info
    obj.priority_scope=scope_info(obj)
    record=UserRecord.objects.filter(user=request.user,opportunity=obj).first() if request.user.is_authenticated else None
    obj.action_view=next_action(obj,record)
    attach_comparison([obj])
    return render(request,'opportunities/detail.html',{'opportunity':obj,'record':record,'record_form':record_form or RecordForm(instance=record,steps=record_steps(obj)),
        'return_url':safe_return(request,request.GET.get('return_to'),'/'),
        'prerequisite_warnings':obj.action_view['warnings'],
        'related_targets':ResearchTarget.objects.filter(is_published=True,institution=obj.institution,mentors__in=obj.mentors.all()).distinct().prefetch_related('mentors'),
        'related_funding':obj.related_funding.filter(review_status='verified',is_test=False)})

def target_detail(request,pk,record_form=None):
    obj=get_object_or_404(ResearchTarget.objects.filter(is_published=True).select_related('group','institution__coverage').prefetch_related('mentors','paths'),pk=pk)
    attach_selection(obj)
    attach_actionability(obj)
    from .priority20 import scope_info
    obj.priority_scope=scope_info(obj)
    record=TargetRecord.objects.filter(user=request.user,target=obj).first() if request.user.is_authenticated else None
    obj.path_views=path_views(obj,request.user,record); obj.selected_path=obj.path_views[0] if obj.path_views else None
    related_opportunities=published_opportunities().filter(institution=obj.institution,mentors__in=obj.mentors.all()).distinct()
    obj.budget_url=reverse('budget',args=[pk]); obj.contact_url=reverse('contact',args=[pk]); obj.compare_url=reverse('compare')
    return render(request,'opportunities/target_detail.html',{'target':obj,'record':record,'related_opportunities':related_opportunities,'record_form':record_form or TargetRecordForm(instance=record,steps=target_record_steps(obj)),
        'return_url':safe_return(request,request.GET.get('return_to')),**mentor_context(obj)})

@login_required
@require_POST
def save_record(request,pk):
    obj=get_object_or_404(published_opportunities(),pk=pk)
    record,_=UserRecord.objects.get_or_create(user=request.user,opportunity=obj)
    if request.POST.get('action')=='bookmark':
        record.saved=not record.saved; record.save(update_fields=['saved','updated_at'])
    else:
        form=RecordForm(request.POST,instance=record,steps=record_steps(obj))
        if not form.is_valid():
            response=detail(request,pk,record_form=form);response.status_code=400;return response
        form.save()
    target=request.POST.get('return_to')
    if target=='list': target=request.META.get('HTTP_REFERER')
    return redirect(safe_return(request,target,f'/opportunities/{pk}/'))

@login_required
@require_POST
def save_target(request,pk):
    obj=get_object_or_404(ResearchTarget,is_published=True,pk=pk); record,_=TargetRecord.objects.get_or_create(user=request.user,target=obj,defaults={'saved':False})
    if request.POST.get('action')=='bookmark': record.saved=not record.saved; record.save(update_fields=['saved','updated_at'])
    else:
        form=TargetRecordForm(request.POST,instance=record,steps=target_record_steps(obj))
        if not form.is_valid():
            response=target_detail(request,pk,record_form=form);response.status_code=400;return response
        form.save()
    return redirect(safe_return(request,request.POST.get('return_to'),f'/targets/{pk}/'))

@login_required
@require_POST
def save_filter(request):
    name=request.POST.get('name','').strip()[:80]
    if not name: return HttpResponseBadRequest('请输入筛选名称')
    value=safe_return(request,request.POST.get('return_to'))
    parts=urlsplit(value)
    from django.http import QueryDict
    query=QueryDict(parts.query)
    SavedFilter.objects.update_or_create(user=request.user,name=name,defaults={'path':parts.path,'query':dict(query.lists())})
    return redirect(value)

@login_required
def use_filter(request,pk):
    obj=get_object_or_404(SavedFilter,user=request.user,pk=pk)
    from django.http import QueryDict
    query=QueryDict(mutable=True)
    for key,values in obj.query.items(): query.setlist(key,values)
    return redirect(safe_return(request,obj.path+'?'+query.urlencode()))

@login_required
def workspace(request):
    records=UserRecord.objects.filter(user=request.user).filter(Q(saved=True)|~Q(stage='none')|~Q(note='')).select_related('opportunity','opportunity__institution').order_by('-updated_at')
    targets=TargetRecord.objects.filter(user=request.user).select_related('target','target__group').order_by('-updated_at')
    records=list(records)
    for rec in records: rec.opportunity.action_view=next_action(rec.opportunity,rec)
    if request.GET.get('sort')=='action':records.sort(key=lambda r:(str(r.opportunity.action_view.get('date') or '9999'),-r.pk))
    for rec in targets:
        rec.target.path_views=path_views(rec.target,request.user,rec)
        rec.target.selected_path=rec.target.path_views[0] if rec.target.path_views else None
    decisions=list(DecisionRecord.objects.filter(user=request.user).select_related('target','path').order_by('-updated_at'))
    for rec in decisions:
        rec.state_label='版本一致 · 证据在复核期内' if decision_current(rec,request.user) else '来源或私人条件已变 · 需复核'
        rec.url=reverse(rec.kind if rec.kind!='comparison' else 'compare',args=[rec.target_id] if rec.target_id and rec.kind!='comparison' else [])+'?saved='+str(rec.pk)
    return render(request,'opportunities/workspace.html',{'records':records,'targets':targets,'profile':LocalProfile.objects.filter(user=request.user).first(),
        'budget_records':[r for r in decisions if r.kind=='budget'],'contact_records':[r for r in decisions if r.kind=='contact'],
        'comparison_records':[r for r in decisions if r.kind=='comparison']})

def sources(request):
    from .maintenance import maintenance_coverage, curated_due_queue
    maintenance=maintenance_coverage()
    refresh_queue=curated_due_queue(user=request.user)
    refresh_queue['page_entries']=refresh_queue['entries'][:25]
    return render(request,'opportunities/sources.html',{'sources':Source.objects.order_by('region','name'),'manual_count':public_opportunities().filter(source__adapter='').count(),
        'coverage':json.loads((settings.BASE_DIR/'data/demo/coverage.json').read_text(encoding='utf8')) if (settings.BASE_DIR/'data/demo/coverage.json').exists() else None,
        'auto_count':public_opportunities().exclude(source__adapter='').count(), 'target_count':ResearchTarget.objects.count(),
        'runs':UpdateRun.objects.select_related('source').order_by('-pk')[:20],'notice':NOTICE,
        'due_count':sum(row['due'] for row in maintenance['entities']),'maintenance':maintenance,'refresh_queue':refresh_queue})

def coverage_registry(request):
    from collections import Counter
    from .models import CoverageInstitution
    from .maintenance import refresh_policy
    entries=list(CoverageInstitution.objects.all());presence=coverage_presence_counts()
    statuses=dict(CoverageInstitution.AUDIT_SCOPE_CHOICES)
    globals_=dict(CoverageInstitution.GLOBAL_PRESENCE_CHOICES)
    for institution in entries:institution._global_opportunity_count=presence.get(institution.pk,0)
    counts=Counter(i.audit_scope_status for i in entries)
    global_counts=Counter(i.global_relevant_opportunity_presence for i in entries)
    audit_selected=request.GET.getlist('audit_scope_status') or request.GET.getlist('status')
    audit_selected=['relevant_found_via_declared_qs_audit' if v=='relevant_opportunity_found' else v for v in audit_selected]
    global_selected=request.GET.getlist('global_presence')
    if audit_selected:entries=[i for i in entries if i.audit_scope_status in audit_selected]
    if global_selected:entries=[i for i in entries if i.global_relevant_opportunity_presence in global_selected]
    if request.GET.get('q'):
        terms=request.GET['q'].casefold();entries=[i for i in entries if terms in (i.official_name+' '+i.country).casefold()]
    scope_labels={'central_graduate':'研究生院 / 招生','relevant_department':'相关院系','careers_or_opportunities':'招聘 / 研究机会','relevant_lab':'相关实验室'}
    state_labels={'read':'已读声明入口','partial':'仅部分正文 / 动态入口','blocked':'入口读取受阻','missing':'本轮导航未找到合适入口','not_discovered':'本轮导航未找到合适入口','not_found':'本轮导航未找到合适入口'}
    for institution in entries:
        institution.scope_rows=[{**v,'label':scope_labels.get(k,k),'state_label':state_labels.get(v.get('status'),v.get('status'))} for k,v in institution.review.get('declared_scope',{}).items()]
    return render(request,'opportunities/coverage.html',{'institutions':entries,'total':sum(counts.values()),
        'counts':[(k,statuses[k],counts[k]) for k in statuses],
        'global_counts':[(k,globals_[k],global_counts[k]) for k in globals_],
        'audit_selected':audit_selected,'global_selected':global_selected,'refresh_policy':refresh_policy()})


@user_passes_test(lambda user:user.is_staff)
@require_POST
def recheck_source(request,pk):
    from .updates import run_source
    source=get_object_or_404(Source,pk=pk)
    if not source.adapter: return HttpResponseBadRequest('此来源只支持人工整理，不自动读取')
    run=run_source(source,manual_read=source.access_method=='manual')
    messages.success(request,f'单次读取 #{run.pk}：{run.status}；新提案 {run.proposed} / 未变 {run.unchanged}。读取未变不刷新事实核验年龄。')
    return redirect('sources')

@user_passes_test(lambda user:user.is_staff)
def update_review(request):
    proposals=UpdateProposal.objects.select_related('source','run').order_by('-pk')
    if request.method=='POST':
        obj=get_object_or_404(proposals,pk=request.POST.get('proposal'))
        try:
            if request.POST.get('action') == 'reject': reject_proposal(obj,request.POST.get('review_note','')); messages.success(request,'已拒绝提案，保留公开历史及私人记录')
            else: apply_proposal(obj,request.POST.get('review_note',''),adjudicate=request.POST.get('action')=='adjudicate'); messages.success(request,'已应用经审核的提案')
        except Exception as error: messages.error(request,'审核未应用：'+str(error)[:250])
        return redirect('update_review')
    return render(request,'opportunities/update_review.html',{'proposals':proposals[:30],'runs':UpdateRun.objects.select_related('source').order_by('-pk')[:20]})

def directory(request):
    return render(request,'opportunities/directory.html',{'organizations':Organization.objects.all(),
        'programs':Program.objects.select_related('institution').prefetch_related('mentors'),'people':Person.objects.select_related('organization')})

def assert_decision_visible(record):
    """Owner is checked by caller; published history is distinct from never-public drafts."""
    choices=record.inputs.get('choices',[]) if record.kind=='comparison' else [{'target':record.target_id,'path':record.path_id}]
    current=True
    for choice in choices:
        target=get_object_or_404(ResearchTarget,is_published=True,pk=choice['target'])
        if record.kind=='comparison' and choice.get('path') is None:continue
        if not target.paths.filter(is_published=True,pk=choice['path']).exists():
            if not any(p['target']==choice['target'] and p['path']==choice['path'] and p.get('published_checked_at') for p in record.public_paths):
                from django.http import Http404
                raise Http404
            current=False
    return current


def historical_decision(request,record):
    return render(request,'opportunities/historical_decision.html',{'saved':record,'historical_inputs':json.dumps(record.inputs,ensure_ascii=False,indent=2),
        'historical_result':json.dumps(record.result,ensure_ascii=False,indent=2)})


def json_safe(value):
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))

@login_required
def profile(request):
    obj = LocalProfile.objects.filter(user=request.user).first()
    initial = dict(obj.data) if obj else {}
    for key in ('enrolled','higher_degree','doctoral_enrolled','independent','host_support','funds_confirmed','agreement_ready','home_endorsement','self_funded','stipend_required'):
        if isinstance(initial.get(key),bool): initial[key] = 'true' if initial[key] else 'false'
    form = ProfileForm(request.POST or None, initial=initial)
    if request.method == 'POST' and form.is_valid():
        data=json_safe(form.cleaned_data)
        # Preserve legacy support values as unassigned history, never apply to every host.
        for field in ('host_support','agreement_ready'):
            if field in initial: data[field]=obj.data[field]
        LocalProfile.objects.update_or_create(user=request.user, defaults={'data':data})
        messages.success(request,'私人条件已保存；旧资格、预算与准备稿须按新版本核对。')
        return redirect('workspace')
    return render(request,'opportunities/profile.html',{'form':form})

@login_required
def compare(request):
    from django import forms
    class CompareForm(forms.Form):
        choices = forms.MultipleChoiceField(label='选择2–4个目标；有路径时逐条比较，无路径时只比较研究证据', widget=forms.CheckboxSelectMultiple)
    targets = list(ResearchTarget.objects.filter(is_published=True).select_related('group','institution').prefetch_related('paths','mentors'))
    options = [(f'{t.pk}:{p.pk}',t.group.name+' · '+p.name) for t in targets for p in t.paths.filter(is_published=True)]
    options += [(f'{t.pk}:0',t.group.name+' · 正式访问路径未确认') for t in targets if not t.paths.filter(is_published=True).exists()]
    saved = get_object_or_404(DecisionRecord,user=request.user,pk=request.GET['saved'],kind='comparison') if request.GET.get('saved') else None
    if request.method=='GET' and saved and not assert_decision_visible(saved): return historical_decision(request,saved)
    form=CompareForm(request.POST or None,initial={'choices':[f"{r['target']}:{r['path'] or 0}" for r in saved.inputs['choices']]} if saved else {})
    form.fields['choices'].choices=options
    rows=[]
    if request.method=='POST': saved=None
    if request.method=='POST' and form.is_valid():
        selected=form.cleaned_data['choices']
        if not 2 <= len(selected) <= 4: form.add_error('choices','请选择2–4个不同目标')
        elif len({x.split(':')[0] for x in selected}) != len(selected): form.add_error('choices','每个目标选择一条正式路径，避免拼接条件')
        else:
            choices=[]; snapshots=[]
            for pair in selected:
                target_id,path_id=map(int,pair.split(':')); target=next(t for t in targets if t.pk==target_id)
                rec=TargetRecord.objects.filter(user=request.user,target=target).first()
                row=next(row for row in path_views(target,request.user,rec) if row['path'].pk==path_id) if path_id else {'path':None,'eligibility':{'label':'制度、个人资格均未确认'},'action':{'label':'先核正式身份与接收院系'}}
                rows.append({**row,'target':target,'dossier':target.details.get('dossier',{})})
                choices.append({'target':target_id,'path':path_id or None}); snapshots.append(versions(target,row['path'],request.user))
            if request.POST.get('action')=='save':
                saved=DecisionRecord.objects.create(user=request.user,kind='comparison',inputs={'choices':choices},
                    result={'labels':[row['eligibility']['label'] for row in rows]},versions={'rows':snapshots})
                messages.success(request,'私人比较已保存；路径与画像版本保留。')
    elif saved:
        for choice in saved.inputs['choices']:
            target=next((t for t in targets if t.pk==choice['target']),None)
            if target:
                rec=TargetRecord.objects.filter(user=request.user,target=target).first()
                row=next((r for r in path_views(target,request.user,rec) if r['path'].pk==choice['path']),None) if choice.get('path') else {'path':None,'eligibility':{'label':'制度、个人资格均未确认'},'action':{'label':'先核正式身份与接收院系'}}
                if row: rows.append({**row,'target':target,'dossier':target.details.get('dossier',{})})
    from .priority20 import scope_info
    for row in rows:
        attach_selection(row['target']);attach_actionability(row['target']);row['target'].priority_scope=scope_info(row['target'])
    return render(request,'opportunities/compare.html',{'form':form,'rows':rows,'saved':saved,'stale':saved and not decision_current(saved,request.user),
        'return_url':safe_return(request,request.GET.get('return_to'))})

@login_required
def budget(request,pk):
    target=get_object_or_404(ResearchTarget,is_published=True,pk=pk); paths=list(target.paths.filter(is_published=True))
    attach_actionability(target)
    saved=get_object_or_404(DecisionRecord,user=request.user,pk=request.GET['saved'],kind='budget',target=target) if request.GET.get('saved') else None
    if request.method=='GET' and saved and not request.GET.get('path') and not assert_decision_visible(saved): return historical_decision(request,saved)
    selected=get_object_or_404(target.paths.filter(is_published=True),pk=request.GET['path']) if request.GET.get('path') else paths[0] if paths else None
    currencies={c.get('currency') for c in selected.details.get('costs',[]) if c.get('currency')} if selected else set()
    if selected and (selected.details.get('minimum_funds') or {}).get('currency'): currencies.add(selected.details['minimum_funds']['currency'])
    form=BudgetForm(request.POST or None,paths=paths,initial=saved.inputs if saved else {'path':selected.pk if selected else '', 'currency':next(iter(currencies)) if len(currencies)==1 else ''})
    result=None; path=saved.path if saved else selected
    if saved: get_object_or_404(target.paths.filter(is_published=True),pk=saved.path_id)
    if request.method=='POST': saved=None
    if request.method=='POST' and form.is_valid():
        path=next(p for p in paths if p.pk==int(form.cleaned_data['path']))
        try: result=calculate_budget(path,form.cleaned_data)
        except ValidationError as error: form.add_error(None,error)
        else:
            if request.POST.get('action')=='save':
                saved=DecisionRecord.objects.create(user=request.user,target=target,path=path,kind='budget',inputs=json_safe(form.cleaned_data),result=json_safe(result),versions=budget_versions(target,path,request.user,form.cleaned_data))
                messages.success(request,'预算输入、事实与规则版本已保存。')
    elif saved: result=saved.result
    from django.conf import settings
    examples=json.loads((settings.BASE_DIR/'data/budget_examples.json').read_text(encoding='utf8')) if (settings.BASE_DIR/'data/budget_examples.json').exists() else []
    if path:
        path.display_details={k:(v.replace('unknown','待核验').replace('not_specified','原文未说明') if isinstance(v,str) else v) for k,v in path.details.items()}
    return render(request,'opportunities/budget.html',{'target':target,'path':path,'form':form,'result':result,'saved':saved,
        'stale':saved and not decision_current(saved,request.user),'examples':examples,
        'budget_income_fields':[form[k] for k in ('income_confirmed','support_once','available_cash')],
        'budget_monthly_fields':[form[k] for k in ('living','housing','insurance','transport')],
        'budget_upfront_fields':[form[k] for k in ('deposit','prepaid')],
        'budget_optional_fields':[form[k] for k in ('research_mode','STP_application','STP_issuance','multiple_journey_visa_if_applicable','display_currency','fx_version','historical_fx')]})

@login_required
def contact(request,pk):
    target=get_object_or_404(ResearchTarget,is_published=True,pk=pk); paths=list(target.paths.filter(is_published=True))
    attach_actionability(target)
    saved=get_object_or_404(DecisionRecord,user=request.user,pk=request.GET['saved'],kind='contact',target=target) if request.GET.get('saved') else None
    if request.method=='GET' and saved and not request.GET.get('path') and not assert_decision_visible(saved): return historical_decision(request,saved)
    selected=get_object_or_404(target.paths.filter(is_published=True),pk=request.GET['path']) if request.GET.get('path') else paths[0] if paths else None
    path=saved.path if saved else selected
    if saved: get_object_or_404(target.paths.filter(is_published=True),pk=saved.path_id)
    initial=saved.inputs if saved else {'path':path.pk if path else '', 'draft':contact_template(target,path) if path else '[正式访问路径待确认]',
        'outline':'[科学问题] [生理对象] [数学/控制方法] [最小验证] [所需数据/技能] [本人已核实成果] [拟起止日期]'}
    form=ContactForm(request.POST or None,paths=paths,initial=initial)
    if request.method=='POST': saved=None
    if request.method=='POST' and form.is_valid():
        path=next(p for p in paths if p.pk==int(form.cleaned_data['path']))
        saved=DecisionRecord.objects.create(user=request.user,target=target,path=path,kind='contact',inputs=json_safe(form.cleaned_data),
            result={'prepared_only':True,'not_sent':True},versions=versions(target,path,request.user))
        messages.success(request,'准备稿仅在本地保存，没有发送。')
    return render(request,'opportunities/contact.html',{'target':target,'path':path,'form':form,'saved':saved,
        'stale':saved and not decision_current(saved,request.user)})

@login_required
def export_decision(request,pk):
    rec=get_object_or_404(DecisionRecord,user=request.user,pk=pk)
    assert_decision_visible(rec)
    payload={'kind':rec.kind,'inputs':rec.inputs,'result':rec.result,'versions':rec.versions,
        'published_path_headers':rec.public_paths,
        'current':decision_current(rec,request.user),'notice':'私人本地导出；不发送或订阅外部服务'}
    response=HttpResponse(json.dumps(payload,ensure_ascii=False,indent=2),content_type='application/json; charset=utf-8')
    response['Content-Disposition']=f'attachment; filename="research-compass-{rec.pk}.json"'
    response['Cache-Control']='private, no-store'; return response

def register(request):
    from django.conf import settings
    if not settings.ALLOW_REGISTRATION:
        from django.http import Http404
        raise Http404
    form=RegistrationForm(request.POST or None)
    if request.method=='POST' and form.is_valid():
        login(request,form.save()); return redirect(safe_return(request,request.GET.get('next'),'/workspace/'))
    return render(request,'registration/register.html',{'form':form})


@login_required
def path_support(request,pk,path_id):
    from .models import PathSupport
    from .forms import PathSupportForm
    target=get_object_or_404(ResearchTarget,is_published=True,pk=pk)
    path=get_object_or_404(target.paths.filter(is_published=True),pk=path_id)
    obj=PathSupport.objects.filter(user=request.user,target=target,path=path).first()
    initial={k:getattr(obj,k) for k in ('host_support','agreement_ready','evidence_note','valid_until')} if obj else {}
    for key in ('host_support','agreement_ready'):
        if isinstance(initial.get(key),bool): initial[key]='true' if initial[key] else 'false'
    form=PathSupportForm(request.POST or None,initial=initial)
    if request.method=='POST' and form.is_valid():
        PathSupport.objects.update_or_create(user=request.user,target=target,path=path,defaults=form.cleaned_data)
        messages.success(request,'已保存这位导师、这条路径的私人确认；其他目标保持原状。')
        return redirect('target_detail',pk=pk)
    return render(request,'opportunities/path_support.html',{'target':target,'path':path,'form':form})
