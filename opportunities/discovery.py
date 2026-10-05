"""Bounded public employer discovery; snapshots, cursors and row failures are durable."""
import json, re, time, hashlib, os
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit, unquote
from urllib.request import Request, build_opener
from urllib.robotparser import RobotFileParser
from datetime import datetime, date
from decimal import Decimal, InvalidOperation
from urllib.error import HTTPError
from django.conf import settings
from django.utils import timezone
from .ingest import MAX_RESPONSE, CACHE_LIMIT, public_url, SameSourceRedirect, digest, opportunity_identity, import_reviewed
from .models import Opportunity, Source, UpdateRun, UpdateProposal
from .updates import stage, apply_proposal

UA='ResearchCompass/4 (bounded noncommercial local research)'
class Text(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]
    def handle_data(self,value): self.parts.append(value)
def plain(value):
    p=Text();p.feed(re.sub(r'<(script|style)\b.*?</\1>','',value,flags=re.I|re.S))
    return re.sub(r'\s+',' ',unescape(' '.join(p.parts))).strip()
RULE_VERSION='doctoral-v3-20261002'
def candidate(title):
    doctoral_part=re.sub(r'\bpost[ -]?doc(?:toral)?\b','',title,flags=re.I)
    positive=bool(re.search(r'\bph\.?d\b|\bdoctoral\b|\bdoktorand\w*\b|\bdoctorant\w*\b',doctoral_part,re.I))
    negative=bool(re.search(r'\bpost[ -]?doc(?:toral)?\b|\blicentiate\b|\bintern(?:ship)?\b|\bprofessor\b|\blecturer\b|\bassistant professor\b',title,re.I))
    if positive and negative:
        return 'mixed' if re.search(r'\b(?:and|or|&|positions|students|candidates)\b',title,re.I) and not re.search(r'professor|lecturer',title,re.I) else 'needs_detail'
    if positive:return 'doctoral'
    if negative:return 'non_doctoral'
    if re.search(r'\bresearcher\b|\bforskare\b|\bbioinformatician\b',title,re.I):return 'needs_detail'
    if re.search(r'\b(administrat\w*|officer|engineer\w*|technician\w*|manager|assistant|assistent\w*|controller|coordinator|specialist|researcher|postdoktor\w*|universitetslektor\w*|universitetsadjunkt|univeristetsadjunkt|laboratorie\w*|techniker\w*|hausmeister\w*|informatiker\w*|kauffrau|kaufmann|referent\w*|architect|bioinformatician|forskningsassistent\w*|forskare|ekonom\w*|amanuens\w*|koordinator|kursadministratör|säkerhetsanalytiker|gebäudetechniker|systemingenieur|informationsspezialist|hilfsassistent|mediamatiker|leiter|projektleiter|labortechniker|projekteinkäufer|fachperson|spezialist|studienkoordinator|betriebsleiter|elektrotechniker|elektroingenieur|medewerker|secretariaat|engD|teacher|projectleider|projectmanager|adviseur|coördinator|advisor|instrumentmaker|technicus|librarian|legal counsel|brandschutzfachfrau|head of research|finance|landwirtschaftliche)\b',title,re.I):return 'non_doctoral'
    return 'needs_detail'
def doctoral(title):
    return candidate(title) in ('doctoral','mixed') and not re.search(r'\b(professor|lecturer)\b',title,re.I)
