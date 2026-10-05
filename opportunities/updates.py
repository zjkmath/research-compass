"""Three fixed, documented public interfaces. No user profile, credentials or arbitrary URL."""
import hashlib
import json
import time
from datetime import datetime, date, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from .ingest import MAX_RESPONSE, CACHE_LIMIT, USER_AGENT, SameSourceRedirect, public_url, digest, import_reviewed
from .history import public_snapshot, remember
from .models import (Source, SourceAuthorization, SourceAlias, UpdateRun, UpdateProposal,
    Opportunity, Person, Organization, ResearchTarget, VisitPath)

NOTICE = 'This product uses the Grants.gov API but is not endorsed or certified by the U.S. Department of Health and Human Services.'

class PlainText(HTMLParser):
    def __init__(self): super().__init__(); self.parts = []
    def handle_data(self, data): self.parts.append(data)

def plain(value):
    parser = PlainText(); parser.feed(str(value or '')); return unescape(' '.join(parser.parts))[:16000]

def configure_sources():
    data = json.loads((settings.BASE_DIR / 'data/demo/source_config.json').read_text(encoding='utf-8'))
    for row in data['sources']:
        scope = row['request_scope']
        url = row['entry'].replace('{entity_id}', scope.get('entity_ids', [''])[0]).replace('{ror_id}', scope.get('approved_ids', [''])[0])
        obj, _ = Source.objects.get_or_create(key=row['key'], defaults={'name': row['family'], 'url': url, 'region': '全球'})
        obj.url, obj.terms_url, obj.robots_url = url, row['terms_url'], row['robots']['url']
        obj.adapter, obj.scope, obj.access_method = row['key'], scope, 'feed'
        obj.policy_checked_at = parse_datetime(row['policy_checked_at']); obj.automation_allowed = True
        obj.rate_seconds = row['rate']['project_min_seconds']; obj.policy_note = row['permissions_zh']
        obj.full_clean(); obj.save()
        if not obj.authorizations.filter(allowed=True, scope=scope, policy_url=row['terms_url']).exists():
            SourceAuthorization.objects.create(source=obj, allowed=True, scope=scope, policy_url=row['terms_url'],
                evidence=row['policy_evidence'] + '\n' + row['permissions_zh'])
    config=settings.BASE_DIR/'data/demo/visit_source.json'
    if config.exists():
        row=json.loads(config.read_text(encoding='utf8'))
        obj,_=Source.objects.get_or_create(key=row['key'],defaults={'name':row['name'],'url':row['url'],'region':'日本'})
        for field in ('url','terms_url','robots_url','policy_note','scope','adapter','rate_seconds'):
            setattr(obj,field,row[field])
        obj.access_method='manual'; obj.automation_allowed=False; obj.policy_checked_at=parse_datetime(row['policy_checked_at']); obj.save()
        SourceAuthorization.objects.get_or_create(source=obj,allowed=True,scope=row['scope'],policy_url=row['terms_url'],defaults={
            'method':'user_authorized_one_shot_public_read','evidence':row['policy_note']})

