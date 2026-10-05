import hashlib
import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from opportunities.ingest import canonical_url, verified_time
from opportunities.history import public_snapshot, remember
from opportunities.models import Organization, Person, VisitPath, ResearchTarget

REGIONS = {'europe': '欧洲', 'united_states': '美国'}
COUNTRIES = {'Switzerland': '瑞士', 'United Kingdom': '英国', 'Austria': '奥地利', 'United States': '美国'}

def import_targets(payload):
    counts = {'created': 0, 'updated': 0, 'unchanged': 0}
    for row in payload['candidates']:
        with transaction.atomic():
            if not row.get('evidence') or not row.get('research_basis') or len(row.get('questions', [])) < 2:
                raise CommandError('候选缺少研究证据或待确认问题')
            for e in row['evidence']:
                canonical_url(e['url']); verified_time(e['verified_at'])
            stamp = min(verified_time(e['verified_at']) for e in row['evidence'])
            country = COUNTRIES.get(row['country'], row['country'])
            org_key = 'org-' + hashlib.sha256(row['institution'].encode()).hexdigest()[:16]
            institution, _ = Organization.objects.get_or_create(key=org_key, defaults={
                'name': row['institution'], 'country': country, 'url': row['path']['url']})
            group, _ = Organization.objects.update_or_create(key='lab-' + row['key'], defaults={
                'name': row['group'], 'kind': 'lab', 'country': country, 'parent': institution, 'url': row['url'],
                'note': '研究候选目录；不构成当前接收或岗位证明。'})
            visits = []
            for path in row.get('paths', [row['path']]):
                path_key = 'path-' + hashlib.sha256((institution.key + path['url'] + path['name']).encode()).hexdigest()[:24]
                if path.get('existing_key'):
                    visit=VisitPath.objects.get(key=path['existing_key'])
                    if visit.institution_id!=institution.pk or canonical_url(visit.url)!=canonical_url(path['url']) or set(visit.identity_types)!=set(path['identity_types']):
                        raise CommandError('既有正式路径身份、机构或URL不同，不能靠标题复用')
                    visits.append(visit); continue
                existing_path=VisitPath.objects.filter(key=path_key).first()
                if existing_path:
                    visits.append(existing_path)
                    # An unchanged/older directory import cannot roll back later field-scoped policy work.
                    latest=verified_time(existing_path.details['verified_at']) if existing_path.details.get('verified_at') else existing_path.verified_at
                    if stamp <= latest: continue
                visit, _ = VisitPath.objects.update_or_create(key=path_key, defaults={'institution': institution,
                    'name': path['name'], 'url': path['url'], 'identity_types': path['identity_types'], 'details': path,
                    'evidence': [e for e in row['evidence'] if 'path' in e['field'] or 'student' in e['field'] or 'scholar' in e['field']],
                    'verified_at': stamp, 'is_published':True})
                visits.append(visit)
            obj = ResearchTarget.objects.filter(key=row['key']).first()
            old = public_snapshot(obj) if obj else {}
            if obj and all(obj.details.get(k)==v for k,v in row.items()):
                counts['unchanged'] += 1; continue
            obj = obj or ResearchTarget(key=row['key'])
            for field in ('receiving_status', 'eligibility_status', 'funding_type', 'match_category', 'themes', 'url'):
                setattr(obj, field, row[field])
            obj.region = REGIONS.get(row['region'], row['region']); obj.country = country
            details={**obj.details,**row}
            obj.institution, obj.group, obj.details, obj.evidence, obj.verified_at = institution, group, details, row['evidence'], stamp
            obj.is_published=True
            obj.full_clean(); obj.save(); obj.paths.set(visits)
            people = []
            for pi in row['mentors']:
                canonical_url(pi['url'])
                person, _ = Person.objects.update_or_create(name=pi['name'], organization=group, defaults={
                    'url': pi['url'], 'verified_at': stamp, 'evidence': '身份及研究见关联候选的官方字段证据；名额未知。'})
                people.append(person)
            obj.mentors.set(people)
            remember(obj, old, record_id=row['key'], reason='official-page Codex assisted research review; not a recruitment record')
            counts['updated' if old else 'created'] += 1
    return counts

class Command(BaseCommand):
    help = 'Import reviewed public research candidates; never creates opportunities or binds private profiles.'
    def add_arguments(self, parser):
        parser.add_argument('files', nargs='+')
    def handle(self, *args, **options):
        for filename in options['files']:
            self.stdout.write(json.dumps(import_targets(json.loads(Path(filename).read_text(encoding='utf-8')))))