def lists(kind,html,url):
    rows=[]; next_url=None
    if kind=='kth':
        match=re.search(r'window.__compressedData__DATA\s*=\s*"([^"]+)"',html)
        if not match:raise ValueError('KTH list structure drift')
        data=json.loads(unquote(match.group(1)))['jobData'];next_url=data['links'].get('next')
        if next_url:raise ValueError('KTH public snapshot has undisplayed API continuation; do not call hidden API')
        for item in data['data']:
            a=item['attributes'];texts=a['translations']['texts']
            rows.append({'id':item['id'],'title':texts['title'],'url':urljoin(url,'/lediga-jobb/'+item['id']+'?l=en'),
                'deadline':a['dates'].get('deadline'),'published':a['dates'].get('published'),'official_id':a.get('reference',''),
                'seats':a.get('positions'),'list_quote':texts['title']})
    elif kind in ('tue','lund','varbi'):
        for tr in re.findall(r'<tr\b[^>]*>.*?</tr>',html,re.I|re.S):
            match=re.search(r'<a\b[^>]*href="([^"]*jobID[:%][^"]*)"[^>]*>(.*?)</a>',tr,re.I|re.S)
            if not match:continue
            link=unescape(match.group(1));title=plain(match.group(2));record_id=re.search(r'jobID(?::|%3A)(\d+)',link,re.I).group(1)
            days=re.findall(r'\b20\d\d-\d\d-\d\d\b',tr)
            deadline=(re.search(r'data-job-ends="([^"]+)"',tr).group(1) if kind=='lund' else days[-1] if days else None)
            published=re.search(r'data-job-published="([^"]+)"',tr)
            rows.append({'id':record_id,'title':title,'url':urljoin(url,link),'deadline':deadline,'published':None,'published_date':published.group(1) if published else None,'official_id':'','seats':None,'list_quote':title})
        next_match=re.search(r'<a[^>]+href="([^"]+)"[^>]+rel="next"',html,re.I)
        next_url=urljoin(url,unescape(next_match.group(1))) if next_match else None
    elif kind=='eth':
        for link,title in re.findall(r'<a\b[^>]*href="([^"]*job/view/[^"]+)"[^>]*>(.*?)</a>',html,re.I|re.S):
            heading=re.search(r'<h3[^>]*>(.*?)</h3>',title,re.S)
            if heading:rows.append({'id':link.rsplit('/',1)[-1],'title':plain(heading.group(1)),'url':urljoin(url,link),'deadline':None,'published':None,'official_id':'','seats':None,'list_quote':plain(heading.group(1))})
    if not rows:raise ValueError('Empty list/structure drift; not a closure signal')
    return list({r['id']:r for r in rows}.values()),next_url

class Reader:
    def __init__(self, directory, hosts, delay=2):
        self.directory=directory;directory.mkdir(parents=True,exist_ok=True);self.hosts=hosts;self.delay=delay;self.requests=0;self.robots={};self.last=0;self.request_headers={};self.response_meta={};self.last_meta={}
    def raw(self,url):
        host=urlsplit(url).hostname
        if host not in self.hosts:raise ValueError('Host outside reviewed public scope')
        public_url(url,host);time.sleep(max(0,self.delay-(time.monotonic()-self.last)))
        self.last=time.monotonic();self.requests+=1
        try:
            with build_opener(SameSourceRedirect(host)).open(Request(url,headers={'User-Agent':UA,'Accept-Encoding':'identity',**self.request_headers}),timeout=15) as response:
                self.response_meta=dict(response.headers.items());raw=response.read(MAX_RESPONSE+1)
                if len(raw)>MAX_RESPONSE:raise ValueError('Response exceeds 1MiB')
                if response.headers.get('Content-Encoding','').lower()=='gzip':
                    import gzip,io
                    raw=gzip.GzipFile(fileobj=io.BytesIO(raw)).read(MAX_RESPONSE+1)
                    if len(raw)>MAX_RESPONSE:raise ValueError('Decoded response exceeds 1MiB')
                return raw.decode('utf8',errors='replace')
        except HTTPError as e:
            if e.code==304:self.response_meta={**dict(e.headers.items()),'status':304};return None
            raise
    def read(self,url,refresh=False,ttl=3600):
        host=urlsplit(url).hostname
        if host not in self.robots:
            robot=self.raw('https://'+host+'/robots.txt');p=RobotFileParser();p.parse(robot.splitlines());self.robots[host]=p
        if not self.robots[host].can_fetch(UA,url):raise ValueError('robots disallows public URL')
        path=self.directory/(hashlib.sha256(url.encode()).hexdigest()+'.html')
        meta_path=path.with_suffix('.json');meta=json.loads(meta_path.read_text(encoding='utf8')) if meta_path.exists() else {}
        now=timezone.now();stamp=meta.get('revalidated_at') or meta.get('observed_content_at')
        control=meta.get('cache_control','').lower()
        max_age=re.search(r'max-age=(\d+)',control)
        effective_ttl=0 if 'no-cache' in control else min(ttl,int(max_age.group(1))) if max_age else ttl
        if path.exists() and stamp and not refresh and now.timestamp()-datetime.fromisoformat(stamp).timestamp()<effective_ttl:
            self.last_meta={**meta,'mode':'CACHE_SAME_VALIDITY'};return path.read_text(encoding='utf8')
        self.request_headers={k:v for k,v in [('If-None-Match',meta.get('etag')),('If-Modified-Since',meta.get('last_modified'))] if v} if path.exists() else {}
        self.response_meta={};html=self.raw(url);headers={k.lower():v for k,v in self.response_meta.items()}
        if html is None:
            if not path.exists():raise ValueError('304 without corresponding cached body')
            meta.update(revalidated_at=now.isoformat(),mode='LIVE_304')
            if headers.get('cache-control'):meta['cache_control']=headers['cache-control']
            self.last_meta=meta
            if 'no-store' in meta.get('cache_control','').lower():
                body=path.read_text(encoding='utf8');path.unlink();meta_path.unlink(missing_ok=True);return body
            atomic_json(meta_path,meta);return path.read_text(encoding='utf8')
        raw=html.encode();fingerprint=hashlib.sha256(raw).hexdigest()
        self.last_meta={'url':url,'sha256':fingerprint,'fetched_at':now.isoformat(),'observed_content_at':meta.get('observed_content_at') if meta.get('sha256')==fingerprint else now.isoformat(),
            'revalidated_at':now.isoformat(),'mode':'LIVE_200','etag':headers.get('etag'),'last_modified':headers.get('last-modified'),'cache_control':headers.get('cache-control','')}
        if re.search(r'\bno-store\b',headers.get('cache-control',''),re.I):
            path.unlink(missing_ok=True);meta_path.unlink(missing_ok=True);return html
        used=sum(p.stat().st_size for p in settings.CACHE_DIR.rglob('*') if p.is_file())-(path.stat().st_size if path.exists() else 0)
        if len(raw)>MAX_RESPONSE:raise ValueError('Cached text exceeds 1MiB')
        for old in sorted(settings.CACHE_DIR.rglob('*.html'),key=lambda p:p.stat().st_mtime):
            if used+len(raw)+2048<=CACHE_LIMIT:break
            if old==path or old.is_symlink():continue
            used-=old.stat().st_size;old.unlink();old.with_suffix('.json').unlink(missing_ok=True)
        if used+len(raw)+2048>CACHE_LIMIT:raise ValueError('Cache capacity exhausted by retained non-cache files')
        temporary=path.with_suffix('.tmp');temporary.write_bytes(raw);os.replace(temporary,path);atomic_json(meta_path,self.last_meta);return html

