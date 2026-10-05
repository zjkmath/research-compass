"""Shared visitor decisions. Evidence, private assumptions and unknowns stay distinct."""
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from types import SimpleNamespace
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time
from .ingest import digest
from .models import Deadline, FinancialFact, ExchangeSnapshot, LocalProfile, UpdateProposal, PathSupport
from .money import comparable

RULE_VERSION = 'G4-decisions-1'
THEMES = [('neural_dynamics','神经动力学'),('control','控制与反馈'),('physiology','生理机制'),
    ('world_models','世界模型'),('embodied','具身与运动'),('bci','脑机接口'),('population_decoding','群体解码')]

def profile_data(user):
    if not user or not user.is_authenticated: return {}
    obj = LocalProfile.objects.filter(user=user).first()
    return obj.data if obj else {}

def scoped_profile(user, target, path):
    data = dict(profile_data(user))
    # Keep legacy global values in storage; they are never a host-specific confirmation.
    data.update(host_support=None, agreement_ready=None, _scoped_conditions=True)
    support = PathSupport.objects.filter(user=user, target=target, path=path).first() if user and user.is_authenticated else None
    if support and support.evidence_note and (not support.valid_until or support.valid_until >= timezone.localdate()):
        data.update(host_support=support.host_support, agreement_ready=support.agreement_ready, _support_version=digest([support.evidence_note,str(support.valid_until),support.updated_at.isoformat()]))
    return data


def visible_paths(target):
    return target.paths.filter(is_published=True)


def evidence_current(value):
    return not any(value.get(k) for k in ('path_pending','target_pending','path_stale','target_stale')) and value.get('fx_current', True)


def path_version(path):
    return digest([path.key, path.identity_types, path.details, path.evidence, path.verified_at.isoformat()])

def review_due(obj,category):
    from .maintenance import fact_rows
    return any(row['stale'] for row in fact_rows(obj))

def versions(target, path, user):
    profile=LocalProfile.objects.filter(user=user).first() if user and user.is_authenticated else None
    if path is None:
        return {'rules':RULE_VERSION,'path':None,'path_unknown':True,
            'profile':digest([profile.data,profile.updated_at.isoformat()]) if profile else digest({}),
            'target':digest([target.key,target.details,target.evidence,target.receiving_status,target.curation]),
            'target_pending':target.pending_change,'target_stale':review_due(target,'research')}
    return {'rules': RULE_VERSION, 'profile': digest([profile.data,profile.updated_at.isoformat()]) if profile else digest({}), 'path': path_version(path),
        'target': digest([target.key, target.details, target.evidence, target.receiving_status, target.curation]),
        'support': digest(scoped_profile(user,target,path)),
        'path_pending': path_pending(path), 'target_pending': target.pending_change,
        'path_stale':review_due(path,'visit_policy'),'target_stale':review_due(target,'research')}

def budget_versions(target,path,user,inputs):
    if not inputs.get('display_currency'): return versions(target,path,user)
    snapshot=ExchangeSnapshot.objects.filter(version=inputs.get('fx_version')).first() if inputs.get('fx_version') else ExchangeSnapshot.objects.order_by('-date').first()
    return {**versions(target,path,user),'fx':snapshot.version if inputs.get('display_currency') and snapshot else None,
        'historical_fx':bool(inputs.get('historical_fx')),'fx_current':snapshot is not None and (timezone.localdate()-snapshot.date).days in range(8)}

def path_pending(path):
    return UpdateProposal.objects.filter(kind='path', status__in=['pending','conflict'], payload__key=path.key).exists()

def decision_current(record, user):
    if record.kind == 'comparison':
        from .models import ResearchTarget, VisitPath
        current = []
        for item in record.inputs.get('choices', []):
            target = ResearchTarget.objects.filter(pk=item['target']).first()
            if item.get('path') is None:
                if not target or not target.is_published:return False
                current.append(versions(target,None,user));continue
            path = VisitPath.objects.filter(pk=item['path'], researchtarget=target).first()
            if not target or not path or not target.is_published or not path.is_published: return False
            current.append(versions(target, path, user))
        return record.versions == {'rows': current} and all(evidence_current(v) for v in current)
    current=budget_versions(record.target,record.path,user,record.inputs) if record.kind=='budget' else versions(record.target, record.path, user)
    return bool(record.target_id and record.path_id and record.target.is_published and record.path.is_published and record.versions == current and evidence_current(current))

