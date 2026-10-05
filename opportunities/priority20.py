"""Frozen discovery scope. Existing local records remain maintainable after activation."""
import hashlib
import json
import re
from pathlib import Path

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

POLICY_KEY = 'priority20-v1'
REGISTRY = Path(settings.BASE_DIR) / 'data/demo/priority20_registry.json'
FROZEN_KEYS = frozenset(f'demo-{i:02}' for i in range(1,21))


def registry():
    data=json.loads(REGISTRY.read_text(encoding='utf8')) if REGISTRY.exists() else {'institutions': []}
    rows=data['institutions']
    if len(rows)!=20 or {r['key'] for r in rows}!=FROZEN_KEYS:
        raise ValidationError('Priority-20 frozen institution set changed; explicit scope review required')
    return data


def scope_fingerprint():
    rows=[{k:r.get(k) for k in ('key','official_name','aliases','organization_keys')} for r in registry()['institutions']]
    return hashlib.sha256(json.dumps(rows,sort_keys=True,ensure_ascii=False).encode()).hexdigest()


def normalized(value):
    return re.sub(r'\s+', ' ', str(value).strip()).casefold()


def institution_key(organization, rows=None):
    rows = rows if rows is not None else registry()['institutions']
    seen = set()
    while organization and organization.pk not in seen:
        seen.add(organization.pk)
        for row in rows:
            if organization.key in row.get('organization_keys', []) or normalized(organization.name) in {
                    normalized(n) for n in [row['official_name'], *row.get('aliases', [])]}:
                return row['key']
        organization = organization.parent if organization.parent_id else None
    return None


def entity_identity(obj):
    return str(getattr(obj, 'key', None) or obj.pk or '')


def host(obj):
    name = obj._meta.model_name
    if name == 'organization': return obj.parent if obj.parent_id else obj
    if name == 'person': return obj.organization if obj.organization_id else None
    return obj.institution if getattr(obj, 'institution_id', None) else None


def policy():
    from .models import DiscoveryPolicy
    state=DiscoveryPolicy.objects.filter(key=POLICY_KEY).first()
    if state and state.registry_fingerprint!=scope_fingerprint():
        raise ValidationError('Priority-20 identity mapping changed since activation; refuse silent scope expansion')
    return state


_NOT_LOADED = object()

def scope_info(obj, state=_NOT_LOADED, rows=None):
    # Explicit None means an already-checked pre-policy instance, not another query per row.
    state = policy() if state is _NOT_LOADED else state
    rows = rows if rows is not None else registry()['institutions']
    key = institution_key(host(obj), rows)
    row = next((r for r in rows if r['key'] == key), {})
    legacy = bool(state and entity_identity(obj) in state.legacy.get(obj._meta.model_name, []))
    channel = dict(row.get('channel', {}))
    if channel.get('priority_channel')=='CSC':
        from .actionability import host_compatibility
        evidence=host_compatibility(key)
        if evidence:
            channel.update(host_csc_acceptance=evidence['host_csc_acceptance'],
                current_program_verified=evidence['strict_csc_actionable'],
                current_application_open=evidence['current_csc_application_open'],
                current_cycle=evidence.get('checks',{}).get('current_cycle',{}).get('value'))
    return {'scope_class': 'legacy_existing' if legacy else 'priority20_new', 'priority20': bool(key),
            'institution_key': key, 'priority_channel': channel.get('priority_channel'),
            'channel_status': channel.get('channel_status', 'unknown'),
            'channel_verified_at': row.get('last_verified_at'),
            'channel_effective_cycle': channel.get('current_cycle'), 'channel': channel}


def guard(sender, instance, raw=False, **kwargs):
    # Fixtures are trusted release input; all normal ORM saves/imports/admin edits use this gate.
    if raw: return
    if sender._meta.model_name=='researchtarget':
        from .actionability import validate_contact
        validate_contact(instance)
    state = policy()
    if state is None: return  # Pre-P20 database; launcher requires explicit safe upgrade before serving.
    scope = scope_info(instance, state)
    if scope['scope_class'] == 'legacy_existing': return
    if scope['priority20']:
        if hasattr(instance, 'curation'):
            instance.curation = {**(instance.curation or {'pool': 'candidate'}),
                                'scope_class': 'priority20_new', 'priority20_key': scope['institution_key']}
        if hasattr(instance,'external_metadata'):
            instance.external_metadata={**instance.external_metadata,'scope_class':'priority20_new','priority20_key':scope['institution_key']}
        return
    if sender._meta.model_name == 'organization' and instance.kind != 'lab':
        return  # An identity/supporting institution is not a selected lab or new opportunity.
    raise ValidationError('新增机会、导师和课题组限于 Priority-20 实际接收机构；原有历史记录可继续维护。')


@transaction.atomic
def activate():
    from .models import DiscoveryPolicy, Opportunity, ResearchTarget, Organization, Person
    existing = policy()
    if existing: return existing, False
    data = registry()
    rows = data['institutions']
    if len(rows) != 20 or len({r['key'] for r in rows}) != 20:
        raise ValidationError('Priority-20 registry must contain exactly 20 unique institutions')
    # Reviewed new keys in a fresh public seed are not grandfathered as legacy.
    new = data.get('new_entity_keys', {})
    legacy = {}
    for model in (Opportunity, ResearchTarget, Organization, Person):
        label = model._meta.model_name
        legacy[label] = []
        for obj in model.objects.all():
            marked=(getattr(obj,'curation',{}) or getattr(obj,'external_metadata',{})).get('scope_class')=='priority20_new'
            reviewed_new=marked and bool(institution_key(host(obj),rows)) and (label=='person' or entity_identity(obj) in new.get(label,[]))
            if not reviewed_new:legacy[label].append(entity_identity(obj))
    return DiscoveryPolicy.objects.create(key=POLICY_KEY, legacy=legacy,
        registry_fingerprint=scope_fingerprint(), activated_at=timezone.now()), True


def require_active():
    if policy() is None:
        raise ValidationError('Priority-20 尚未激活；请先运行 compass.py --data-dir <原数据目录> upgrade。')
