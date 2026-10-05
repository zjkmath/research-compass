import json
from collections import Counter

from django.conf import settings
from django.http import Http404
from django.shortcuts import render

from .models import ResearchTarget, Opportunity, VisitPath
from .priority20 import registry, institution_key
from .curation import dossier_coverage, evidence_present
from .actionability import layer_counts,attach_actionability,host_compatibility,host_acceptance,_host_data


def unit_status_label(value):
    status=str(value or '').upper()
    if 'INDEX' in status or 'SEARCH' in status:return '公开索引证据；正文读取边界见台账'
    if any(x in status for x in ('SHELL','EMPTY')):return '页面可达，研究正文未取得'
    if any(x in status for x in ('BLOCK','403','ERROR','FAIL')):
        return '部分读取，其余受限' if 'READ' in status else '访问受限'
    if any(x in status for x in ('PENDING','UNPROCESSED')):return '尚待核查'
    if any(x in status for x in ('BOUNDED_ENTRIES','DISPOSITION','ENUMERATED')):return '目录条目已处置；逐条正文状态见台账'
    if any(x in status for x in ('AGE_FLAG','HISTORICAL')):return '历史目录证据；当前身份另核'
    return '已读取所列范围'


def institution_cards():
    rows=registry()['institutions']
    targets=list(ResearchTarget.objects.filter(is_published=True).select_related('institution','group').prefetch_related('mentors','paths'))
    opportunities=list(Opportunity.objects.filter(is_published=True,is_test=False,review_status='verified').select_related('institution').prefetch_related('deadlines'))
    coverage_path=settings.BASE_DIR/'data/demo/priority20_coverage.json'
    coverage=json.loads(coverage_path.read_text(encoding='utf8')) if coverage_path.exists() else {'institutions':[]}
    paths=list(VisitPath.objects.filter(is_published=True).select_related('institution'))
    channel_path=settings.BASE_DIR/'data/demo/yuanhang_current_status.json'
    yuanhang=json.loads(channel_path.read_text('utf8')).get('records',{}) if channel_path.exists() else {}
    for r in rows:
        ts=[t for t in targets if institution_key(t.institution,rows)==r['key'] and t.curation.get('pool')=='selected']
        ops=[o for o in opportunities if institution_key(o.institution,rows)==r['key'] and o.curation.get('pool')=='selected']
        r['targets']=ts; r['opportunities']=ops
        r['paths']=[p for p in paths if institution_key(p.institution,rows)==r['key']]
        r['counts']={'labs':len({t.group_id for t in ts}), 'mentors':len({p.name.casefold() for t in ts for p in t.mentors.all()}),
            'A':sum(t.curation.get('fit_tier')=='A' for t in ts),'B':sum(t.curation.get('fit_tier')=='B' for t in ts),
            'deep':sum(t.details.get('dossier',{}).get('level')=='deep' for t in ts), 'opportunities':len(ops),
            'open':sum(o.effective_status=='open' and not o.pending_change for o in ops),
            'paths':len(r['paths']), 'support_conditions':sum(dossier_coverage(t)['support_conditions'] for t in ts),
            'visitor_cash':sum(any(evidence_present(v) and v.get('amount') is not None and v.get('eligible_identity') in ('visiting_student','visiting_researcher','visiting_scholar')
                for k,v in t.details.get('mentor_profile',{}).get('support',{}).items() if k in ('salary','stipend','other_cash')) for t in ts),
            'unknown_receiving':sum(t.receiving_status=='unknown' for t in ts)}
        r['counts'].update(layer_counts(ts))
        r['counts']['identity_open']=sum(o.effective_status=='open' and not o.pending_change and o.identity_fit in ('directly_applicable','potentially_applicable') for o in ops)
        r['counts']['identity_direct_open']=sum(o.effective_status=='open' and not o.pending_change and o.identity_fit=='directly_applicable' for o in ops)
        r['counts']['identity_potential_open']=sum(o.effective_status=='open' and not o.pending_change and o.identity_fit=='potentially_applicable' for o in ops)
        r['csc_host']=host_compatibility(r['key']);r['csc_label']={'accepted':'制度明确接受','restricted':'该访问身份受限','unknown':'Host接受未知'}[host_acceptance(r['csc_host'])]
        r['contact_targets']=[attach_actionability(t) for t in ts if t.effective_actionability=='contact_candidate']
        r['host_checks']=[]
        ledger=settings.BASE_DIR/'data/demo/csc_host_compatibility.json'
        sources=_host_data(str(ledger),ledger.stat().st_mtime_ns).get('source_ledger',{}) if ledger.exists() else {}
        for label,value in r['csc_host'].get('checks',{}).items():
            r['host_checks'].append({'label':{'official_csc_specific_page':'CSC专属制度','visiting_phd_policy':'在读博士访问制度','current_chinese_phd_applicability':'当前中国博士适用','tuition_bench_fee':'学费 / bench fee','host_csc_funding_acceptance':'Host接受CSC资金','current_cycle':'当前轮次','invitation_nomination':'邀请 / 提名','page_year_status':'页面年份与状态'}.get(label,label),**value,
                'sources':[sources[s] for s in value.get('source_ids',[]) if s in sources]})
        r['coverage']=next((x for x in coverage['institutions'] if x.get('key')==r['key']),{})
        for unit in r['coverage'].get('units_checked',[]):
            unit['status_label']=unit_status_label(unit.get('status'))
        c=r['channel']
        if c['priority_channel']=='YUANHANG' and r['key'] in yuanhang:
            review=yuanhang[r['key']];r['yuanhang_review']=review
            c.update(current_program_verified=review.get('current_program_verified'),
                current_application_open=review.get('current_application_open'),
                current_cycle=review.get('current_cycle'),current_program_url=review.get('current_cycle_notice_url'),
                host_specific_eligibility=review.get('degree_level'),host_specific_funding=review.get('funding_rule'),
                funding_summary=review.get('funding_rule'),funding_url=review.get('funding_url'))
        r['channel_label']='CSC 框架精选' if c['priority_channel']=='CSC' else '远航合作机构'
        r['program_label']='具名项目通知已核（不等于当期可申请）' if c.get('current_program_verified') is True else '具体项目尚未确认'
        r['open_label']='通道本轮开放' if c.get('current_application_open') is True else '通道已核轮次截止' if c.get('current_application_open') is False else '通道当前窗口未知'
        if c['priority_channel']=='CSC' and r['csc_host']:
            r['program_label']='已核在读博士CSC访学制度' if r['csc_host']['strict_csc_actionable'] else '未确认适用在读博士的CSC访学制度'
            r['open_label']={'closed':'已核轮次截止','open':'当前轮次开放','not_yet_open':'窗口尚未开始'}.get(r['csc_host']['current_csc_application_state'],'当前窗口未知或该身份受限')
    return rows


def index(request):
    cards=institution_cards()
    channel=request.GET.get('channel')
    if channel in ('CSC','YUANHANG'):cards=[r for r in cards if r['channel']['priority_channel']==channel]
    path=settings.BASE_DIR/'data/demo/csc_actionable_set.json'
    strict=json.loads(path.read_text('utf8')) if path.exists() else {}
    return render(request,'opportunities/priority20.html',{'cards':cards,'channel':channel,'strict_csc':strict})


def detail(request,key):
    row=next((r for r in institution_cards() if r['key']==key),None)
    if row is None:raise Http404
    return render(request,'opportunities/priority20_detail.html',{'institution':row})
