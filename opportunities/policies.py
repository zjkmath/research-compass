"""Explicit one-shot OIST visitor-policy recheck. No standing crawl permission."""
import re
import time
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser
from django.core.exceptions import ValidationError
from django.utils import timezone
from .ingest import bounded_read, USER_AGENT, digest

class PolicyText(HTMLParser):
    def __init__(self): super().__init__(); self.parts=[]; self.skip=0
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style'): self.skip+=1
    def handle_endtag(self,tag):
        if tag in ('script','style'): self.skip=max(0,self.skip-1)
    def handle_data(self,data):
        if not self.skip: self.parts.append(data)

def parse_oist(raw, key, url, stamp):
    parser=PolicyText(); parser.feed(raw.decode('utf8',errors='strict'))
    text=' '.join(' '.join(parser.parts).split())
    sections=list(re.finditer(r'3\s+Visiting Research Students\b(.*?)(?=4\s+Credit Seeking Students\b)',text,re.S))
    if not sections: raise ValueError('正式VRS章节边界改变；停止解析，人工复核')
    section=sections[-1].group(1)
    if re.search(r'\b(?:not eligible|ineligible|no longer|may not|not permitted|not approved)\b',section,re.I):
        raise ValueError('资格或支持否定条款变化；停止旧规则解析，进入人工复核队列')
    required=['students enrolled for higher degrees at other institutions','Visiting Research Student Agreement',
        'approved by the Dean','one (1) calendar year','in general bring their own funding','Copy of Student ID Card']
    if not all(phrase.casefold() in section.casefold() for phrase in required):
        raise ValueError('正式身份/期限/资金条款已变化或缺失；不生成猜测字段')
    witness='students enrolled for higher degrees at other institutions'
    patch={'policy_content_version':digest(section),'requirements':[
        {'key':'enrolled','value':True,'label':'在其他机构正式在读','evidence_url':url,'quote':witness},
        {'key':'higher_degree','value':True,'label':'在其他机构攻读高等学位','evidence_url':url,'quote':witness},
        {'key':'agreement_ready','value':True,'label':'正式访问研究学生协议须在开始前完成','evidence_url':url,'quote':'Visiting Research Student Agreement'},
        {'key':'host_support','value':True,'label':'已有OIST导师支持并准备正式研究协议','evidence_url':url,'quote':'Visiting Research Student Agreement'}],
        'max_months':12,'renewal_note':'超过1日历年须Dean批准续期，另行核查；不当普通一期自动符合',
        'funding_type':'external','cash_facts':[], 'receiving_status':'unknown',
        'materials':['访问研究学生申请表','访问研究学生协议','OIST导师研究安排与计划','CV','照片','本校学生证'],
        'next_action':'先请拟接收导师确认研究项目与支持，再准备VRS协议交Graduate School核查',
        'funding_note':'通常自带经费；组可酌情支持旅费、住宿与补助，金额及承诺未说明，不计现金收入。',
        'review_period_days':90}
    quote='students enrolled for higher degrees at other institutions; Visiting Research Student Agreement; one (1) calendar year'
    return {'key':key,'details_patch':patch,'evidence':[{'field':'formal_visit_policy','url':url,'quote':quote,
        'verified_at':stamp,'method':'codex_assisted_live_policy_review'}]}

def read_oist(source, manual_read=False):
    scope=source.scope
    if not manual_read or scope.get('mode')!='manual_one_shot' or source.automation_allowed:
        raise ValueError('本来源仅允许明确启动的一次人工复核，不具备循环自动采集许可')
    if not source.authorizations.filter(allowed=True,scope=scope).exists(): raise ValueError('一次读取范围未核准')
    if not source.policy_checked_at or timezone.now()>=source.policy_checked_at+timedelta(days=source.review_period_days):
        raise ValueError('来源读取依据到期，须重新核查')
    host=urlsplit(source.url).hostname
    if host not in scope['allowed_hosts'] or urlsplit(source.url).path not in scope['allowed_paths'] or urlsplit(source.url).query:
        raise ValueError('地址超出准确路径范围')
    if source.last_attempt_at and (timezone.now()-source.last_attempt_at).total_seconds()<source.rate_seconds:
        raise ValueError('限速间隔未到；本次停止')
    if source.retry_after_at and timezone.now()<source.retry_after_at: raise ValueError('Retry-After冷却中')
    source.last_attempt_at=timezone.now(); source.save(update_fields=['last_attempt_at'])
    robot=RobotFileParser(); robot.parse(bounded_read(source.robots_url,host,['/robots.txt']).decode('utf8').splitlines())
    if not robot.can_fetch(USER_AGENT,source.url): raise ValueError('robots禁止正文访问，未请求正文')
    time.sleep(max(source.rate_seconds,robot.crawl_delay(USER_AGENT) or 0))
    raw=bounded_read(source.url,host,scope['allowed_paths'])
    stamp=timezone.now().isoformat()
    payload=parse_oist(raw,scope['path_key'],source.url,stamp)
    # The page declares no ordered source version; content ID is opaque, never a publication timestamp.
    revision=payload['details_patch']['policy_content_version']
    return payload,revision,{'url':source.url,'bytes':len(raw),'fingerprint':digest(raw.decode('utf8')),
        'fetched_at':stamp,'purpose':'managed_visit_path_recheck','retained':'结构化必要事实与短证据；未保留整页'}
