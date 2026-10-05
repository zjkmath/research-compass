"""Explainable, reviewed selection. Membership never controls private record retention."""
from datetime import date
from django.utils import timezone

REASONS = [('qs_top100','QS 2027 前100'), ('academic_impact','学术影响依据'),
    ('industry_or_public_role','公开专业 / 产业职务'), ('high_verified_support','已核实较高支持'),
    ('exceptional_new_pi','突出青年 PI'), ('direct_scientific_fit','具体神经 / 生理机制依据'),
    ('industry_or_public_professional_role','公开专业 / 产业职务依据'), ('priority20_research_fit','Priority-20 · 已证研究连接')]
POOLS = [('selected','精选'), ('candidate','条件待补证'), ('historical','历史 / 非精选'), ('all','全部保留数据')]
FIT_TIERS = [('A','Tier A · 直接神经 / 生理匹配'), ('B','Tier B · 方法延伸'), ('unknown','分层尚待核查')]
DOSSIER_FIELDS = [('awards','奖项 / 正式荣誉'),('projects','项目'),('first_destination','校友第一去向'),
    ('lineage','一层师承'),('support_conditions','访问支持条件'),('professional_roles','公开专业职务')]
HONOR_LABELS = {'Swartz Prize for Theoretical and Computational Neuroscience':'Swartz理论神经科学奖',
    'International Prize of Translational Neuroscience':'国际转化神经科学奖',
    'ICRA Most Influential Paper Award':'ICRA最具影响力论文奖','Best Conference Paper Award':'最佳会议论文奖',
    'IEEE Fellow':'IEEE会士','IEEE Fellow (class of 2022)':'IEEE会士 · 2022',
    'Young Investigator Award':'青年研究者奖','Young Scientists’ Award':'青年科学家奖',
    'Young Scientists’ Prize':'青年科学家奖','McKnight Scholar Award':'McKnight学者奖',
    'Sloan Research Fellow':'Sloan研究会士','CIFAR Senior Fellow':'CIFAR高级会士',
    'Bernstein Award for Computational Neuroscience':'Bernstein计算神经科学奖',
    'Member, National Academy of Sciences':'美国国家科学院院士','Australian Laureate Fellowship':'澳大利亚桂冠研究资助'}


def evidence_present(value):
    """A populated placeholder is not an evidenced fact."""
    return isinstance(value,dict) and value.get('status') not in ('unknown','blocked','pending','stale','conflict','source_changed','rejected') and bool(value.get('source_url') or value.get('evidence_url'))


def known_destination(row,kind):
    fact=row.get(kind,{})
    if fact.get('status')!='verified' or not fact.get('value') or not evidence_present(fact): return False
    if kind=='first_destination':
        relationship=str(row.get('relationship','')).lower()
        if relationship not in ('phd','phd student','postdoc','visiting','visiting student','visiting researcher','staff','researcher','staff/researcher'): return False
        if not evidence_present(row.get('relationship_evidence')): return False
    return True


def dossier_coverage(target):
    p=target.details.get('mentor_profile',{})
    alumni=p.get('alumni',{}); roster=alumni.get('items',[])
    first=sum(known_destination(r,'first_destination') for r in roster)
    lineage=p.get('lineage',{})
    roles=p.get('professional_roles',{})
    facts={
        'awards':any(evidence_present(r) for r in p.get('awards',[])+p.get('honors',[])),
        'projects':any(evidence_present(r) for r in p.get('projects',[])),
        'first_destination':first>0,
        'lineage':any(bool(lineage.get(k,{}).get('value')) and evidence_present(lineage.get(k)) for k in ('phd_advisor','postdoc_mentor')),
        'support_conditions':any(evidence_present(v) for v in p.get('support',{}).values()),
        'professional_roles':any(evidence_present(r) for r in roles.get('academic_and_public',[])+roles.get('industry',{}).get('items',[]))}
    return {**facts,'dimension_count':sum(facts.values()),'first_known':first,'structured_alumni':len(roster),
        'public_alumni_total':alumni.get('total_publicly_listed_count')}


def dossier_summary(targets):
    rows=[dossier_coverage(t) for t in targets]
    return {'total':len(rows),'fields':[{'key':k,'label':label,'known':sum(r[k] for r in rows)} for k,label in DOSSIER_FIELDS],
        'first_known':sum(r['first_known'] for r in rows),'structured_alumni':sum(r['structured_alumni'] for r in rows),
        'public_total_known_groups':sum(r['public_alumni_total'] is not None for r in rows),
        'first_known_in_public_groups':sum(r['first_known'] for r in rows if r['public_alumni_total'] is not None),
        'structured_in_public_groups':sum(r['structured_alumni'] for r in rows if r['public_alumni_total'] is not None),
        'public_total':sum(r['public_alumni_total'] or 0 for r in rows)}