def request_json(source, url, body=None):
    host = urlsplit(source.url).hostname
    public_url(url, host)
    if not source.automation_allowed or not source.authorizations.filter(allowed=True, scope=source.scope).exists():
        raise ValueError('未核准当前接口范围')
    parts = urlsplit(url); scope = source.scope
    if host not in scope.get('allowed_hosts', []): raise ValueError('当前主机超出政策核准范围')
    if not source.policy_checked_at or timezone.now() >= source.policy_checked_at + timedelta(days=source.review_period_days):
        raise ValueError('来源政策核查已过期，须重新审核')
    if source.retry_after_at and timezone.now() < source.retry_after_at:
        raise ValueError('来源要求Retry-After冷却；本次停止，不占用长等待')
    if source.adapter == 'grants-gov-public-api':
        allowed = parts.path in scope['allowed_paths'] and isinstance(body, dict)
        if parts.path.endswith('search2'):
            allowed = allowed and body == scope['search_body'] and body.get('rows', 0) <= 20
        else: allowed = allowed and set(body) == {'opportunityId'} and isinstance(body['opportunityId'], int)
    elif source.adapter == 'wikidata-entity-json':
        allowed = parts.path in ['/wiki/Special:EntityData/' + i + '.json' for i in scope['entity_ids']] and body is None
    elif source.adapter == 'ror-v2-organizations':
        allowed = parts.path in ['/v2/organizations/' + i for i in scope['approved_ids']] and body is None
    else: allowed = False
    if not allowed or parts.query: raise ValueError('接口地址或参数超出核准范围')
    if source.last_attempt_at:
        delay = source.rate_seconds - (timezone.now() - source.last_attempt_at).total_seconds()
        if delay > 0: time.sleep(delay)
    source.last_attempt_at = timezone.now(); source.save(update_fields=['last_attempt_at'])
    payload = json.dumps(body).encode() if body is not None else None
    for attempt in range(2):
        try:
            request = Request(url, data=payload, headers={'User-Agent': USER_AGENT, 'Accept': 'application/json', 'Content-Type': 'application/json'})
            with build_opener(SameSourceRedirect(host, [parts.path])).open(request, timeout=20) as response:
                if response.headers.get_content_type() != 'application/json': raise ValueError('接口不是JSON')
                raw = response.read(MAX_RESPONSE + 1)
                if len(raw) > MAX_RESPONSE: raise ValueError('单响应超过1MiB')
                result = json.loads(raw)
            return result, {'url': url, 'bytes': len(raw), 'fingerprint': hashlib.sha256(raw).hexdigest(), 'fetched_at': timezone.now().isoformat()}
        except (URLError, OSError, TimeoutError) as error:
            if isinstance(error, HTTPError) and error.code == 429:
                value = error.headers.get('Retry-After', '') if error.headers else ''
                try: until = timezone.now() + timezone.timedelta(seconds=max(10, int(value)))
                except (ValueError, TypeError):
                    try: until = parsedate_to_datetime(value)
                    except (ValueError, TypeError): until = timezone.now() + timezone.timedelta(minutes=10)
                if timezone.is_naive(until): until = timezone.make_aware(until)
                source.retry_after_at = until; source.save(update_fields=['retry_after_at'])
            if isinstance(error, HTTPError) and error.code in (401, 403, 404, 429): raise
            if attempt: raise
            time.sleep(source.rate_seconds)

def grant_payload(source, item, stamp):
    synopsis = item.get('synopsis')
    if not item.get('id') or not item.get('opportunityTitle') or not isinstance(synopsis, dict): raise ValueError('资助详情结构不完整')
    number = str(item['opportunityNumber']); url = 'https://www.grants.gov/search-results-detail/' + str(item['id'])
    eligibility = '; '.join(sorted(x.get('description', '') for x in synopsis.get('applicantTypes', []))) + '\n' + plain(synopsis.get('applicantEligibilityDesc'))
    deadline = synopsis.get('responseDateStr') or synopsis.get('responseDate', '')
    day = None
    for fmt in ('%Y-%m-%d-%H-%M-%S', '%m/%d/%Y', '%b %d, %Y %I:%M:%S %p %Z', '%b %d, %Y'):
        try: day = datetime.strptime(deadline, fmt).date().isoformat(); break
        except ValueError: pass
    facts = []
    floor, ceiling = synopsis.get('awardFloor'), synopsis.get('awardCeiling')
    if str(floor).replace('.', '', 1).isdigit() or str(ceiling).replace('.', '', 1).isdigit():
        facts.append({'kind': 'total_grant', 'amount': floor if str(floor).replace('.', '', 1).isdigit() else None,
            'amount_max': ceiling if str(floor).replace('.', '', 1).isdigit() and str(ceiling).replace('.', '', 1).isdigit() else None,
            'currency': '', 'pay_period': 'total', 'note': 'API award范围是机构项目预算；币种须原文另核实，不是个人现金收入。'})
    summary = plain(synopsis.get('synopsisDesc') or synopsis.get('description') or '')
    record = {'key': 'grants-' + str(item['id']), 'official_id': number, 'type': 'research_funding',
        'title': item['opportunityTitle'], 'title_zh': '机构科研资助 · ' + number, 'country': '美国', 'region': '美国',
        'discipline': '神经科学', 'institution': synopsis.get('agencyName') or item.get('agencyDetails', {}).get('agencyName') or 'U.S. federal agency',
        'source_key': source.key, 'url': url, 'application_url': url, 'status': 'open',
        'status_note': 'Grants.gov posted 检索结果；机构资助，不证明访学名额或个人申请资格。',
        'summary_zh': '正式机构科研资助。标题采用规则标签；研究范围、外国机构资格及附件须阅读原文。',
        'eligibility': eligibility, 'funding_status': 'funded', 'funding_source': number, 'deadline_mode': 'fixed' if day else 'unknown',
        'financials': facts, 'deadlines': [{'kind': 'funding', 'effect': 'hard_close', 'precision': 'date', 'label': '机构资助截止',
            'date': day, 'timezone': '', 'status': 'verified' if day else 'unknown', 'original': deadline}],
        'evidence': [{'field': 'title_type_status', 'quote': item['opportunityTitle'], 'url': url, 'verified_at': stamp},
            {'field': 'eligibility', 'quote': eligibility[:1800] or 'API未提供资格说明', 'url': url, 'verified_at': stamp},
            {'field': 'deadline', 'quote': deadline or 'API未提供截止日期', 'url': url, 'verified_at': stamp},
            {'field': 'finance', 'quote': f'Funding Opportunity Number: {number}; awardFloor: {floor}; awardCeiling: {ceiling}', 'url': url, 'verified_at': stamp}],
        'translation_method': '规则标题标签；模型API未配置；正文未自动翻译', 'translated_at': stamp,
        'fetched_at': stamp, 'record_verified_at': stamp, 'import_mode': 'patch'}
    return {'record': record, 'source_description': summary[:1500], 'source_updated': synopsis.get('lastUpdatedDate', '')}

