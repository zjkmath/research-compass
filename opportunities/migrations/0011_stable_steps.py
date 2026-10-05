import hashlib
import json
from django.db import migrations


def preserve_steps(apps, schema_editor):
    Deadline = apps.get_model('opportunities', 'Deadline')
    Record = apps.get_model('opportunities', 'UserRecord')
    for record in Record.objects.all():
        mapped = []
        for token in record.completed_steps:
            matches = list(Deadline.objects.filter(opportunity_id=record.opportunity_id))
            matches = [d for d in matches if d.kind + ':' + d.label == token]
            if len(matches) == 1:
                d = matches[0]
                d.step_id = d.step_id or d.kind + '-' + str(d.pk)
                values = [d.kind, str(d.date), str(d.time), d.timezone, d.effect]
                d.requirement_version = hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
                d.save(update_fields=['step_id', 'requirement_version'])
                mapped.append(f'step:{d.step_id}:{d.requirement_version}')
            else:
                mapped.append(token)  # Keep ambiguous legacy evidence for explicit reconciliation.
        record.completed_steps = mapped
        record.save(update_fields=['completed_steps'])
    for d in Deadline.objects.filter(step_id=''):
        d.step_id = d.kind + '-' + str(d.pk)
        values = [d.kind, str(d.date), str(d.time), d.timezone, d.effect]
        d.requirement_version = hashlib.sha256(json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:16]
        d.save(update_fields=['step_id', 'requirement_version'])


class Migration(migrations.Migration):
    dependencies = [('opportunities', '0010_deadline_requirement_version_deadline_step_id_and_more')]
    operations = [migrations.RunPython(preserve_steps, migrations.RunPython.noop)]