def reason_codes(obj):
    if obj.curation.get('review_state')=='source_changed': return set()
    return {r['code'] for r in obj.curation.get('inclusion_reasons',[]) if r.get('evidence') and not (r['code']=='high_verified_support' and obj.curation.get('support_review_state')=='source_changed')}

def invalidate_selection(obj, old):
    """A changed source cannot silently retain old matching or support conclusions."""
    if not old or not obj.curation: return
    from .history import public_snapshot
    current=public_snapshot(obj)
    fields=('type','title','country','region','discipline','url','status','eligibility','language_requirements','employment_type','funding_status','summary_zh','work_fraction','details','themes','receiving_status')
    def child_facts(rows):
        return [{k:v for k,v in r.items() if k not in ('id','opportunity_id','scope_observed_at','verified_at')} for r in rows]
    changed=any(old.get(k)!=current.get(k) for k in fields) or any(child_facts(old.get(k,[]))!=child_facts(current.get(k,[])) for k in ('financials','deadlines'))
    if changed:
        obj.curation={**obj.curation,'previous_pool':obj.curation.get('pool'),'pool':'candidate','review_state':'source_changed',
            'disposition_reason':'来源事实已获新审核；此前研究匹配、支持与精选判断须独立重核，旧理由留存。'}
        fields=['curation']
        if hasattr(obj,'actionability'):
            previous_actionability=obj.actionability
            obj.actionability='research_watchlist'
            obj.classification_evidence={**obj.classification_evidence,'status':'source_changed','previous_actionability':previous_actionability}
            fields+=['actionability','classification_evidence']
        if hasattr(obj,'identity_fit'):
            obj.identity_fit='unknown';fields.append('identity_fit')
        obj.save(update_fields=fields)

def coverage_presence_counts():
    """Reviewed selected records by actual host, irrespective of discovery route.

    Presence is not an open-vacancy count. Pending conditions stay visible on
    the record; unreviewed candidates and retained historical data don't qualify.
    """
    from collections import Counter
    from .models import Opportunity
    rows=Opportunity.objects.filter(is_test=False, is_published=True,
        review_status='verified', institution__coverage__isnull=False,
        curation__pool='selected', curation__fit_tier__in=['A','B']).values('institution__coverage_id','curation')
    return dict(Counter(r['institution__coverage_id'] for r in rows if r['curation'].get('review_state')!='source_changed'))


def attach_selection(obj, presence_counts=None):
    obj.qs = obj.institution.coverage if obj.institution_id else None
    if obj.qs and presence_counts is not None:
        obj.qs._global_opportunity_count = presence_counts.get(obj.qs.pk, 0)
    obj.selection_badges = [label for code,label in REASONS if code in reason_codes(obj)][:3]
    obj.pool_label = dict(POOLS).get(obj.curation.get('pool'), '尚未评定精选范围')
    if hasattr(obj,'actionability') and obj.curation.get('pool')=='selected':obj.pool_label='科研覆盖范围'
    obj.selection_current = not obj.pending_change and bool(obj.curation)
    obj.fit_label = dict(FIT_TIERS).get(obj.curation.get('fit_tier'), '分层尚待核查')
    obj.evidence_coverage = dossier_coverage(obj) if hasattr(obj,'group_id') else None
    profile=obj.details.get('mentor_profile',{}) if hasattr(obj,'details') else {}
    obj.impact_badges=list(dict.fromkeys(filter(None,(r.get('name') or r.get('title') or r.get('award') for r in profile.get('awards',[])+profile.get('honors',[]) if evidence_present(r)))))[:2]
    chips=[{'label':HONOR_LABELS.get(name,name),'title':name,'kind':'honor'} for name in obj.impact_badges]
    for code,label in REASONS:
        if code not in reason_codes(obj) or code in ('qs_top100','direct_scientific_fit') or code=='academic_impact' and obj.impact_badges: continue
        chips.append({'label':label,'title':label,'kind':'support' if code=='high_verified_support' else 'evidence'})
    obj.evidence_chips=chips[:2 if obj.qs else 3]
    return obj