def completed(deadline, tokens):
    # Legacy label keys are only accepted on unversioned synthetic/old steps.
    return deadline.completion_key in tokens or not deadline.requirement_version and deadline.kind + ':' + deadline.label in tokens

def next_action(opportunity, record=None):
    tokens = record.completed_steps if record else []
    warnings = [d for d in opportunity.prerequisite_warning if not completed(d, tokens)]
    dates = [d for d in opportunity.deadlines.all() if d.status == 'verified' and d.date and
        d.effect in ('prerequisite', 'priority', 'hard_close') and not d.expired and not completed(d, tokens)]
    item = min(dates, key=lambda d: d.boundary) if dates else None
    return {'label': item.label if item else '下一行动日期待核验', 'date': item.date if item else None,
        'kind': item.get_effect_display() if item else opportunity.get_deadline_mode_display(),
        'risk': '；'.join('未完成前置步骤已过期：' + d.label for d in warnings), 'warnings': warnings,
        'pending': opportunity.pending_change, 'deadline': item}

def path_steps(path):
    deadlines = []
    for raw in path.details.get('actions', []):
        valid=True
        try:
            day=parse_date(raw.get('date') or ''); clock=parse_time(raw.get('time') or '')
            zone=raw.get('timezone') or ''
            if zone: ZoneInfo(zone)
            if raw.get('date') and not day or raw.get('time') and (not clock or not day): valid=False
        except (ValueError,TypeError,ZoneInfoNotFoundError): day=None;clock=None;zone='';valid=False
        d = Deadline(kind=raw.get('kind', 'other'), label=raw['label'], date=day,
            time=clock, timezone=zone, effect={'priority_consideration':'priority'}.get(raw.get('effect'),raw.get('effect','unknown')),
            status=raw.get('status', 'unknown'), step_id=path.key + ':' + raw.get('step_id', 'other'),
            requirement_version=raw.get('requirement_version') or digest([raw.get('kind'), raw.get('date'), raw.get('time'), raw.get('timezone'), raw.get('effect')])[:16])
        d.dependencies=raw.get('depends_on',[])
        if not isinstance(d.dependencies,list) or not all(isinstance(v,str) for v in d.dependencies): d.dependencies=[];valid=False
        d.actor=raw.get('actor', 'institution' if d.kind in ('result','notification','publication','opening') else 'applicant')
        if not valid or d.actor not in ('applicant','institution') or d.effect not in dict(Deadline._meta.get_field('effect').choices): d.status='unknown'
        deadlines.append(d)
    return deadlines

def path_action(path, record=None):
    deadlines = path_steps(path)
    tokens = record.completed_steps if record else []
    warnings = [d for d in deadlines if d.effect == 'prerequisite' and d.expired and not completed(d, tokens)]
    available = [d for d in deadlines if d.status == 'verified' and not d.expired and not completed(d, tokens) and d.actor == 'applicant' and d.effect in ('prerequisite', 'priority', 'hard_close','informational') and all(any(x.step_id.endswith(':'+dep) and completed(x,tokens) for x in deadlines) for dep in d.dependencies)]
    preparation=[d for d in available if d.effect=='prerequisite' or d.kind=='procedure']
    pool=preparation or available
    dated=[d for d in pool if d.date]
    item=min(dated,key=lambda d:d.boundary) if dated else pool[0] if pool else None
    return {'label': item.label if item else ('行动条件或日期待核验' if any(d.status!='verified' and d.actor=='applicant' for d in deadlines) else '当前没有待办，等待机构通知或核查接收' if deadlines else path.details.get('next_action', '先核对正式身份、接收与访问制度')),
        'date': item.date if item else None, 'kind': item.get_effect_display() if item else '行动日期未知',
        'risk': '；'.join('未完成前置步骤已过期：' + d.label for d in warnings), 'warnings': warnings,
        'pending': path_pending(path), 'deadline': item}

