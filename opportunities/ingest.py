"""Reviewed field-scoped import; bounded one-shot network reader."""
import hashlib
import ipaddress
import json
import re
import socket
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.robotparser import RobotFileParser
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_time, parse_datetime
from .models import (Source, Organization, Person, Program, Opportunity, Deadline, FinancialFact,
    Evidence, ImportRun, SourceAlias, SourceAuthorization, FundingRelation)
from .history import public_snapshot, remember

MAX_RESPONSE = 1024 * 1024
CACHE_LIMIT = 20 * 1024 * 1024
USER_AGENT = 'ResearchCompassG2/2.0 (bounded local research; no bulk republication)'


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def canonical_url(url, rules=None):
    parts = urlsplit(url)
    if parts.scheme not in ('https', 'http') or not parts.hostname or parts.username or parts.password:
        raise ValidationError('仅接受不含凭据的 HTTP(S) 来源 URL。')
    ignore = (rules or {}).get('ignore_query', [])
    pairs = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() != 'fbclid' and k not in ignore]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', urlencode(sorted(pairs)), ''))


def opportunity_identity(opportunity):
    round_id = opportunity.round_key if opportunity.type == 'program_round' else opportunity.round_label if opportunity.type == 'scholarship' else ''
    anchor = opportunity.program.key if opportunity.type == 'program_round' and opportunity.program_id else canonical_url(opportunity.url, opportunity.source.url_rules if opportunity.source_id else {})
    parts = [opportunity.type, anchor, round_id]
    # An official roundup may publish multiple separately numbered vacancies.
    # Opt-in preserves all existing identities and does not invent distinct URLs.
    if opportunity.identity_discriminator:
        parts.append(opportunity.identity_discriminator)
    return digest(parts)


def verified_time(value):
    parsed = parse_datetime(value or '')
    if parsed is None or timezone.is_naive(parsed):
        raise ValidationError('核验时间必须为含时区的 ISO 时间。')
    if parsed > timezone.now() + timezone.timedelta(minutes=5):
        raise ValidationError('核验时间不能在未来。')
    return parsed


def numeric(value):
    return Decimal(str(value)) if value not in (None, '') else None


def alias(source, identifier, opportunity, original_url):
    row, created = SourceAlias.objects.get_or_create(source=source, record_id=identifier,
        defaults={'opportunity': opportunity, 'original_url': original_url})
    if not created and row.opportunity_id != opportunity.pk:
        raise ValidationError('稳定标识冲突，须隔离审核，不能自动合并。')