def parse_entity(item, qid, stamp):
    entity = item.get('entities', {}).get(qid)
    if not entity or not entity.get('lastrevid'): raise ValueError('实体缺少稳定标识或版本')
    metadata = {k: entity.get(k) for k in ('id', 'lastrevid', 'modified')}
    metadata['labels'] = {k: v for k, v in entity.get('labels', {}).items() if k in ('en', 'zh')}
    metadata['claims'] = {k: entity.get('claims', {}).get(k, []) for k in ('P496', 'P856', 'P108', 'P101')}
    return {'metadata': metadata, 'name': entity.get('labels', {}).get('en', {}).get('value', qid),
        'verified_at': stamp, 'limitation': 'Wikidata社区元数据；任职与接收须回官方机构核验；不创建岗位。'}

def revision_kind(source, revision):
    declared = source.scope.get('revision_type')
    if declared: return declared
    # Legacy adapters have explicit, documented revision semantics.
    return {'ror-v2-organizations': 'date', 'wikidata-entity-json': 'integer'}.get(source.adapter, 'integer' if str(revision).isdigit() else 'opaque')

def compare_revision(a, b, kind):
    try:
        if kind == 'integer': x, y = int(a), int(b)
        elif kind == 'date': x, y = date.fromisoformat(a), date.fromisoformat(b)
        elif kind == 'datetime':
            x, y = parse_datetime(a), parse_datetime(b)
            if not x or not y or timezone.is_naive(x) or timezone.is_naive(y): return None
        else: return 0 if a == b else None
        return (x > y) - (x < y)
    except (ValueError, TypeError): return None

def proposal_entity(source, identifier, kind, payload):
    link = SourceAlias.objects.filter(source=source, record_id=identifier).first()
    if link and kind in ('opportunity', 'person', 'organization', 'target', 'path'):
        return getattr(link, kind)
    if kind == 'opportunity': return Opportunity.objects.filter(key=payload.get('record', {}).get('key')).first()
    if kind == 'path': return VisitPath.objects.filter(key=payload.get('key')).first()
    if kind == 'target': return ResearchTarget.objects.filter(key=payload.get('key')).first()

def semantic_facts(value):
    if isinstance(value, dict):
        return {k: semantic_facts(v) for k, v in value.items() if k not in (
            'verified_at', 'fetched_at', 'translated_at', 'record_verified_at', 'updated_at',
            'pending_change', 'translation_status', 'review_status', 'fingerprint','scope_observed_at','revalidated_at','observed_content_at')}
    if isinstance(value, list): return [semantic_facts(v) for v in value]
    return value

def entity_fingerprint(entity):
    return digest(semantic_facts(public_snapshot(entity))) if entity else ''

