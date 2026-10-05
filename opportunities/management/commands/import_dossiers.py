import json
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from opportunities.models import ResearchTarget, VisitPath
from opportunities.history import public_snapshot, remember
from opportunities.ingest import verified_time, canonical_url
from opportunities.management.commands.import_targets import import_targets

def fact_stamp(value, fallback):
    """A field supplement cannot use an old policy timestamp to reset a newer fee."""
    stamps=[fallback]
    if isinstance(value,dict):
        if value.get('verified_at'): stamps.append(verified_time(value['verified_at']))
        stamps += [fact_stamp(v,fallback) for v in value.values() if isinstance(v,(dict,list))]
    elif isinstance(value,list): stamps += [fact_stamp(v,fallback) for v in value]
    return max(stamps)

@transaction.atomic
def import_dossiers(data):
    counts={'targets_updated':0,'paths_updated':0,'unchanged':0,'requires_review':[]}
    if data.get('candidates'): counts['candidates']=import_targets({'candidates':data['candidates']})
    for row in data['targets']:
        obj=ResearchTarget.objects.get(key=row['key']); dossier=row['dossier']
        if len(dossier.get('research_anchors',[]))<2: raise CommandError('档案须有至少2项内容证据')
        for anchor in dossier['research_anchors']:
            canonical_url(anchor['source_url'])
            if not anchor.get('reading_scope'): raise CommandError('必须标实际读取范围')
        if obj.details.get('dossier')==dossier: counts['unchanged']+=1; continue
        if obj.details.get('dossier') and fact_stamp(dossier,obj.verified_at)<=fact_stamp(obj.details['dossier'],obj.verified_at):
            counts['requires_review'].append('target:'+obj.key); counts['unchanged']+=1; continue
        old=public_snapshot(obj); obj.details={**obj.details,'dossier':dossier}; obj.save()
        remember(obj,old,record_id=obj.key,reason='G3 Codex辅助研究档案；不确认个人身份、资金或名额')
        counts['targets_updated']+=1
    for correction in data.get('corrections',[]):
        key=correction.get('target_key') or correction.get('key') or correction.get('target')
        if not key: continue
        obj=ResearchTarget.objects.get(key=key)
        # Only explicitly source-backed named corrections; never rewrite user records.
        replacement=correction.get('research_basis_corrected') or correction.get('replacement')
        if correction.get('field')=='research_basis' and not replacement and correction.get('source_url'):
            replacement='；'.join(f"{a['title']}（{a.get('year','年份待核')}）：{a.get('connection','内容见公开档案')}" for a in obj.details['dossier']['research_anchors'])+'。研究分析不代表具体接收名额。'
        if replacement and obj.details.get('research_basis')!=replacement:
            old=public_snapshot(obj); obj.details={**obj.details,'research_basis':replacement}; obj.save()
            remember(obj,old,record_id=key,reason='G3论文书目正式来源纠正')
    for row in data.get('path_patches',[]):
        obj=VisitPath.objects.get(key=row['key']); patch=row['details_patch']
        stamp=patch.get('verified_at') or max((e['verified_at'] for e in row.get('evidence',[]) if e.get('verified_at')),default=None)
        if stamp:
            # Equal timestamps with different content require review; never infer an ordering.
            blocked=[k for k,v in patch.items() if k in obj.details and v!=obj.details[k] and fact_stamp(v,verified_time(stamp))<=fact_stamp(obj.details[k],obj.verified_at)]
            counts['requires_review'] += ['path:'+obj.key+':'+k for k in blocked]
            patch={k:v for k,v in patch.items() if k not in blocked}
        details={**obj.details,**patch}
        for e in row.get('evidence',[]): canonical_url(e['url']); verified_time(e['verified_at'])
        evidence=obj.evidence+[e for e in row.get('evidence',[]) if e not in obj.evidence]
        if details==obj.details and evidence==obj.evidence: counts['unchanged']+=1; continue
        old=public_snapshot(obj); obj.details=details; obj.evidence=evidence
        # A field-scoped supplement must not refresh unrelated old policy facts.
        obj.full_clean(); obj.save(); remember(obj,old,record_id=obj.key,reason='G3访问路径必要事实补充，既有未更新事实保留原核验时刻')
        counts['paths_updated']+=1
    return counts

class Command(BaseCommand):
    help='Idempotent public dossier supplement; never imports private profiles/drafts.'
    def add_arguments(self,parser): parser.add_argument('file')
    def handle(self,*args,**options):
        self.stdout.write(json.dumps(import_dossiers(json.loads(Path(options['file']).read_text(encoding='utf8'))),ensure_ascii=False))