@transaction.atomic
def import_reviewed(payload, input_name='reviewed.json'):
    counts = {'created': 0, 'updated': 0, 'unchanged': 0}
    aliases = {}
    for item in payload.get('sources', []):
        source, created = Source.objects.get_or_create(key=item['key'], defaults={
            'name': item['name'], 'url': item['url'], 'region': item.get('region', '')})
        old = public_snapshot(source)
        canonical_url(item['url'])
        changed_domain = canonical_url(source.url, source.url_rules) != canonical_url(item['url'], source.url_rules)
        for field in ('name', 'url', 'region', 'access_method', 'terms_url', 'robots_url', 'policy_note', 'rate_seconds'):
            if field in item:
                setattr(source, field, item[field])
        # Content import is never an authorization grant or revocation.
        if created:
            source.automation_allowed = False
            source.policy_checked_at = verified_time(item['policy_checked_at']) if item.get('policy_checked_at') else None
            source.url_rules = item.get('url_rules', {})
        elif changed_domain and source.automation_allowed:
            source.automation_allowed = False
            SourceAuthorization.objects.create(source=source, allowed=False, scope=source.scope,
                policy_url=source.terms_url or source.url, evidence='来源主机、路径或有效参数变化，原授权失效；须重新核查。')
        source.full_clean(); source.save()
        if not created and old != public_snapshot(source):
            remember(source, old, source=source, reason='content metadata update; authorization retained unless host changed')
    for record in payload.get('records', []):
        source = Source.objects.get(key=record['source_key'])
        existing = Opportunity.objects.filter(key=record['key']).first()
        record_alias = SourceAlias.objects.filter(source=source, record_id='key:' + record['key']).first()
        existing = existing or (record_alias.opportunity if record_alias else None)
        correction = record.get('identity_correction_evidence')
        if correction:
            if not isinstance(correction, dict) or not all(correction.get(k) for k in ('url', 'quote', 'verified_at')):
                raise ValidationError('身份纠正须结构化正式URL、说明同一轮次的原文和核验时间')
            canonical_url(correction['url']); verified_time(correction['verified_at'])
        if existing and not record.get('identity_correction_evidence'):
            changes_type = 'type' in record and record['type'] != existing.type
            changes_id = existing.official_id and record.get('official_id', existing.official_id) != existing.official_id
            changes_discriminator = record.get('identity_discriminator', existing.identity_discriminator) != existing.identity_discriminator
            changes_program = existing.program_id and record.get('program_key', existing.program.key) != existing.program.key
            changes_round = existing.type == 'program_round' and record.get('round_key', existing.round_key) != existing.round_key
            old_years = set(re.findall(r'20\d{2}', existing.round_key + ' ' + existing.round_label))
            new_years = set(re.findall(r'20\d{2}', record.get('round_key', existing.round_key) + ' ' + record.get('round_label', existing.round_label)))
            if changes_type or changes_id or changes_discriminator or changes_program or changes_round or old_years and new_years and not old_years & new_years:
                raise ValidationError('稳定key的身份或申请年度改变：新轮次须新key；纠正须提供identity_correction_evidence')
        if not record.get('evidence') or not record.get('status_note', existing.status_note if existing else ''):
            raise ValidationError(f"{record.get('key')}: 缺少来源证据或状态说明")
        for e in record['evidence']:
            canonical_url(e['url']); verified_time(e['verified_at'])
        fp = digest(record)
        if existing and existing.fingerprint == fp:
            aliases[record['key']] = existing; counts['unchanged'] += 1; continue
        finance_keys = {'financials','cash_amount','currency','pay_period','tax_basis','funding_source','tuition_waiver','payment_schedule'}
        finance_changes = finance_keys & (set(record) | set(record.get('clear_fields', [])) | set(record.get('complete_fields', [])))
        evidence_fields = {e['field'].lower() for e in record['evidence']}
        if existing and finance_changes and not any(field == 'record' or any(word in field for word in ('finance','funding','cash','salary','stipend','fee','tuition')) for field in evidence_fields):
            raise ValidationError('资金/费用字段变更须同步提供相关字段证据，不能复用无关标题证据')
        institution = record.get('institution', existing.institution.name if existing else '')
        if not institution:
            raise ValidationError('机构不可省略')
        org_key = 'org-' + hashlib.sha256(institution.encode()).hexdigest()[:16]
        org, new_org = Organization.objects.get_or_create(key=org_key, defaults={
            'name': institution, 'country': record.get('country', ''), 'url': record.get('institution_url', '')})
        org_old = public_snapshot(org)
        for field, key in [('url', 'institution_url'), ('country', 'country')]:
            if key in record: setattr(org, field, record[key])
        org.full_clean(); org.save()
        if not new_org and org_old != public_snapshot(org): remember(org, org_old, source=source)
        program = existing.program if existing else None
        round_key = existing.round_key if existing else ''
        if record.get('type', existing.type if existing else None) == 'program_round':
            program, new_program = Program.objects.get_or_create(key=record.get('program_key', program.key if program else ''),
                defaults={'title': record.get('program_title', record.get('title', '')), 'institution': org, 'url': record.get('url', '')})
            program_old = public_snapshot(program)
            if 'program_title' in record: program.title = record['program_title']
            if 'url' in record: program.url = record['url']
            program.full_clean(); program.save()
            if not new_program and program_old != public_snapshot(program): remember(program, program_old, source=source)
            round_key = record.get('round_key') or record.get('round_label') or round_key
            if not round_key: raise ValidationError('申请轮次不能为空')
        else:
            program = None; round_key = ''
        obj = existing or Opportunity(key=record['key'], institution=org, source=source)
        old = public_snapshot(existing) if existing else {}
        fields = ('type', 'title', 'title_zh', 'country', 'region', 'discipline', 'url', 'application_url',
            'round_label', 'status', 'status_note', 'summary_zh', 'eligibility', 'language_requirements',
            'employment_type', 'funding_status', 'funding_source', 'tuition_waiver', 'payment_schedule',
            'official_id', 'identity_discriminator', 'deadline_mode', 'translation_status', 'pending_change', 'translation_method', 'review_method')
        mode = record.get('import_mode', payload.get('import_mode', 'patch'))
        if mode not in ('snapshot', 'patch'): raise ValidationError('import_mode 必须为 snapshot 或 patch')
        clear = record.get('clear_fields', [])
        complete = record.get('complete_fields', []) if mode == 'snapshot' else []
        allowed_clear = set(fields) - {'type', 'title', 'title_zh', 'country', 'region', 'discipline', 'url', 'status'}
        if any(k not in allowed_clear | {'deadlines', 'financials', 'related_funding'} for k in clear + complete):
            raise ValidationError('不能清空必需字段或私有字段')
        for field in fields:
            if field in record: setattr(obj, field, record[field])
            elif field in clear or field in complete:
                default = Opportunity._meta.get_field(field).get_default()
                setattr(obj, field, default if default is not None else '')
        if 'work_fraction' in record: obj.work_fraction = numeric(record['work_fraction'])
        obj.institution, obj.source, obj.program, obj.round_key = org, source, program, round_key
        obj.is_test = bool(record.get('is_test', False)) or bool(existing and existing.is_test)
        obj.fingerprint, obj.review_status = fp, 'verified'
        obj.is_published = True
        if 'record_verified_at' in record: obj.verified_at = verified_time(record['record_verified_at'])
        elif not existing: obj.verified_at = min(verified_time(e['verified_at']) for e in record['evidence'])
        for field in ('fetched_at', 'source_published_at', 'translated_at'):
            if field in record: setattr(obj, field, verified_time(record[field]) if record[field] else None)
        obj.identity = opportunity_identity(obj)
        if not existing:
            # Exact URL/type/round or declared official identity only; no title similarity.
            hit = Opportunity.objects.filter(identity=obj.identity).first()
            if not hit and record.get('official_id') and record.get('entity_link_evidence'):
                hits = Opportunity.objects.filter(official_id=record['official_id'], type=obj.type,
                    institution=org, round_key=obj.round_key, round_label=obj.round_label)
                if hits.count() > 1: raise ValidationError('官方实体标识存在多解，隔离审核')
                hit = hits.first()
            if hit:
                if hit.official_id and obj.official_id and hit.official_id != obj.official_id:
                    raise ValidationError('同页面官方编号不同：须使用独立identity_discriminator，不得合并旧岗位')
                existing = hit; old = public_snapshot(hit)
                values = {field: getattr(obj, field) for field in fields if field in record or field in clear + complete}
                for field in ('work_fraction', 'fetched_at', 'source_published_at', 'translated_at'):
                    if field in record: values[field] = getattr(obj, field)
                values.update(institution=org, source=source, program=program, round_key=round_key,
                    fingerprint=fp, review_status='verified', is_test=obj.is_test or hit.is_test, is_published=True)
                obj = hit
                for field, value in values.items(): setattr(obj, field, value)
        obj.full_clean(); obj.save()
        aliases[record['key']] = obj
        alias(source, 'key:' + record['key'], obj, record.get('url', obj.url))
        if record.get('official_id'): alias(source, 'official:' + record['official_id'], obj, obj.url)
        if 'deadlines' in record or 'deadlines' in clear + complete:
            prior = list(obj.deadlines.all()); retained = []
            for d in record.get('deadlines', []):
                try:
                    day = parse_date(d['date']) if d.get('date') else None
                    clock = parse_time(d['time']) if d.get('time') else None
                except ValueError as error: raise ValidationError('无法解析日期 / 时间') from error
                if d.get('date') and day is None or d.get('time') and clock is None: raise ValidationError('无法解析日期 / 时间')
                kind = {'supervisor_consent': 'consent', 'program_application': 'application', 'scholarship_application': 'funding', 'nomination': 'other'}.get(d.get('kind'), d.get('kind', 'application'))
                candidates = [x for x in prior if x.pk not in retained and (x.step_id == d.get('step_id') if d.get('step_id') else x.kind == kind)]
                # A unique business step survives a label correction; ambiguous old steps remain unmapped.
                existing_step = candidates[0] if len(candidates) == 1 else None
                deadline = Deadline(opportunity=obj, kind=kind, label=d.get('label', '申请截止'), date=day, time=clock,
                    timezone=d.get('timezone', ''), original=d.get('original', ''), status=d.get('status', 'unknown'),
                    effect=d.get('effect', 'prerequisite' if kind == 'consent' else 'opening' if kind == 'opening' else 'hard_close'),
                    precision=d.get('precision', 'time' if clock else 'date' if day else 'unknown'),
                    step_id=d.get('step_id') or (existing_step.step_id if existing_step else '') or (kind if sum(x.get('kind', 'application') == d.get('kind', 'application') for x in record.get('deadlines', [])) == 1 else kind + '-' + digest(d)[:12]))
                deadline.effect={'priority_consideration':'priority'}.get(deadline.effect,deadline.effect)
                if deadline.effect not in dict(Deadline._meta.get_field('effect').choices):
                    deadline.original += '\nUnrecognized source effect: '+str(deadline.effect)
                    deadline.effect='unknown'; deadline.status='unknown'
                deadline.requirement_version = d.get('requirement_version') or digest([kind, str(day), str(clock), deadline.timezone, deadline.effect])[:16]
                if existing_step:
                    deadline.pk = existing_step.pk
                    deadline._state.adding = False
                deadline.full_clean(); deadline.save()
                retained.append(deadline.pk)
            obj.deadlines.exclude(pk__in=retained).delete()
            if 'deadline_mode' not in record:
                obj.deadline_mode = 'fixed' if obj.deadlines.filter(effect='hard_close', date__isnull=False).exists() else 'unknown'
                obj.save(update_fields=['deadline_mode'])
        if 'financials' in record or 'cash_amount' in record or 'financials' in clear + complete:
            obj.financials.all().delete()
            facts = list(record.get('financials') or [])
            if record.get('cash_amount') not in (None, ''):
                facts.append({'kind': 'salary' if record.get('employment_type', obj.employment_type) == 'employee' else 'stipend',
                    'amount': record['cash_amount'], 'amount_max': record.get('cash_max'), 'currency': record.get('currency', ''),
                    'pay_period': record.get('pay_period', 'unknown'), 'tax_basis': record.get('tax_basis', 'unknown'),
                    'note': record.get('cash_note') or '条件与发放以原文为准；未确认币种不折算'})
            for f in facts:
                kwargs = {k: v for k, v in f.items() if k in {x.name for x in FinancialFact._meta.fields} - {'id', 'opportunity'}}
                for k in ('amount', 'amount_max', 'conditional_amount'):
                    if k in kwargs: kwargs[k] = numeric(kwargs[k])
                fact = FinancialFact(opportunity=obj, **kwargs); fact.full_clean(); fact.save()
        # Patch evidence replaces only the named field. Snapshot can explicitly replace all evidence.
        if record.get('evidence_mode') == 'snapshot': obj.evidence.all().delete()
        else: obj.evidence.filter(field__in={e['field'] for e in record['evidence']}).delete()
        for e in record['evidence']:
            evidence = Evidence(opportunity=obj, field=e['field'], quote=e['quote'], url=e['url'],
                verified_at=verified_time(e['verified_at']), method=e.get('method', 'codex_assisted_review'))
            evidence.full_clean(); evidence.save()
        from .curation import invalidate_selection
        invalidate_selection(obj,old)
        remember(obj, old, source=source, record_id=record['key'], reason=f'{mode} reviewed import; field-scoped evidence')
        counts['updated' if existing else 'created'] += 1
    for record in payload.get('records', []):
        complete = record.get('complete_fields', []) if record.get('import_mode', payload.get('import_mode', 'patch')) == 'snapshot' else []
        if 'related_funding' in record or 'related_funding' in record.get('clear_fields', []) + complete:
            obj = aliases[record['key']]
            relation_old = public_snapshot(obj)
            awards = [aliases.get(key) or Opportunity.objects.filter(key=key).first() for key in set(record.get('related_funding', []))]
            if any(a is None or a.type != 'scholarship' for a in awards): raise ValidationError('关联资助必须引用已有奖学金')
            obj.related_funding.set(awards)
            obj.funding_relations.exclude(award__in=awards).delete()
            for award in awards:
                relation = next((r for r in record.get('funding_relations', []) if r['award'] == award.key), {})
                link, _ = FundingRelation.objects.get_or_create(opportunity=obj, award=award, defaults={
                    'mode': 'unknown', 'conditions': '关联存在；申请模式未核验'})
                for field in ('mode', 'conditions', 'evidence', 'exclusive_group'):
                    if field in relation: setattr(link, field, relation[field])
                link.full_clean(); link.save()
            if relation_old != public_snapshot(obj): remember(obj, relation_old, source=obj.source, reason='reviewed funding relationship update')
    for entry in payload.get('directory', []):
        organization = Organization.objects.get(name=entry['institution'])
        if entry.get('lab'):
            canonical_url(entry['lab']['url'])
            organization, _ = Organization.objects.update_or_create(key=entry['lab']['key'], defaults={
                'name': entry['lab']['name'], 'kind': 'lab', 'country': organization.country, 'parent': organization,
                'url': entry['lab']['url'], 'note': '目录不构成当前招生或接收证明。'})
        organization.full_clean(); canonical_url(entry['url'])
        person, _ = Person.objects.update_or_create(name=entry['name'], organization=organization, defaults={
            'url': entry['url'], 'evidence': entry['evidence'], 'verified_at': verified_time(entry['verified_at'])})
        person.full_clean()
        for key in entry.get('opportunity_keys', []): (aliases.get(key) or Opportunity.objects.get(key=key)).mentors.add(person)
        for key in entry.get('program_keys', []): Program.objects.get(key=key).mentors.add(person)
    ImportRun.objects.create(method='codex_assisted_review', input_name=Path(input_name).name, fingerprint=digest(payload), **counts)
    return counts


