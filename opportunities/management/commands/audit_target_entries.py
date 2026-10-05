"""Entry/path/funding check for existing targets; never infers personal acceptance."""
import json, re
from urllib.parse import urljoin,urlsplit
from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.utils import timezone
from opportunities.discovery import Reader,plain
from opportunities.models import ResearchTarget

class Command(BaseCommand):
    def add_arguments(self,p):
        p.add_argument('--manual-read',action='store_true');p.add_argument('--max-targets',type=int,default=20)
    def handle(self,*args,**o):
        if not o['manual_read'] or not 1<=o['max_targets']<=20:raise CommandError('仅明确人工触发的20目标有界检查')
        directory=settings.DATA_DIR/'discovery';directory.mkdir(parents=True,exist_ok=True)
        out=directory/'target_entries.json';rows=json.loads(out.read_text(encoding='utf8')) if out.exists() else []
        done={r['key'] for r in rows};memo={}
        for t in ResearchTarget.objects.select_related('institution','group').prefetch_related('paths').order_by('key')[:o['max_targets']]:
            if t.key in done:continue
            row={'key':t.key,'institution':t.institution.name,'group':t.group.name,'checked_at':timezone.now().isoformat(),
                'scope':'现有官方组主页、其公开招聘链接、已有正式路径与资金/费用原文；不精读论文；不联系机构',
                'current_acceptance':'UNKNOWN：制度/研究相关不证明本人的当前接收','maintenance':'招聘入口30天；制度/资金90天；单次人工复核，失败不关闭'}
            def check(url):
                if url in memo:return memo[url][0]
                result={'url':url,'checked_at':timezone.now().isoformat()}
                try:
                    reader=Reader(settings.CACHE_DIR/'target_entries',[urlsplit(url).hostname]);html=reader.read(url)
                    result.update(status='LIVE_SOURCE',title=plain(re.search(r'<title[^>]*>(.*?)</title>',html,re.I|re.S).group(1))[:180])
                    memo[url]=(result,html)
                except Exception as e:result.update(status='UNVERIFIED',reason=str(e));memo[url]=(result,'')
                return result
            row['group_page']=check(t.url)
            html=memo[t.url][1]
            links=[(urljoin(t.url,u),plain(label)) for u,label in re.findall(r'<a[^>]+href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>',html,re.I|re.S)]
            recruit=[(u,label) for u,label in links if re.search(r'vacanc|open position|join us|job|recruit|career',label,re.I)]
            same=[(u,l) for u,l in recruit if urlsplit(u).hostname==urlsplit(t.url).hostname]
            row['recruitment_entry']=check(same[0][0]) if same else {'url':t.url,'status':'UNKNOWN','reason':'已查组主页未定位独立招聘链接；不代表没有招聘'}
            row['candidate_recruitment_links']=[{'url':u,'label':label[:100]} for u,label in recruit[:4]]
            row['formal_paths']=[check(p.url) for p in t.paths.all()]
            funds=[u for p in t.paths.all() for c in p.details.get('costs',[]) if (u:=c.get('source_url')) and not u.lower().endswith('.pdf')]
            funds+= [u for u,label in links if re.search(r'scholarship|funding|financial aid',label,re.I) and urlsplit(u).hostname==urlsplit(t.url).hostname]
            row['funding_entries']=[check(u) for u in dict.fromkeys(funds)] or [{'status':'UNKNOWN','reason':'已查范围未定位可确认资助入口；费用页不计新增资助'}]
            rows.append(row);out.write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf8')
            self.stdout.write(t.key+': checked; acceptance UNKNOWN')