def release_fingerprint(entity):
    """Fact-equivalent releases may have different database surrogate row IDs."""
    if not entity: return ''
    value=semantic_facts(public_snapshot(entity));value.pop('id',None)
    for field in entity._meta.concrete_fields:
        if field.many_to_one and value.get(field.name) is not None:
            related=getattr(entity,field.name)
            value[field.name]=getattr(related,'key',value[field.name])
    for relation in ('deadlines','financials','evidence'):
        if relation in value:
            rows=[{k:v for k,v in row.items() if k not in ('id','opportunity')} for row in value[relation]]
            value[relation]=sorted(rows,key=lambda row:json.dumps(row,sort_keys=True,ensure_ascii=False))
    return digest(value)


def applied_is_current(proposal, entity=None):
    if not proposal or proposal.status != 'applied' or not proposal.applied_entity_fingerprint:
        return False
    entity = entity or proposal_entity(proposal.source, proposal.record_id, proposal.kind, proposal.payload)
    return bool(entity and proposal.applied_entity_fingerprint == entity_fingerprint(entity))

def latest_state(source, identifier, statuses):
    rows = list(UpdateProposal.objects.filter(source=source, record_id=identifier, status__in=statuses).order_by('pk'))
    latest = None
    for row in rows:
        if latest is None or compare_revision(row.source_revision, latest.source_revision, row.revision_type) in (0, 1) or row.revision_type == 'opaque' and row.status == 'applied':
            latest = row
    return latest

def refresh_pending(entity):
    if not entity or not hasattr(entity, 'pending_change'): return
    kind = {'researchtarget':'target', 'visitpath':'path'}.get(entity._meta.model_name, entity._meta.model_name)
    pending = False
    for p in UpdateProposal.objects.filter(status__in=['pending', 'conflict'], kind=kind):
        if proposal_entity(p.source, p.record_id, p.kind, p.payload) == entity: pending = True; break
    entity.pending_change = pending
    fields = ['pending_change']
    if isinstance(entity, Opportunity):
        entity.translation_status = 'stale' if pending else 'pending' if entity.translation_status=='pending' else 'ready'
        fields += ['translation_status']
    entity.save(update_fields=fields)

@transaction.atomic
def stage(run, identifier, kind, payload, revision, observed):
    def facts(value):
        if isinstance(value, dict):
            return {k: facts(v) for k, v in value.items() if k not in ('verified_at', 'fetched_at', 'translated_at', 'record_verified_at','scope_observed_at','revalidated_at','observed_content_at')}
        if isinstance(value, list): return [facts(v) for v in value]
        return value
    fp = digest(facts(payload))
    version_type = revision_kind(run.source, revision)
    revision = str(revision)
    entity = proposal_entity(run.source, identifier, kind, payload)
    base = entity_fingerprint(entity)
    previous = latest_state(run.source, identifier, ['applied'])
    newest = latest_state(run.source, identifier, ['applied', 'pending', 'conflict', 'rejected'])
    same = UpdateProposal.objects.filter(source=run.source, record_id=identifier, fingerprint=fp).order_by('-pk').first()
    same_order = compare_revision(revision, same.source_revision, version_type) if same else None
    newest_order = compare_revision(revision, newest.source_revision, version_type) if newest else 1
    if same and same_order in (0, 1) and newest_order != -1 and (applied_is_current(same, entity) or same.status in ('pending', 'conflict', 'rejected') and same.base_fingerprint == base):
        if same_order == 1:
            same = UpdateProposal.objects.create(run=run, source=run.source, record_id=identifier, kind=kind, payload=payload,
                diff={'unchanged_facts':True,'previous_proposal':same.pk}, fingerprint=fp, status=same.status, observed_at=observed,
                source_revision=revision, revision_type=version_type, base_fingerprint=base,
                applied_at=same.applied_at, applied_entity_fingerprint=same.applied_entity_fingerprint,
                review_note='同内容较新版本水位；沿用原审核或拒绝状态，不刷新事实核验时间')
        refresh_pending(entity)
        run.unchanged += 1
        return same
    order = compare_revision(revision, newest.source_revision, version_type) if newest else 1
    conflict = compare_revision(revision, revision, version_type) is None or newest and (order == -1 or order is None or order == 0 and newest.fingerprint != fp)
    if conflict:
        row = UpdateProposal.objects.create(run=run, source=run.source, record_id=identifier, kind=kind, payload=payload,
            diff={'before': newest.payload if newest else {}, 'after': payload}, fingerprint=fp, status='conflict', observed_at=observed,
            source_revision=revision, revision_type=version_type, base_fingerprint=base,
            review_note='来源版本较旧、不可排序或同版本内容冲突；需明确人工裁决，不能按抓取时间覆盖')
        run.proposed += 1
        refresh_pending(entity)
        return row
    for obsolete in UpdateProposal.objects.filter(source=run.source, record_id=identifier, status='pending').exclude(fingerprint=fp):
        obsolete.status = 'rejected'; obsolete.review_note = 'Superseded by a newer observed source state'; obsolete.save()
    if previous and previous.fingerprint == fp and applied_is_current(previous, entity):
        if compare_revision(revision, previous.source_revision, version_type) == 1:
            # Record a newer watermark without pretending a fresh human fact review occurred.
            UpdateProposal.objects.create(run=run, source=run.source, record_id=identifier, kind=kind, payload=payload,
                diff={'unchanged_facts': True}, fingerprint=fp, status='applied', observed_at=observed,
                source_revision=revision, revision_type=version_type, base_fingerprint=base,
                applied_at=timezone.now(), applied_entity_fingerprint=base, review_note='同内容新版水位；沿用既有审核，不刷新事实核验时间')
        refresh_pending(entity)
        run.unchanged += 1; return previous
    row, created = UpdateProposal.objects.get_or_create(source=run.source, record_id=identifier, fingerprint=fp, status='pending',
        defaults={'run': run, 'kind': kind, 'payload': payload, 'source_revision': revision, 'observed_at': observed,
            'revision_type': version_type, 'base_fingerprint': base,
            'diff': {'before': previous.payload if previous else {}, 'after': payload}})
    if created: run.proposed += 1
    else: run.unchanged += 1
    if kind == 'opportunity' and created:
        obj = Opportunity.objects.filter(key=payload['record']['key']).first()
        if obj:
            old = public_snapshot(obj); obj.pending_change = True; obj.translation_status = 'stale'
            obj.save(update_fields=['pending_change', 'translation_status'])
            remember(obj, old, source=run.source, record_id=identifier, reason='material source revision pending; old judgement invalidated')
    elif kind == 'target' and created and entity:
        entity.pending_change = True; entity.save(update_fields=['pending_change'])
    refresh_pending(entity)
    return row