def public_url(url, source_host):
    parts = urlsplit(url)
    if parts.scheme != 'https' or parts.hostname != source_host or parts.port not in (None, 443) or parts.username or parts.password:
        raise ValueError('自动访问只接受同来源 HTTPS 地址与默认端口')
    for answer in socket.getaddrinfo(parts.hostname, 443):
        if not ipaddress.ip_address(answer[4][0]).is_global:
            raise ValueError('拒绝私网、环回或保留地址')


class SameSourceRedirect(HTTPRedirectHandler):
    def __init__(self, host, allowed_paths=None):
        self.host, self.allowed_paths = host, allowed_paths

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        public_url(newurl, self.host)
        if self.allowed_paths is not None and (urlsplit(newurl).path not in self.allowed_paths or urlsplit(newurl).query):
            raise ValueError('重定向超出获准路径或查询范围')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def bounded_read(url, host, allowed_paths=None):
    public_url(url, host)
    opener = build_opener(SameSourceRedirect(host, allowed_paths))
    with opener.open(Request(url, headers={'User-Agent': USER_AGENT}), timeout=15) as response:
        if response.headers.get_content_type() not in ('text/html', 'text/plain', 'application/json',
            'application/xml', 'text/xml', 'application/rss+xml', 'application/atom+xml'):
            raise ValueError('非支持的文本响应')
        data = response.read(MAX_RESPONSE + 1)
        if len(data) > MAX_RESPONSE:
            raise ValueError('单响应超过 1 MiB')
        return data