def eligibility(path, data):
    passed, unknown, failed = [], [], []
    identity = data.get('formal_identity')
    if not identity: unknown.append('本人访问身份尚未填写')
    elif identity not in path.identity_types: failed.append('该路径不适用于已填写的访问身份')
    else: passed.append('访问身份类别匹配')
    requirements = path.details.get('requirements', [])
    for condition in requirements:
        key = condition['key']; value = data.get(key)
        if key in ('host_support','agreement_ready') and not data.get('_scoped_conditions'): value=None
        label = condition.get('label', key)
        if value is None or value == '': unknown.append(label + '：本人条件未知'); continue
        expected = condition.get('value')
        ok = value == expected
        if condition.get('operator') in ('max','min'):
            try: ok = decimal(value) <= decimal(expected) if condition['operator']=='max' else decimal(value) >= decimal(expected)
            except ValidationError: ok = False
        elif condition.get('operator') not in (None,'eq'):
            unknown.append(label + '：条件类型尚未支持'); continue
        if not condition.get('evidence_url') or not condition.get('quote'):
            unknown.append(label + '：规则依据待核验')
        elif ok: passed.append(label)
        else: failed.append(label + '：已填条件不满足')
    if not requirements: unknown.append('制度条件尚未完整结构化，须读原文确认')
    bounds = (path.details.get('duration_min_months'), path.details.get('duration_max_months',path.details.get('max_months')))
    if any(v is not None for v in bounds):
        try:
            months=decimal(data.get('months'))
            if months <= 0: raise ValidationError('时长须大于零')
            if all(v is not None for v in bounds) and decimal(bounds[0])>decimal(bounds[1]): raise ValidationError('原文时长边界不一致')
            outside=(bounds[0] is not None and months<decimal(bounds[0])) or (bounds[1] is not None and months>decimal(bounds[1]))
            if outside:
                (failed if path.details.get('duration_constraint')=='hard' else unknown).append('超出通常时长范围，须确认例外、续期批准或另一正式路径')
        except ValidationError: unknown.append('访问时长缺失或无效，须填写有限正月数')
    window=path.details.get('arrival_window') or {}
    if window:
        try:
            raw=data.get('start_date'); start=raw if isinstance(raw,date) else parse_date(raw or '')
            if not start: raise ValueError()
            first=parse_date(window['start_month']+'-01'); last=parse_date(window['end_month']+'-01')
            if not first or not last or first>last: raise ValueError()
            if not first <= start.replace(day=1) <= last: unknown.append('不在本年度资助起始窗口；一般访问与其他年度资金须另核，不是永久禁止访问')
        except (KeyError,ValueError,TypeError): unknown.append('资助起始窗口或本人日期缺失/无效，适用性待核验')
    if review_due(path,'visit_policy'): unknown.append('路径重要事实超过复核期限，已填条件不能认定当前匹配')
    if path_pending(path): unknown.append('访问制度新版本待审，旧资格判断失效')
    status = 'incompatible' if failed else 'compatible' if passed and not unknown else 'partial' if passed else 'insufficient'
    return {'status': status, 'label': {'compatible':'已录入条件匹配','incompatible':'明确条件不匹配','partial':'部分条件待核对','insufficient':'个人资料不足'}[status],
        'reasons': failed + unknown, 'passed': passed, 'official_confirmation': '主办方审核与具体接收仍须确认'}

def path_cash(path, basis='gross_salary'):
    fx = ExchangeSnapshot.objects.order_by('-date').first()
    values, reasons = [], []
    for raw in path.details.get('cash_facts', []):
        accepted = {f.name for f in FinancialFact._meta.fields} - {'id', 'opportunity'}
        fact = FinancialFact(**{k:v for k,v in raw.items() if k in accepted})
        fact.opportunity = __import__('opportunities.models', fromlist=['Opportunity']).Opportunity(work_fraction=None)
        for key in ('amount', 'amount_max'):
            if getattr(fact, key) is not None: setattr(fact, key, decimal(getattr(fact, key)))
        result, reason = comparable(fact, fx, basis)
        if result: values.append(result)
        else: reasons.append(reason)
    # Alternative awards are never summed; minimum compatible base is conservative.
    return min(values, key=lambda v:v['monthly_eur']) if values else None, '；'.join(dict.fromkeys(reasons)) or '没有已核验的周期现金'