def atomic_json(path,value):
    temporary=path.with_suffix('.tmp');raw=json.dumps(value,ensure_ascii=False,indent=2).encode()
    with temporary.open('wb') as stream:stream.write(raw);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,path)

def section_text(html, labels):
    headings=list(re.finditer(r'<h[2-4]\b[^>]*>(.*?)</h[2-4]>',html,re.I|re.S))
    for i,h in enumerate(headings):
        if re.fullmatch(labels,plain(h.group(1)),re.I):
            return plain(html[h.end():headings[i+1].start() if i+1<len(headings) else len(html)])[:1600]
    return ''


def qualifications(html):
    section=section_text(html,r'job requirements|qualifications?|qualification requirements|your profile|requirements|eligibility(?: requirements)?|entry requirements')
    if section:return section
    passages=[plain(p) for p in re.findall(r'<(?:p|li)\b[^>]*>(.*?)</(?:p|li)>',html,re.I|re.S)]
    selected=[]
    for p in passages:
        m=re.search(r'\bmaster\w*\b|\bdegree\b|\bqualification\w*\b|\beligible\b|\bexamen\b',p,re.I)
        if m:selected.append(p[max(0,m.start()-120):m.end()+700])
    return ' '.join(dict.fromkeys(selected))[:1600]


def known_dates(html,item):
    """Only explicit application/consent phrases; ambiguous numeric dates stay unknown."""
    text=plain(html);rows=[]
    pattern=r'20\d{2}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-z]+\s+20\d{2}|[A-Za-z]+\s+\d{1,2},?\s+20\d{2}'
    for marker in re.finditer(r'application deadline|deadline for applications|closing date|last application date|apply by|sista ansökningsdag|Bewerbungsfrist|(?:contact|obtain|secure)[^.;]{0,60}(?:supervisor|consent)[^.;]{0,40}\bby\b',text,re.I):
        span=text[marker.start():marker.end()+130]
        match=re.search(pattern,span)
        if not match:continue
        value=match.group(0);day=None
        for fmt in ('%Y-%m-%d','%d %B %Y','%d %b %Y','%B %d, %Y','%B %d %Y','%b %d, %Y','%b %d %Y'):
            try:day=datetime.strptime(value,fmt).date().isoformat();break
            except ValueError:pass
        if not day:continue
        prerequisite=bool(re.search(r'supervisor|consent',marker.group(),re.I))
        rows.append({'kind':'consent' if prerequisite else 'application','effect':'prerequisite' if prerequisite else 'hard_close',
            'label':'导师同意前置步骤' if prerequisite else '申请截止','date':day,'original':span[:match.end()],
            'status':'verified','precision':'date','evidence_scope':'official_detail'})
    if not any(r['kind']=='application' for r in rows) and item.get('deadline'):
        raw=str(item['deadline'])[:10]
        try:day=date.fromisoformat(raw).isoformat()
        except ValueError:day=None
        if day:rows.append({'kind':'application','effect':'hard_close','label':'申请截止','date':day,'original':str(item['deadline']),'status':'verified','precision':'date','evidence_scope':'official_list'})
    unique={}
    for row in rows:unique[(row['kind'],row['date'])]=row
    return list(unique.values())


