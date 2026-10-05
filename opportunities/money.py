"""Comparable personal cash only. ECB foreign units per EUR; Decimal throughout."""
from decimal import Decimal, InvalidOperation
from django.utils import timezone
from .models import ExchangeSnapshot

def comparable(fact, snapshot, basis='gross_salary', today=None):
    today = today or timezone.localdate()
    if fact.opportunity.pending_change: return None, '有变化待审核；旧金额暂不参与当前比较'
    if snapshot and snapshot.base != 'EUR': return None, '不支持的汇率基准；要求官方EUR方向'
    if not snapshot or snapshot.date > today or (today - snapshot.date).days > 7:
        return None, '汇率缺失 / 超过7日 / 未来日期'
    expected = {'gross_salary': ('salary', 'gross'), 'net_salary': ('salary', 'net'), 'stipend': ('stipend', None)}[basis]
    if fact.kind != expected[0] or expected[1] and fact.tax_basis != expected[1]: return None, '现金性质或税口径不同组'
    if fact.tax_basis == 'unknown': return None, '税口径未知，不参与可比排序'
    advertisement=getattr(fact,'applicability_basis','year')=='advertisement'
    if advertisement:
        observed=getattr(fact,'scope_observed_at',None)
        if not observed or timezone.is_naive(observed) or observed.date()>today or (today-observed.date()).days>30 or fact.applicability_scope!=fact.opportunity.url or fact.opportunity.effective_status!='open':
            return None,'本公告报价范围不匹配、非开放或超过30日；须重新核查'
    if not fact.comparability_verified or not fact.currency_evidence or not (fact.applicable_year or advertisement):
        return None, '币种、适用年度或可比事实未核验'
    if fact.kind == 'salary' and fact.work_basis not in ('actual', 'full_time'): return None, '工时口径未知'
    if fact.work_basis == 'full_time' and fact.opportunity.work_fraction not in (None, Decimal('100')):
        return None, '全职标价与实际工作比例不同；不自动折算'
    if fact.amount is None or not fact.currency: return None, '金额或币种未知'
    if fact.pay_period == 'year': monthly = fact.amount / Decimal('12')
    elif fact.pay_period == 'month':
        if fact.payments_per_year not in (None, 12, 13, 14): return None, '特殊发薪周期尚不支持比较'
        if fact.payments_per_year in (13, 14) and not fact.verification_note: return None, '13/14薪缺少核验依据'
        monthly = fact.amount * Decimal(fact.payments_per_year or 12) / Decimal('12')
    else: return None, '一次性 / 总额 / 周期无法确认，不参与排序'
    try:
        rate = Decimal('1') if fact.currency == snapshot.base else Decimal(snapshot.rates[fact.currency])
        if not rate.is_finite() or rate <= 0: raise InvalidOperation
    except (KeyError, InvalidOperation): return None, '官方汇率缺少该币种'
    return {'monthly_eur': monthly / rate, 'monthly_original': monthly, 'year': '当前公告报价' if advertisement else fact.applicable_year,
        'basis': basis, 'tax_group': fact.tax_basis, 'fx_date': snapshot.date, 'fx_version': snapshot.version, 'fact': fact}, ''

def attach_comparison(items, basis='gross_salary'):
    snapshot = ExchangeSnapshot.objects.order_by('-date').first()
    for item in items:
        values, reasons = [], []
        for fact in item.cash_facts:
            value, reason = comparable(fact, snapshot, basis)
            if value: values.append(value)
            else: reasons.append(reason)
        # Alternative awards/ranges are never added. Conservative lower bound.
        item.comparison = min(values, key=lambda v: v['monthly_eur']) if values else None
        item.comparison_reason = '；'.join(dict.fromkeys(reasons)) or '没有已核验的周期性个人现金'
    return snapshot