def fetch_source(source, url=None):
    """One-shot fetch only for reviewed permission; never changes opportunity status."""
    if not source.automation_allowed or not source.policy_checked_at or source.access_method != 'feed':
        raise ValueError('自动接入未获核准；请使用人工审核 JSON 导入')
    if not source.robots_url:
        raise ValueError('未配置 robots 检查，拒绝自动接入')
    target = url or source.url
    host = urlsplit(source.url).hostname
    public_url(target, host)
    if canonical_url(target, source.url_rules) != canonical_url(source.url, source.url_rules):
        raise ValueError('旧单页读取器只允许已核准的准确入口；其他路径须单独核准适配器')
    if source.scope:
        if host not in source.scope.get('allowed_hosts', []): raise ValueError('当前来源超出已核准主机范围')
        if source.adapter: raise ValueError('核准API必须通过具备范围校验的适配器访问')
    now = timezone.now()
    if source.last_attempt_at and (now - source.last_attempt_at).total_seconds() < source.rate_seconds:
        raise ValueError('未到来源限速间隔')
    source.last_attempt_at = now
    source.save(update_fields=['last_attempt_at'])
    try:
        robot = RobotFileParser()
        robot.parse(bounded_read(source.robots_url, host).decode('utf-8', errors='replace').splitlines())
        if not robot.can_fetch(USER_AGENT, target):
            raise ValueError('robots 禁止该地址，未访问正文')
        delay = max(source.rate_seconds, robot.crawl_delay(USER_AGENT) or 0)
        time.sleep(delay)
        data = None
        for attempt in range(2):
            try:
                data = bounded_read(target, host)
                break
            except (URLError, TimeoutError, OSError) as error:
                if isinstance(error, HTTPError) and error.code in (401, 403, 404, 429):
                    raise
                if attempt == 1:
                    raise
                time.sleep(delay)
        fingerprint = hashlib.sha256(data).hexdigest()
        cache = settings.BASE_DIR / 'cache'
        cache.mkdir(exist_ok=True)
        # Only bounded evidence metadata, no wholesale HTML republication.
        target_file = cache / (source.key + '.json')
        metadata = json.dumps({'url': target, 'fingerprint': fingerprint, 'bytes': len(data),
            'verified_at': timezone.now().isoformat()}, ensure_ascii=False).encode()
        total = sum(p.stat().st_size for p in cache.iterdir() if p.is_file() and p != target_file)
        if total + len(metadata) > CACHE_LIMIT:
            raise ValueError('缓存超过 20 MiB；停止写入，待人工处理')
        target_file.write_bytes(metadata)
        source.last_success_at = timezone.now()
        source.last_error = ''
        source.save(update_fields=['last_success_at', 'last_error'])
        return {'url': target, 'fingerprint': fingerprint, 'bytes': len(data)}
    except (ValueError, URLError, OSError, TimeoutError) as error:
        source.last_error = f'{type(error).__name__}: {str(error)[:350]}'
        source.save(update_fields=['last_error'])
        raise