def selected_filter(items, data):
    from .priority20 import policy, registry, scope_info
    state=policy(); institutions=registry()['institutions']
    result=[];presence=coverage_presence_counts();now=timezone.now()
    for obj in items:
        attach_selection(obj,presence)
        obj.priority_scope=scope_info(obj,state,institutions)
        scope=obj.priority_scope; channel=scope['channel']
        if data.get('priority20') and not scope['priority20']: continue
        if data.get('priority_institution') and scope['institution_key'] not in data['priority_institution']: continue
        if data.get('priority_channel') and not ({scope['priority_channel']} if scope['priority_channel']!='BOTH' else {'CSC','YUANHANG'})&set(data['priority_channel']): continue
        if data.get('current_program') and channel.get('current_program_verified') is not True: continue
        if data.get('channel_open') and channel.get('current_application_open') is not True: continue
        if data.get('dossier_level') and getattr(obj,'details',{}).get('dossier',{}).get('level','basic') not in data['dossier_level']: continue
        if data.get('formal_visit') and not (hasattr(obj,'paths') and obj.paths.filter(is_published=True).exists() or getattr(obj,'type',None) in ('visiting','visiting_route')): continue
        c=obj.curation
        # Old, not-yet-reviewed instances remain readable during staged upgrades.
        if c and (data.get('pool') or 'selected') not in ('all',c.get('pool')): continue
        if data.get('qs_top100') and not (obj.qs and obj.qs.ranking_year==2027 and obj.qs.qs_rank<=100): continue
        if data.get('coverage_key') and (not obj.qs or obj.qs.key!=data['coverage_key']): continue
        if data.get('audit_scope_status') and (not obj.qs or obj.qs.audit_scope_status not in data['audit_scope_status']): continue
        if data.get('global_presence') and (not obj.qs or obj.qs.global_relevant_opportunity_presence not in data['global_presence']): continue
        if data.get('inclusion') and not reason_codes(obj)&set(data['inclusion']): continue
        if data.get('high_support') and ('high_verified_support' not in reason_codes(obj) or obj.pending_change): continue
        if data.get('theme') and not set(c.get('themes',getattr(obj,'themes',[])))&set(data['theme']): continue
        if data.get('fit_tier') and (obj.pending_change or c.get('fit_tier','unknown') not in data['fit_tier']): continue
        if data.get('dossier') and (not obj.evidence_coverage or not any(obj.evidence_coverage.get(k) for k in data['dossier'])): continue
        language=c.get('language_status','unknown')
        if data.get('language') and language not in data['language'] and not ('known' in data['language'] and language=='english'): continue
        if data.get('freshness'):
            verified=obj.verified_at
            if obj.pending_change or not verified or verified>now or not 0<=(timezone.localdate(now)-timezone.localdate(verified)).days<=int(data['freshness']): continue
        result.append(obj)
    return result

def selection_sort(items, order, themes=()):
    if not order: items.sort(key=lambda o:({'A':0,'B':1}.get(o.curation.get('fit_tier'),2),o.pending_change))
    elif order=='completeness': items.sort(key=lambda o:(-((o.evidence_coverage or {}).get('dimension_count',0)),o.pending_change,o.key))
    elif order=='qs': items.sort(key=lambda o:(o.qs.qs_rank if o.qs else 10000,o.key))
    elif order=='impact': items.sort(key=lambda o:(o.pending_change,-sum(r['code']=='academic_impact' and bool(r.get('evidence')) for r in o.curation.get('inclusion_reasons',[])),o.key))
    elif order=='newest': items.sort(key=lambda o:(-o.pk,o.key))
    elif order=='priority_institution':
        from .priority20 import registry
        rank={r['key']:i for i,r in enumerate(registry()['institutions'])}
        items.sort(key=lambda o:(rank.get(getattr(o,'priority_scope',{}).get('institution_key'),100),o.key))
    elif order=='actionable': items.sort(key=lambda o:(o.pending_change,getattr(o,'effective_status',getattr(o,'receiving_status','unknown'))!='open',o.key))
    elif order=='match': items.sort(key=lambda o:(o.pending_change,{'A':0,'B':1}.get(o.curation.get('fit_tier'),2),o.curation.get('research_gate')!='verified',
        -len(set(o.curation.get('themes',getattr(o,'themes',[])))&set(themes)),o.key))

def support_key(comparison, key):
    """Native-currency groups: no implied exchange rate, tax estimate or unknown zero."""
    if not comparison: return (True,'','','',0,key)
    f=comparison['fact']
    return (False,str(comparison['year']),comparison['tax_group'],f.currency,-comparison['monthly_original'],key)