def run_source(source, manual_read=False):
    run = UpdateRun.objects.create(source=source, url=source.url,method='live_manual_policy_recheck' if source.adapter=='oist-visit-policy' else 'live_api')
    observations = []
    try:
        if source.adapter == 'oist-visit-policy':
            from .policies import read_oist
            payload,revision,meta=read_oist(source,manual_read); observations.append(meta)
            stage(run,payload['key'],'path',payload,revision,parse_datetime(meta['fetched_at'])); run.parsed+=1
        elif source.adapter == 'grants-gov-public-api':
            from .priority20 import policy
            discovered=[]
            if policy() is None:
                data, meta = request_json(source, source.url, source.scope['search_body']); observations.append(meta)
                if data.get('errorcode') != 0 or not isinstance(data.get('data', {}).get('oppHits'), list): raise ValueError('检索结构变化')
                hits = data['data']['oppHits']
                discovered = [str(h['id']) for h in hits[:source.scope['search_body']['rows']]]
            managed = list(SourceAlias.objects.filter(source=source, opportunity__isnull=False).order_by('checked_at','pk').values_list('record_id', flat=True))
            managed += list(UpdateProposal.objects.filter(source=source, status='applied').order_by('applied_at','pk').values_list('record_id', flat=True))
            ids = list(dict.fromkeys(discovered + [i for i in managed if str(i).isdigit()]))[:20]
            if not ids: raise ValueError('空批次隔离，不关闭已有记录')
            for identifier in ids:
                item, meta = request_json(source, 'https://api.grants.gov/v1/api/fetchOpportunity', {'opportunityId': int(identifier)})
                meta['purpose'] = 'discovery' if identifier in discovered else 'managed_recheck'; observations.append(meta)
                if item.get('errorcode') != 0: raise ValueError('资助详情错误')
                payload = grant_payload(source, item['data'], meta['fetched_at'])
                SourceAlias.objects.filter(source=source,record_id=str(item['data']['id'])).update(checked_at=parse_datetime(meta['fetched_at']))
                stage(run, str(item['data']['id']), 'opportunity', payload, item['data'].get('revision', 0), parse_datetime(meta['fetched_at']))
                run.parsed += 1
        elif source.adapter == 'wikidata-entity-json':
            for qid in source.scope['entity_ids']:
                url = 'https://www.wikidata.org/wiki/Special:EntityData/' + qid + '.json'
                item, meta = request_json(source, url); observations.append(meta)
                payload = parse_entity(item, qid, meta['fetched_at'])
                stage(run, qid, 'person', payload, payload['metadata']['lastrevid'], parse_datetime(meta['fetched_at'])); run.parsed += 1
        elif source.adapter == 'ror-v2-organizations':
            for rid in source.scope['approved_ids']:
                item, meta = request_json(source, 'https://api.ror.org/v2/organizations/' + rid); observations.append(meta)
                if item.get('id') != 'https://ror.org/' + rid or not item.get('names'): raise ValueError('ROR实体结构不完整')
                payload = {k: item.get(k) for k in ('id', 'status', 'names', 'links', 'locations', 'relationships', 'admin', 'external_ids')}
                stage(run, rid, 'organization', payload, item.get('admin', {}).get('last_modified', {}).get('date', ''), parse_datetime(meta['fetched_at'])); run.parsed += 1
        else: raise ValueError('未实现当前适配器')
        if source.adapter=='oist-visit-policy':
            SourceAlias.objects.filter(source=source,record_id=payload['key']).update(checked_at=parse_datetime(meta['fetched_at']))
        run.status = 'pending_review' if run.proposed else 'unchanged'
        source.last_success_at = timezone.now(); source.last_error = ''
    except (ValueError, KeyError, TypeError, URLError, OSError, TimeoutError) as error:
        run.status = 'partial' if run.parsed else 'failed'; run.error = f'{type(error).__name__}: {str(error)[:350]}'
        source.last_error = run.error
    run.fetched_at = timezone.now(); run.fingerprint = digest(observations); run.save()
    source.save(update_fields=['last_success_at', 'last_error'])
    cache = settings.CACHE_DIR; cache.mkdir(exist_ok=True)
    target = cache / (source.key + '.json')
    summary = json.dumps({'run': run.pk, 'observations': observations, 'status': run.status}, ensure_ascii=False).encode()
    if sum(p.stat().st_size for p in cache.iterdir() if p.is_file() and p != target) + len(summary) > CACHE_LIMIT:
        raise ValueError('缓存超过20MiB；停止写入')
    target.write_bytes(summary)
    return run