def path_views(target, user, record=None, basis='gross_salary'):
    data = profile_data(user); rows = []
    target.theme_labels = [dict(THEMES).get(code,code) for code in target.themes]
    for path in visible_paths(target):
        path.display_details = {key:(value.replace('unknown','待核验').replace('not_specified','原文未说明') if isinstance(value,str) else value) for key,value in path.details.items()}
        cash, reason = path_cash(path, basis)
        stale = review_due(path,'visit_policy')
        pending = path_pending(path) or target.pending_change
        capacity = 'unknown' if pending or stale else path.details.get('receiving_status', 'unknown')
        eligibility_view = eligibility(path, scoped_profile(user,target,path))
        if pending: eligibility_view={**eligibility_view,'status':'unknown','label':'条件有变化待审核'}
        rows.append({'path': path, 'action': path_action(path, record), 'eligibility': eligibility_view,
            'version': path_version(path), 'stale': stale, 'pending': pending, 'capacity': capacity,
            'capacity_label': dict(target.RECEIVING).get(capacity, '接收未确认'),
            'comparison': cash if not stale and not pending else None,
            'cash_summary': '旧资金事实须复核' if pending or stale else reason if not cash else f"{cash['fact'].currency} {cash['monthly_original']:.2f}/月均 · {cash['year']}",
            'funding_type': 'unknown' if pending or stale else path.details.get('funding_type', 'unknown')})
    return rows

def matches_path(row, filters):
    path = row['path']; unknown = filters.get('include_unknown', False)
    if (row.get('pending') or row.get('stale')) and any(filters.get(k) for k in ('receiving_verified','external_proof','eligibility_verified','cash_only','known_date')): return False
    if (row.get('pending') or row.get('stale')) and not unknown and (filters.get('deadline_from') or filters.get('deadline_to')): return False
    if filters.get('type') and not set(path.identity_types) & set(filters['type']): return False
    if filters.get('funding') and row['funding_type'] not in filters['funding']:
        if not (unknown and row['funding_type'] == 'unknown'): return False
    if filters.get('receiving') and row['capacity'] not in filters['receiving']:
        if not (unknown and row['capacity'] == 'unknown'): return False
    if filters.get('receiving_verified') and row['capacity'] != 'open': return False
    if filters.get('external_proof') and row['capacity'] != 'external_funding': return False
    if filters.get('eligibility_verified') and row['eligibility']['status'] != 'compatible': return False
    if filters.get('cash_only') and not row['comparison']: return False
    day = row['action']['date']
    if filters.get('known_date') and not day: return False
    if filters.get('deadline_from') and (not day and not unknown or day and day < filters['deadline_from']): return False
    if filters.get('deadline_to') and (not day and not unknown or day and day > filters['deadline_to']): return False
    return True

def research_order(target, themes):
    # Explainable tags, not a probability or evaluation of the PI.
    overlap = len(set(themes) & set(target.themes)) if themes else 0
    return (-overlap, {'direct':0, 'extension':1}.get(target.match_category, 2), target.key)

def decimal(value, *, nullable=False):
    if value in (None, ''):
        if nullable: return None
        raise ValidationError('数值缺失，不能当作0')
    try: number = Decimal(str(value))
    except (InvalidOperation, ValueError): raise ValidationError('无效金额')
    if not number.is_finite() or number < 0: raise ValidationError('金额必须是有限非负数')
    return number