def validate_selection(value):
    from django.core.exceptions import ValidationError
    if value.get('pool') not in set(dict(POOLS))-{'all'}: raise ValidationError('精选范围无效')
    if value['pool']=='selected':
        if value.get('fit_schema')==1:
            if value.get('fit_tier') not in ('A','B') or not value.get('fit_reason'):
                raise ValidationError('精选对象需要证据分层和理由')
            if value['fit_tier']=='A' and not value.get('physiology_basis'):
                raise ValidationError('Tier A需要具体生理或神经对象，不能仅凭AI关键词')
            if value['fit_tier']=='B' and not value.get('method_transfer_reason'):
                raise ValidationError('Tier B需要具体方法迁移理由')
        if value.get('research_gate')!='verified' or not value.get('research_connection') or not value.get('research_evidence'):
            raise ValidationError('精选必须有具体研究连接和原文依据')
        reasons=value.get('inclusion_reasons',[])
        if not reasons or any(r.get('code') not in dict(REASONS) or not r.get('evidence') for r in reasons):
            raise ValidationError('精选必须有已证实的纳入理由')
    return value

def mentor_context(target):
    import json
    from django.conf import settings
    from django.utils.dateparse import parse_date
    p=target.details.get('mentor_profile',{})
    labels={'salary':'工资','stipend':'津贴','housing':'住房','travel':'差旅','insurance':'保险','tuition_waiver':'学费 / 费用减免',
        'research_allowance':'研究补助','relocation':'搬迁','other_cash':'其他现金','minimum_proof_of_funds':'最低资金证明','minimum_funds':'最低资金证明','proof_of_funds':'资金证明要求','self_funded_requirement':'自筹资金要求','self_funded':'自筹资金要求'}
    support=p.get('support',{})
    known=[];unknown=[]
    for key,value in support.items():
        if not isinstance(value,dict): continue
        year=value.get('year')
        year_display=('适用年度未知；页面观察 '+year.removeprefix('page_current_')
            if isinstance(year,str) and year.startswith('page_current_') else year or '适用年未明确')
        row={**value,'label':labels.get(key,key),'year_display':year_display}
        (known if evidence_present(value) else unknown).append(row)
    alumni=p.get('alumni',{})
    roster=alumni.get('items',[])
    counts={'first_known':sum(known_destination(r,'first_destination') for r in roster),
        'current_known':sum(known_destination(r,'current_destination') for r in roster),
        'structured':len(roster),'public_total':alumni.get('total_publicly_listed_count')}
    relationship_labels={'phd':'博士','phd student':'博士生','postdoc':'博士后','visiting':'访问者','visiting student':'访问学生','visiting researcher':'访问研究人员','staff':'研究人员','researcher':'研究人员','staff/researcher':'研究人员','unknown':'身份未明','former_lab_member_role_unknown':'已离组成员 · 具体身份未明'}
    alumni_rows=[{**r,'relationship_display':relationship_labels.get(str(r.get('relationship','')).lower(),r.get('relationship') or '身份未明'),'first_verified':known_destination(r,'first_destination'),'current_verified':known_destination(r,'current_destination'),
        'first_from_chronology':'chronology' in r.get('first_destination',{}).get('evidence_basis','')} for r in roster]
    sources=[];seen=set()
    def walk(value):
        if isinstance(value,dict):
            url=value.get('source_url')
            if url and (url,value.get('last_verified')) not in seen:
                seen.add((url,value.get('last_verified')));sources.append({'url':url,'verified':value.get('last_verified'),'scope':value.get('read_scope','已列字段')})
            for child in value.values():walk(child)
        elif isinstance(value,list):
            for child in value:walk(child)
    walk(p)
    lineage_display={}
    for kind in ('phd_advisor','postdoc_mentor'):
        fact=p.get('lineage',{}).get(kind,{})
        value=fact.get('value') if evidence_present(fact) else None
        lineage_display[kind]=' / '.join(value) if isinstance(value,list) else value
    window=5
    rules=settings.DATA_DIR/'curation_rules.json'
    if rules.exists():
        window=json.loads(rules.read_text(encoding='utf8')).get('first_independent_pi_window_years',5)
        if type(window) is not int or not 1<=window<=15: raise ValueError('独立PI窗口须为1—15年')
    first=p.get('new_pi',{}).get('first_independent_appointment',{})
    stamp=parse_date(str(first.get('value'))) if first.get('status')=='verified' else None
    young=(0<=(timezone.localdate()-stamp).days<=window*365.2425) if stamp else None
    return {'mentor':p,'known_support':known,'unknown_support':unknown,'alumni_counts':counts,'alumni_rows':alumni_rows,'mentor_sources':sources,
        'lineage_display':lineage_display,'pi_window':window,'pi_within_window':young,'support_stale':target.curation.get('support_review_state')=='source_changed'}
