"""Evidence-based contact layers; institutional availability is not an invitation."""
import json
from functools import lru_cache
from collections import Counter
from django.conf import settings
from django.core.exceptions import ValidationError
from .models import ResearchTarget


def validate_contact(target):
    if target.actionability!='contact_candidate':return
    proof=target.classification_evidence
    exception=proof.get('host_exception',{})
    if target.role_class in ('research_staff','postdoc','emeritus_retired','former','unknown') and not (
            exception.get('verified') is True and exception.get('url') and exception.get('quote')):
        raise ValidationError('参考身份不能默认进入可联系层；须有明确当前正式指导/Host例外依据。')
    if target.host_capability in ('restricted','not_applicable','unknown') or target.contact_receiving_status in ('not_receiving','not_applicable'):
        raise ValidationError('Host受限、未知或明确不接收者不能进入可联系层。')
    if not (proof.get('current_active') is True and proof.get('institution_verified') is True
            and proof.get('official_profile_url') and proof.get('route_evidence') and proof.get('next_action')
            and proof.get('personal_research_bridge') and target.curation.get('fit_tier') in ('A','B')):
        raise ValidationError('可联系层须具备现职、实际机构、官方档案、可用路径、科研桥接和下一行动的证据。')


def layer_counts(targets):
    counts=Counter(t.effective_actionability for t in targets)
    return {'coverage':len(targets),'contacts':counts['contact_candidate'],'watchlist':counts['research_watchlist'],
        'reference':counts['reference_only'],'deep':sum(t.details.get('dossier',{}).get('level')=='deep' for t in targets)}


def host_compatibility(key):
    path=settings.BASE_DIR/'data/demo/csc_host_compatibility.json'
    if not path.exists():return {}
    data=_host_data(str(path),path.stat().st_mtime_ns)
    records=data.get('records',{})
    return records.get(key,{}) if isinstance(records,dict) else next((r for r in records if r.get('key')==key),{})


@lru_cache(maxsize=4)
def _host_data(path,mtime):
    from pathlib import Path
    return json.loads(Path(path).read_text('utf8'))


def host_acceptance(row):
    value=row.get('host_csc_acceptance','unknown')
    return value.get('value','unknown') if isinstance(value,dict) else value


def support_flags(obj):
    """Reviewed support attributes; fees/proof/grants never qualify as visitor cash."""
    if hasattr(obj,'classification_evidence'):
        from .priority20 import institution_key
        flags=set(obj.classification_evidence.get('support_flags',[]))
        row=host_compatibility(institution_key(obj.institution))
        for f in row.get('funding_components',[]):
            # Conditional published scheme attributes, never individual cash amounts.
            if f.get('source_id') and f.get('condition'):
                flags.update({'tuition_waiver':{'fee_waiver'},'stipend_and_travel':{'csc_stipend','travel'},'conditional_required_top_up':{'host_topup'}}.get(f.get('kind'),set()))
                if f.get('kind')=='stipend' and f.get('provider')=='CSC':flags.add('csc_stipend')
                if f.get('kind')=='travel':flags.add('travel')
        return flags
    return set(obj.curation.get('identity_review',{}).get('support_flags',[]))


def action_filters(objects,data):
    from .priority20 import institution_key
    out=[]
    for obj in objects:
        if hasattr(obj,'actionability'):
            conflict=obj.pending_change or obj.classification_evidence.get('status')=='source_changed'
            if conflict and data.get('host_capability') and obj.host_capability in {'confirmed_by_official_policy_or_profile','plausible_but_unverified'}:continue
            if conflict and data.get('contact_receiving') and obj.contact_receiving_status in {'explicitly_open','inquiries_welcome','current_program_open'}:continue
            if any(data.get(field) and getattr(obj,field) not in data[field] for field in ('role_class','host_capability')):continue
            if data.get('contact_receiving') and obj.contact_receiving_status not in data['contact_receiving']:continue
        if data.get('host_csc') and host_acceptance(host_compatibility(institution_key(obj.institution))) not in data['host_csc']:continue
        if data.get('verified_support') and (obj.pending_change or not support_flags(obj)&set(data['verified_support'])):continue
        out.append(obj)
    return out


def attach_actionability(target):
    from .priority20 import institution_key
    target.actionability_label=dict(ResearchTarget.ACTIONABILITIES)[target.effective_actionability]
    target.actionability_next=target.classification_evidence.get('next_action') or '先核当前角色、正式访问身份及Host资格；不按目录记录直接联系。'
    target.csc_host=host_compatibility(institution_key(target.institution))
    target.csc_host_acceptance=host_acceptance(target.csc_host)
    target.csc_host_label={'accepted':'有明确接受依据','restricted':'该访问身份受限','unknown':'Host接受尚未确认'}[target.csc_host_acceptance]
    target.current_role=target.classification_evidence.get('current_role') or target.get_role_class_display()
    target.support_conditions=[{**f,'kind_label':{'tuition_waiver':'有条件学费豁免','stipend_and_travel':'有条件生活与差旅资助','stipend':'项目公布津贴（未确认个人获款）','travel':'有条件差旅','visa_fee':'有条件签证费用','conditional_required_top_up':'有条件Host补足','income_requirement':'最低资金要求（非收入）'}.get(f.get('kind'),'有条件支持制度')} for f in target.csc_host.get('funding_components',[])]
    return target


def contact_sort(targets):
    # Each dimension stays visible; no score or admission probability.
    targets.sort(key=lambda t:({'A':0,'B':1}.get(t.curation.get('fit_tier'),2),
        {'contact_candidate':0,'research_watchlist':1,'reference_only':2}[t.effective_actionability],
        {'confirmed_by_official_policy_or_profile':0,'plausible_but_unverified':1}.get(t.host_capability,2),
        {'explicitly_open':0,'current_program_open':0,'inquiries_welcome':1}.get(t.contact_receiving_status,2),
        not bool(t.classification_evidence.get('route_evidence')),-len(t.impact_badges),
        not t.evidence_coverage.get('support_conditions'),-t.verified_at.timestamp(),t.key))