def record(config,item,html,stamp):
    text=plain(html);title=item['title']
    qualification_quote=' '.join(plain(p) for p in re.findall(r'<p\b[^>]*>(.*?)</p>',html,re.I|re.S) if re.search(r'qualif|degree|examen|PhD|Ph.D|MSc|master|docent|competence|forskare',plain(p),re.I))[-1400:]
    heading=re.search(r'<h1[^>]*>(.*?)</h1>',html,re.I|re.S);detail_title=plain(heading.group(1)) if heading else title
    route=candidate(detail_title)
    if route=='non_doctoral' and not doctoral(title):raise ValueError('non_doctoral_detail:'+detail_title)
    student_offer=re.search(r'\b(?:ph\.?d|doctoral)\s+(?:student|candidate|researcher|position|positions|students|candidates)|\bdoktorand\w*\b|\bdoctorant\w*\b',text,re.I)
    completed=re.search(r'(?:hold|completed|obtained|have|required|must possess)[^.]{0,45}\bph\.?d\b|(?:doctoral degree|doctorsexamen)',text,re.I)
    recruiting=re.search(r'(?:recruit|looking for|seek|hiring|opening for|position for)[^.]{0,70}(?:PhD|doctoral)\s+(?:student|candidate|researcher)',text,re.I)
    if not doctoral(detail_title) and not doctoral(title) and completed and not recruiting:
        raise ValueError('non_doctoral_detail:'+detail_title+'; '+text[max(0,completed.start()-80):completed.end()+200])
    if not doctoral(detail_title) and not doctoral(title) and not re.search(r'(?:recruit|looking for|seek|hiring|opening for|position for)[^.]{0,70}(?:PhD|doctoral)\s+(?:student|candidate|researcher)',text,re.I):
        raise ValueError('uncertain_type:'+detail_title+'; '+(qualification_quote or text[:240]))
    if not student_offer and not (doctoral(detail_title) and re.search(r'\bmaster\b|\bmasters\b|\bMSc\b',text,re.I)):
        if re.search(r'(?:hold|completed|obtained|have|required)[^.]{0,45}\bph\.?d\b|postdoctoral',text,re.I):raise ValueError('non_doctoral_detail:'+detail_title+'; '+text[:240])
        raise ValueError('uncertain_type:'+detail_title+'; '+text[:240])
    lifecycle='open';lifecycle_note='官方博士招聘正文与申请入口；个人资格与接收另核。'
    for match in re.finditer(r'(position (?:has been|is) (?:filled|closed)|(?:this )?(?:ad|advertisement) (?:has )?expired)',text,re.I):
        if not re.search(r'\b(until|if|when|once|unless)\b',text[max(0,match.start()-90):match.start()],re.I):lifecycle='closed';lifecycle_note=match.group(0)
    deadlines=known_dates(html,item)
    deadline=min((r['date'] for r in deadlines if r['effect']=='hard_close'),default='')
    if deadline and date.fromisoformat(deadline)<timezone.localdate():lifecycle='closed';lifecycle_note='官方列表硬截止已过；保留博士类型与历史。'
    anchors=re.findall(r'<a[^>]+href=[\'"]([^\'"]+)[\'"][^>]*>(.*?)</a>',html,re.I|re.S)
    apply=[urljoin(item['url'],unescape(link)) for link,label in anchors if re.search(r'apply (?:here|for|online)|apply now|log\s?in and apply|jetzt online bewerben|ansök|solliciteer|logga in och sök',plain(label),re.I)]
    if not apply and lifecycle!='closed':raise ValueError('application_entry_unconfirmed')
    application=apply[0] if apply else item['url']
    mail_application=urlsplit(application).scheme=='mailto' and bool(re.search(r'cover letter|curriculum vitae|\bCV\b',text,re.I))
    if mail_application:application=item['url']
    if urlsplit(application).scheme!='https':raise ValueError('non_https_application')
    refs=list(dict.fromkeys(re.findall(r'Reference number\s+((?:[A-Z]+(?:-[A-Z]+)?\s+)?\d{4}/\d+|\d{5,})(?=\s|$)',text)))
    if len(refs)>1:raise ValueError('ambiguous_reference:multiple official references require individually scoped manual extraction')
    official=refs[0] if refs else item.get('official_id') or item['id']
    prefix=re.sub(r'^(Doctoral student|PhD student|PhD candidate|Doctoral Researcher|PhD)\s*(?:position)?\s*(?:in|on)?\s*','',title,flags=re.I)
    # A deterministic Chinese category summary; topic stays in its original language.
    label=title
    fieldmap=[(r'\b(computer|software|LLM|AI|machine learning|data science|informatics)\b','计算与信息'),(r'\b(control|robot\w*|electrical|communication\w*)\b','电气、控制与机器人'),(r'\b(physic\w*|quantum|astro\w*|photon\w*)\b','物理'),(r'\b(biology|medical|neuro\w*|surgery|biomechanic\w*)\b','生命与医学'),(r'\b(chem\w*|material\w*)\b','化学与材料'),(r'\b(supply|economic\w*|management|law)\b','管理与社会科学')]
    discipline=' / '.join(dict.fromkeys(z for pattern,z in fieldmap if re.search(pattern,title,re.I))) or '跨学科 / 原题核查'
    if re.search(r'Neuronics',title,re.I) and re.search(r'finite element|traffic|biomechanic',text,re.I):discipline='生命与医学 / 生物力学'
    paragraphs=[plain(p) for p in re.findall(r'<p\b[^>]*>(.*?)</p>',html,re.I|re.S)]
    project=[p for p in paragraphs if len(p)>100 and re.search(r'project|research|investigat|develop|focus|neuro|control|robot|finite element',p,re.I)]
    excerpt=' '.join(project[:3])[:2400]
    evidence=[{'field':'type','quote':title,'url':item['url'],'verified_at':stamp,'method':'public_employer_template_check'},
        {'field':'application','quote':'Apply entry present on the public employer advertisement','url':item['url'],'verified_at':stamp,'method':'public_employer_template_check'}]
    if mail_application:evidence[-1]['quote']='Official advertisement provides a mailto application with CV/cover-letter instructions; this HTTPS advertisement is the navigation entry. No message sent.'
    eligibility=qualifications(html)
    language=re.search(r'[^.!?]*(?:\bEnglish\b|\bB2\+|\bIELTS\b|\bTOEFL\b)[^.!?]*[.!?]?',eligibility,re.I)
    language_text=section_text(html,r'language(?: requirements?| proficiency)?|English(?: language)? requirements?') or (language.group(0).strip() if language else '')
    if not language_text:
        language_text=' '.join(p for p in paragraphs if len(p)>25 and re.search(r'\bIELTS\b|\bTOEFL\b|(?:proficiency|command|fluen\w*|level)[^.;]{0,45}\bEnglish\b|\bEnglish\b[^.;]{0,45}(?:proficiency|required|level|B2)',p,re.I))[:1200]
    result={'key':config['key']+'-'+item['id'],'source_key':config['key'],'type':'position','title':title,'title_zh':label[:350],
        'institution':config['institution'],'institution_url':config['institution_url'],'country':config['country'],'region':'欧洲','discipline':discipline,
        'url':item['url'],'application_url':application,'official_id':official,'fetched_at':stamp,'record_verified_at':stamp,
        'status':lifecycle,'status_note':lifecycle_note+('公告明确名额 '+str(item['seats'])+'；公告仍计1条。' if item.get('seats') else '名额未确证。'),
        'summary_zh':'主题中文待核；研究原题见原文。','eligibility':eligibility,'language_requirements':language_text,
        'review_method':'automated_extraction','translation_method':'待主题翻译；原文段落已限定提取','translation_status':'pending','evidence':evidence,'_detail_title':detail_title,'_project_quote':excerpt}
    if re.search(r'\bdoctoral fellowship\b',title,re.I) and (year:=re.search(r'\b20\d\d\b',title)):
        result.update(type='program_round',program_title=config['institution']+' Doctoral Fellowship',program_key=config['key']+'-doctoral-fellowship',round_key=year.group(),round_label=year.group())
    if config.get('parser')=='eth' and re.search(r'Work location\s*:[^.]{0,100}Singapore|The Singapore-ETH Centre was established',text,re.I):
        result.update(country='新加坡',region='亚洲',institution='Singapore-ETH Centre',institution_url='https://sec.ethz.ch/')
    elif '(TUM)' in title and 'TUM Campus Munich' in text:
        result.update(country='德国',institution='Technical University of Munich (TUM)',institution_url='https://www.tum.de/en/')
    if item.get('published'):result['source_published_at']=item['published']
    if deadlines:
        result['deadlines']=[{k:v for k,v in d.items() if k!='evidence_scope'} for d in deadlines]
        evidence.extend({'field':'deadline','quote':d['original'],'url':item['url'] if d['evidence_scope']=='official_detail' else config['url'],'verified_at':stamp,'method':d['evidence_scope']} for d in deadlines)
    else:result['deadline_mode']='unknown'
    for field,value in [('eligibility',eligibility),('language_requirements',language_text)]:
        if value:evidence.append({'field':field,'quote':value[:1600],'url':item['url'],'verified_at':stamp,'method':'official_detail'})
    salary=re.search(r'€\s*([\d,.]+)\s*(?:and|to|–|-|—)\s*(?:max\.\s*)?€?\s*([\d,.]+)\s+gross\s+(?:base\s+)?salary\s+per\s+month\s*\(full-time\)',text,re.I)
    if salary:
        low,high=(decimal_number(v,'en') for v in salary.groups())
        if 1000<=low<=high<=15000 and 'gross' in salary.group(0).lower():
            result.update(employment_type='employee',funding_status='funded')
            result['financials']=[{'kind':'salary','amount':str(low),'amount_max':str(high),'currency':'EUR','pay_period':'month','tax_basis':'gross','work_basis':'full_time',
                'currency_evidence':salary.group(0),'applicability_basis':'advertisement','applicability_scope':item['url'],'scope_observed_at':stamp,
                'comparability_verified':False,'verification_note':'待逐条审核本公告原币、税前、周期与全职依据。','note':'全职基本税前月薪；额外津贴另计，未冒填年度。'}]
            evidence.append({'field':'salary','quote':salary.group(0)[:180],'url':item['url'],'verified_at':stamp,'method':'public_employer_template_check'})
    else:
        monthly=re.search(r'(?:monthly salary|salary)[^.]{0,80}?(\d[\d ,.]*)\s*SEK|SEK\s*(\d[\d ,.]*)\s*(?:per month|/month)',text,re.I)
        if monthly:
            value=decimal_number(next(v for v in monthly.groups() if v),'en')
            result['financials']=[{'kind':'salary','amount':str(value),'currency':'SEK','pay_period':'month','tax_basis':'unknown','currency_evidence':monthly.group(0),'note':'原文明示月金额；税口径与工时尚未确证，不进入可比工资。'}]
            evidence.append({'field':'salary','quote':monthly.group(0)[:180],'url':item['url'],'verified_at':stamp,'method':'public_employer_template_check'})
    return result

def decimal_number(value,locale):
    value=value.strip().replace(' ','').replace('\u00a0','')
    if locale=='en':value=value.replace(',','')
    elif locale=='de':value=value.replace('.','').replace(',','.')
    else:raise ValueError('Unknown numeric locale; retain for review')
    if not re.fullmatch(r'\d+(?:\.\d{1,2})?',value):raise ValueError('Ambiguous salary number')
    return Decimal(value)


from .discovery_state import collect