def calculate_budget(path, inputs):
    months = decimal(inputs.get('months'))
    if not Decimal('0') < months <= Decimal('36'): raise ValidationError('时长须大于0且不超过36个月')
    currency = inputs.get('currency', '')
    if not isinstance(currency,str) or len(currency) != 3 or not currency.isalpha() or currency != currency.upper(): raise ValidationError('请选择三字母原币')
    values = {key: decimal(inputs.get(key), nullable=True) for key in ('income_confirmed', 'living', 'housing', 'insurance', 'transport',
        'deposit', 'prepaid', 'support_once', 'available_cash')}
    gaps = [label for key,label in [('income_confirmed','确认可得月收入'),('living','月生活费'),('housing','月住房费'),('insurance','月保险费'),('transport','月交通费'),('deposit','可退押金'),('prepaid','报销前垫付'),('support_once','确认一次现金支持')] if values[key] is None]
    cost_once, initial_fees, official_monthly, official_total, initial_recurring, refundable, prepaid, rows = Decimal('0'), Decimal('0'), Decimal('0'), Decimal('0'), Decimal('0'), Decimal('0'), Decimal('0'), []
    liquidity_gaps=[]
    exclusive = set()
    for cost in path.details.get('costs', []):
        cost={**cost,'label':cost.get('label') or {'registration':'注册费','research_fee':'研究访问费','housing':'住房费','food':'饮食费','transport':'交通费','insurance':'保险费','refundable_deposit':'可退押金','reimbursement_prepaid':'报销前垫付','visa':'签证手续费用','miscellaneous':'杂费','processing':'处理费','travel':'交通旅费','living':'生活费参考'}.get(cost.get('kind'),'机构费用')}
        if cost.get('exclude_from_personal_budget'):
            if not cost.get('source_url'): gaps.append('非个人费用排除依据缺失')
            rows.append({**cost,'calculated':None,'reason':'由主办方承担的费用，不计个人预算'}); continue
        condition=cost.get('condition')
        if condition:
            selected=inputs.get('research_mode') if condition in ('lab_based','non_lab_based') else inputs.get(condition)
            if selected in (None,''): gaps.append(cost.get('label',cost['key'])+'：适用情景未知'); continue
            if selected is False or condition in ('lab_based','non_lab_based') and selected != condition:
                rows.append({**cost,'calculated':None,'reason':'所选演示情景不适用此项'}); continue
        group=cost.get('exclusive_group')
        if group and group in exclusive: raise ValidationError('互斥费用不能同时计入')
        if group: exclusive.add(group)
        if cost.get('included_in'):
            parent = next((p for p in path.details.get('costs', []) if p.get('key') == cost['included_in']), None)
            if not parent or parent.get('amount') is None or parent.get('currency') != currency or not parent.get('source_url') or parent.get('included_in'):
                gaps.append(cost.get('label', cost.get('key', '费用')) + '：包含关系未有可计费父项')
            rows.append({**cost, 'calculated': None, 'reason':'声明包含关系，父项完整时避免重复'}); continue
        amount = decimal(cost.get('amount'), nullable=True)
        if cost.get('status') in ('unknown','pending','stale'): gaps.append(cost['label']+'：费用事实尚未核验')
        if cost.get('status') in ('reference_estimate','official_estimate'):
            rows.append({**cost,'calculated':None,'reason':'官方参考估计单列；请将本人选定成本填入私人输入，避免重复计入'})
            continue
        if amount is None or cost.get('currency') != currency or not cost.get('source_url'):
            gaps.append(cost.get('label', cost.get('key', '机构费用')) + '：金额/原币/依据缺失'); continue
        if cost.get('amount_max') is not None:
            gaps.append(cost.get('label', '费用') + '：区间未选定，已知小计采用下限')
        period = cost.get('period'); subtotal = amount
        if period == 'month':
            billed=months.to_integral_value(rounding=ROUND_CEILING) if cost.get('proration') is False else months
            subtotal=amount * billed
        elif period == 'six_month_block': subtotal = amount * (months / Decimal('6')).to_integral_value(rounding=ROUND_CEILING)
        elif period == 'semester':
            terms = decimal(inputs.get('semesters'), nullable=True)
            if terms is None or terms != terms.to_integral_value(): gaps.append('实际学期数未知'); continue
            subtotal = amount * terms
        elif period != 'once': gaps.append('机构费用周期待核实'); continue
        if cost.get('kind') == 'refundable_deposit': refundable += subtotal
        elif cost.get('kind') == 'reimbursement_prepaid': prepaid += subtotal
        elif period=='once': cost_once += subtotal; initial_fees += amount
        else:
            official_monthly += subtotal / months; official_total += subtotal
            if cost.get('payment_timing')=='full_upfront': initial_recurring += subtotal
            elif cost.get('payment_timing')=='periodic': initial_recurring += amount
            else: liquidity_gaps.append(cost['label']+'：付款时间未核验')
        rows.append({**cost, 'calculated': str(subtotal)})
    if not path.details.get('costs_complete', False): gaps.append('机构必要费用覆盖仍待确认')
    personal_monthly = sum((v for k,v in values.items() if k in ('living','housing','insurance','transport') and v is not None), Decimal('0'))
    monthly_cost = personal_monthly + official_monthly
    known_cost = personal_monthly * months + official_total + cost_once
    inputs_complete = not gaps
    evidence_valid = not path_pending(path) and not review_due(path,'visit_policy')
    if not evidence_valid: gaps.append('路径证据过期或待审，仅可复算历史已知金额')
    complete = inputs_complete and evidence_valid
    if path_pending(path): gaps.append('源制度待审，预算仅供历史核对')
    net = known_cost - values['income_confirmed'] * months - values['support_once'] if complete else None
    surplus = values['income_confirmed'] - monthly_cost if complete else None
    initial_personal=sum((values[k] for k in ('living','housing','insurance','transport') if values[k] is not None),Decimal('0'))
    liquidity = initial_fees + initial_recurring + initial_personal + values['deposit'] + values['prepaid'] + refundable + prepaid if complete and not liquidity_gaps else None
    proof = path.details.get('minimum_funds') or {}
    threshold = None
    if proof.get('currency') == currency and proof.get('amount') is not None:
        threshold = decimal(proof['amount']) * (months if proof.get('period') == 'month' else Decimal('1'))
    result = {'rules':RULE_VERSION, 'currency':currency, 'months':str(months), 'complete':complete, 'inputs_complete':inputs_complete, 'evidence_valid':evidence_valid, 'reproducible':True,
        'monthly_known_cost':str(monthly_cost), 'known_cost_subtotal':str(known_cost), 'official_costs':rows,
        'one_time_nonrefundable':str(cost_once), 'refundable_official':str(refundable), 'prepaid_official':str(prepaid),
        'monthly_surplus':str(surplus) if surplus is not None else None, 'net_outlay':str(net) if net is not None else None,
        'initial_liquidity':str(liquidity) if liquidity is not None else None,
        'minimum_proof':str(threshold) if threshold is not None else None, 'proof_evidence':proof,
        'missing':gaps+liquidity_gaps, 'liquidity_complete':liquidity is not None, 'assumption_note':'私人输入为演示/本人假设；月度成本按所填月数线性估计。初始流动性按已核验全期预付或周期付款、首月个人成本及押金累计；付款时间未知时不下充分结论；收入与一次支持到账时间未核实，未提前抵扣。资金证明不是收入。',
        'affordability':'输入完整，可自行核对现金覆盖' if complete and liquidity is not None and values['available_cash'] is not None else '资料不足，不能判断可负担'}
    target_currency=inputs.get('display_currency')
    if target_currency:
        snapshot=ExchangeSnapshot.objects.filter(version=inputs.get('fx_version')).first() if inputs.get('fx_version') else ExchangeSnapshot.objects.order_by('-date').first()
        stale=not snapshot or snapshot.date>timezone.localdate() or (timezone.localdate()-snapshot.date).days>7
        if not snapshot or snapshot.base!='EUR' or snapshot.date>timezone.localdate() or stale and not inputs.get('historical_fx'):
            result['fx_error']='官方汇率缺失/过期；保留原币，历史展示须明确选择'
        else:
            try:
                source_rate=Decimal('1') if currency=='EUR' else decimal(snapshot.rates[currency])
                target_rate=Decimal('1') if target_currency=='EUR' else decimal(snapshot.rates[target_currency])
                if source_rate<=0 or target_rate<=0: raise ValidationError('汇率须大于0')
                result['reference_display']={'currency':target_currency,'base':snapshot.base,'date':snapshot.date.isoformat(),
                    'version':snapshot.version,'url':snapshot.url,'historical':bool(inputs.get('historical_fx')),
                    'rates_direction':'每1 EUR兑换外币单位','values':{k:str(Decimal(str(result[k]))*target_rate/source_rate) if result[k] is not None else None
                        for k in ('monthly_known_cost','known_cost_subtotal','one_time_nonrefundable','refundable_official','monthly_surplus','net_outlay','initial_liquidity','minimum_proof') }}
            except (KeyError,ValidationError,InvalidOperation): result['fx_error']='官方快照无所需有效币种，保留原币'
    return result

def contact_template(target, path):
    dossier = target.details.get('dossier', {})
    mentors = ', '.join(p.name for p in target.mentors.all())
    preparation = dossier.get('contact_preparation') or dossier.get('contact_template') or {}
    draft = preparation.get('english') or preparation.get('english_draft') if isinstance(preparation, dict) else None
    return draft or (f"Dear {mentors or '[HOST NAME]'},\n\nI am [NAME], currently [FORMAL ENROLLMENT / EMPLOYMENT — TO CONFIRM]. "
        f"I am interested in {target.group.name} and its research on [SPECIFIC EVIDENCE-BASED QUESTION]. "
        f"May I ask whether {path.name} could be an appropriate formal route for a visit from [START] to [END]? "
        "My funding is [CONFIRMED SOURCE / NOT YET CONFIRMED]. Could you advise on host availability, eligibility and the correct administrative contact?\n\n"
        "[CV / PUBLICATIONS — INSERT ONLY VERIFIED FACTS]\nBest regards,\n[NAME]")
