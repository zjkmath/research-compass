"""Public fact snapshots; never includes user records, notes or local profiles."""
import json
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Max
from django.utils import timezone
from .models import EntityRevision

def public_snapshot(entity):
    values={f.name:f.value_from_object(entity) for f in entity._meta.concrete_fields}
    # Neutral new classification fields do not change archived source facts before
    # their guarded evidence review. This preserves old upgrade preconditions.
    neutral={'identity_fit':'unknown','role_class':'unknown','host_capability':'unknown',
        'contact_receiving_status':'unknown_contact_to_confirm','actionability':'research_watchlist','classification_evidence':{}}
    for field,default in neutral.items():
        if values.get(field)==default:values.pop(field,None)
    if entity._meta.model_name=='opportunity':
        # Empty discriminator is the pre-P20 identity. Keep its historical fact
        # fingerprint stable so reviewed old-release corrections still apply.
        # A real advert discriminator remains a material identity fact.
        if not values.get('identity_discriminator'):
            values.pop('identity_discriminator',None)
        for relation in ('deadlines','financials','evidence'):
            values[relation]=[{f.name:f.value_from_object(row) for f in row._meta.concrete_fields}
                              for row in getattr(entity,relation).all()]
        values['related_funding'] = list(entity.related_funding.values_list('key', flat=True))
        values['funding_relations'] = list(entity.funding_relations.values('award__key', 'mode', 'conditions', 'evidence', 'exclusive_group'))
    return json.loads(json.dumps(values,cls=DjangoJSONEncoder))

def remember(entity, old, *, source=None, record_id='', source_revision='', observed_at=None, reason='reviewed revision'):
    kind=entity._meta.model_name
    key=str(getattr(entity,'key',entity.pk))
    latest=EntityRevision.objects.filter(entity_type=kind,entity_key=key).aggregate(n=Max('sequence'))['n'] or 0
    return EntityRevision.objects.create(entity_type=kind,entity_key=key,sequence=latest+1,old=old,
        new=public_snapshot(entity),source=source,source_record_id=record_id,source_revision=source_revision,
        observed_at=observed_at or timezone.now(),reason=reason)