@transaction.atomic
def apply_proposal(proposal, review_note, adjudicate=False):
    proposal = UpdateProposal.objects.select_for_update().get(pk=proposal.pk)
    if proposal.status == 'applied':
        if applied_is_current(proposal): return 'unchanged'
        raise ValidationError('历史已应用提案不再对应当前有效版本；请重新取证，不能复用旧批准')
    if proposal.status not in (('pending', 'conflict') if adjudicate else ('pending',)) or not review_note.strip(): raise ValidationError('须逐项审核待审提案并注明核查方法')
    if adjudicate and len(review_note.strip()) < 20: raise ValidationError('人工裁决须说明版本与事实依据（至少20字）')
    latest = latest_state(proposal.source, proposal.record_id, ['applied'])
    order = compare_revision(proposal.source_revision, latest.source_revision, proposal.revision_type) if latest else 1
    if latest and (order == -1 or order is None and not adjudicate or order == 0 and proposal.fingerprint != latest.fingerprint and not adjudicate):
        raise ValidationError('旧版本、不可排序版本或同版本冲突不能自动覆盖')
    source, payload = proposal.source, proposal.payload
    current = proposal_entity(source, proposal.record_id, proposal.kind, payload)
    if entity_fingerprint(current) != proposal.base_fingerprint:
        raise ValidationError('提案基线已变化：保留当前人工/其他来源修正，请重新取证和预览')
    # SQLite has no row lock: conditional claim plus atomic write guards competing reviewers.
    previous_status = proposal.status
    if not UpdateProposal.objects.filter(pk=proposal.pk, status=previous_status).update(status='applying'):
        raise ValidationError('另一审核者已处理该提案')
    if proposal.kind == 'opportunity':
        row = dict(payload['record']); row.update(pending_change=False);row.setdefault('translation_status','ready')
        import_reviewed({'records': [row]}, input_name='API proposal ' + str(proposal.pk))
        entity = Opportunity.objects.get(key=row['key'])
    elif proposal.kind == 'person':
        link = SourceAlias.objects.filter(source=source, record_id=proposal.record_id).first()
        org, _ = Organization.objects.get_or_create(key='wikidata-unresolved', defaults={
            'name': 'Wikidata 身份元数据 · 现任机构待核验', 'country': '', 'url': 'https://www.wikidata.org/'})
        entity = link.person if link else Person(name=payload['name'], organization=org,
            url='https://www.wikidata.org/wiki/' + proposal.record_id, verified_at=proposal.observed_at)
        old = public_snapshot(entity) if entity.pk else {}
        entity.external_metadata = payload; entity.evidence = payload['limitation']; entity.save()
        remember(entity, old, source=source, record_id=proposal.record_id, source_revision=proposal.source_revision)
    elif proposal.kind == 'organization':
        link = SourceAlias.objects.filter(source=source, record_id=proposal.record_id).first()
        name = next((n['value'] for n in payload['names'] if 'ror_display' in n.get('types', [])), payload['names'][0]['value'])
        # Explicit institution match from the reviewed ROR ID; never infer a vacancy.
        entity = link.organization if link else Organization.objects.filter(name=name).first()
        entity = entity or Organization(key='ror-' + proposal.record_id, name=name, url=payload['id'])
        old = public_snapshot(entity) if entity.pk else {}; entity.external_metadata = payload; entity.save()
        remember(entity, old, source=source, record_id=proposal.record_id, source_revision=proposal.source_revision)
    elif proposal.kind == 'path':
        entity = VisitPath.objects.get(key=payload['key'])
        old = public_snapshot(entity)
        entity.details = {**entity.details, **payload['details_patch']}
        entity.evidence = entity.evidence + payload.get('evidence', [])
        entity.verified_at = proposal.observed_at
        entity.full_clean(); entity.save()
        for target in ResearchTarget.objects.filter(paths=entity):
            if not target.curation: continue
            target.curation={**target.curation,'support_review_state':'source_changed'}
            target.save(update_fields=['curation'])
        remember(entity, old, source=source, record_id=proposal.record_id, source_revision=proposal.source_revision,
            reason='正式访问制度逐项审核；不确认个人资格或组内名额')
    elif proposal.kind == 'target':
        entity = ResearchTarget.objects.get(key=payload['key']); old = public_snapshot(entity)
        entity.details = {**entity.details, **payload['details_patch']}; entity.save()
        from .curation import invalidate_selection
        invalidate_selection(entity,old)
        remember(entity, old, source=source, record_id=proposal.record_id, source_revision=proposal.source_revision)
    else: raise ValidationError('不支持的提案类型')
    field = proposal.kind
    link, _ = SourceAlias.objects.get_or_create(source=source, record_id=proposal.record_id,
        defaults={field: entity, 'original_url': entity.url, 'checked_at':proposal.observed_at})
    if getattr(link, field + '_id') != entity.pk: raise ValidationError('实体链接冲突')
    link.full_clean()
    proposal.status = 'applied'; proposal.applied_at = timezone.now(); proposal.review_note = review_note
    proposal.applied_entity_fingerprint = entity_fingerprint(entity); proposal.save()
    # Explicit approval of this observation resolves only older observations of this same source record.
    UpdateProposal.objects.filter(source=source, record_id=proposal.record_id, status__in=['pending','conflict'],
        observed_at__lte=proposal.observed_at, pk__lt=proposal.pk).update(status='rejected', review_note='Superseded by explicitly reviewed proposal '+str(proposal.pk))
    UpdateRun.objects.filter(pk=proposal.run_id).update(applied=F('applied') + 1)
    refresh_pending(entity)
    if not proposal.run.proposals.filter(status__in=['pending','conflict']).exists():
        proposal.run.status = 'applied'; proposal.run.save(update_fields=['status'])
    return 'applied'

@transaction.atomic
def reject_proposal(proposal, review_note):
    if not review_note.strip(): raise ValidationError('拒绝须填写原因')
    proposal = UpdateProposal.objects.select_for_update().get(pk=proposal.pk)
    if not UpdateProposal.objects.filter(pk=proposal.pk, status__in=['pending', 'conflict']).update(status='rejected', review_note=review_note):
        raise ValidationError('此提案已处理')
    refresh_pending(proposal_entity(proposal.source, proposal.record_id, proposal.kind, proposal.payload))
    return 'rejected'
