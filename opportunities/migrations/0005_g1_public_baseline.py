from django.db import migrations
from django.core.serializers.json import DjangoJSONEncoder
import json

def upgrade(apps, schema_editor):
    Opportunity = apps.get_model('opportunities', 'Opportunity')
    Deadline = apps.get_model('opportunities', 'Deadline')
    Evidence = apps.get_model('opportunities', 'Evidence')
    Revision = apps.get_model('opportunities', 'EntityRevision')
    Alias = apps.get_model('opportunities', 'SourceAlias')
    Relation = apps.get_model('opportunities', 'FundingRelation')
    def row(obj):
        return json.loads(json.dumps({f.name:f.value_from_object(obj) for f in obj._meta.concrete_fields}, cls=DjangoJSONEncoder))
    for opportunity in Opportunity.objects.all():
        before = row(opportunity)
        before['deadlines'] = [row(x) for x in Deadline.objects.filter(opportunity=opportunity)]
        before['financials'] = [row(x) for x in apps.get_model('opportunities','FinancialFact').objects.filter(opportunity=opportunity)]
        before['evidence'] = [row(x) for x in Evidence.objects.filter(opportunity=opportunity)]
        Revision.objects.create(entity_type='opportunity', entity_key=opportunity.key, sequence=1, old={},
            new=before, source_id=opportunity.source_id, source_record_id='legacy:'+opportunity.key,
            observed_at=opportunity.verified_at or opportunity.updated_at,
            reason='G1 migration baseline; original methods and evidence preserved', application_version='G1-baseline')
        Alias.objects.create(source_id=opportunity.source_id, record_id='legacy:'+opportunity.key,
            original_url=opportunity.url, opportunity=opportunity)
        if opportunity.key=='monash-phd-rolling-admission':
            opportunity.deadline_mode='rolling'
            Deadline.objects.filter(opportunity=opportunity).update(effect='rolling')
        elif Deadline.objects.filter(opportunity=opportunity, date__isnull=False, kind__in=['application','funding']).exists():
            opportunity.deadline_mode='fixed'
        opportunity.translation_method='Codex辅助审阅式中文整理；未调用翻译API'
        opportunity.save(update_fields=['deadline_mode','translation_method'])
        for award in opportunity.related_funding.all():
            Relation.objects.get_or_create(opportunity=opportunity, award=award,
                defaults={'mode':'unknown','conditions':'G1关联保留；申请关系须据当前指南核验，资助不能自动相加。'})
    Deadline.objects.filter(kind='consent').update(effect='prerequisite')
    Deadline.objects.filter(kind='opening').update(effect='opening')
    Deadline.objects.filter(time__isnull=False).update(precision='time')
    Evidence.objects.filter(method='manual_review').update(method='codex_assisted_review')

class Migration(migrations.Migration):
    dependencies=[('opportunities','0004_exchangesnapshot_deadline_effect_deadline_precision_and_more')]
    operations=[migrations.RunPython(upgrade, migrations.RunPython.noop)]

