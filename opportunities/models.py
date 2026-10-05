from datetime import datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator, RegexValidator, URLValidator
from django.db import models
from django.utils import timezone

unknown = '未知 / 原文未说明'
safe_url = URLValidator(schemes=['https', 'http'])


class DiscoveryPolicy(models.Model):
    """Per-instance immutable grandfathering boundary; never exported in public seeds."""
    key = models.SlugField(unique=True)
    legacy = models.JSONField(default=dict)
    registry_fingerprint = models.CharField(max_length=64)
    activated_at = models.DateTimeField()


class Source(models.Model):
    key = models.SlugField(unique=True)
    name = models.CharField(max_length=200)
    url = models.URLField(max_length=1000, validators=[safe_url])
    region = models.CharField(max_length=40)
    access_method = models.CharField(max_length=20, choices=[('manual', '人工审核'), ('feed', '正式 Feed/API')], default='manual')
    terms_url = models.URLField(max_length=1000, blank=True, validators=[safe_url])
    robots_url = models.URLField(max_length=1000, blank=True, validators=[safe_url])
    policy_note = models.TextField(blank=True)
    automation_allowed = models.BooleanField(default=False)
    policy_checked_at = models.DateTimeField(null=True, blank=True)
    rate_seconds = models.PositiveIntegerField(default=10, validators=[MinValueValidator(2), MaxValueValidator(3600)])
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=500, blank=True)
    retry_after_at = models.DateTimeField(null=True, blank=True)
    adapter = models.CharField(max_length=40, blank=True)
    scope = models.JSONField(default=dict, blank=True)
    url_rules = models.JSONField(default=dict, blank=True)
    review_period_days = models.PositiveIntegerField(default=30)

    def __str__(self):
        return self.name


class CoverageInstitution(models.Model):
    """An explicit ranking universe and bounded official-entry review, not all vacancies."""
    AUDIT_SCOPE_CHOICES = [
        ('relevant_found_via_declared_qs_audit', 'QS审计入口已发现'),
        ('bounded_entry_checked_no_extracted_opportunity', 'QS审计有限入口 · 尚未抽取'),
        ('verified_none_in_declared_scope', '声明范围反查后未发现'),
        ('blocked', 'QS审计入口受限'), ('pending_review', 'QS审计待复核')]
    GLOBAL_PRESENCE_CHOICES = [
        ('known_relevant_opportunity_exists', '全局已收录精选相关机会'),
        ('none_verified_in_declared_scope', '仅声明范围内未发现'),
        ('unknown', '全局存在情况未知')]
    key = models.SlugField(max_length=180, unique=True)
    official_name = models.CharField(max_length=250)
    qs_rank = models.PositiveIntegerField()
    ranking_year = models.PositiveIntegerField(default=2027)
    country = models.CharField(max_length=80)
    relevant_departments = models.JSONField(default=list)
    entries = models.JSONField(default=list)
    status = models.CharField('QS审计入口状态', max_length=64, default='pending_review', choices=[
        ('relevant_opportunity_found', 'QS审计入口已发现'),
        ('bounded_entry_checked_no_extracted_opportunity', 'QS审计有限入口 · 尚未抽取'),
        ('verified_none_in_declared_scope', '声明范围反查后未发现'),
        ('blocked', 'QS审计入口受限'), ('pending_review', 'QS审计待复核')])
    review = models.JSONField(default=dict, blank=True)
    evidence = models.JSONField(default=list)
    last_checked_at = models.DateTimeField(null=True, blank=True)

    @property
    def audit_scope_status(self):
        # Keep the legacy column/fixtures and their audit history intact.
        return 'relevant_found_via_declared_qs_audit' if self.status == 'relevant_opportunity_found' else self.status

    @property
    def known_relevant_opportunity_count(self):
        if hasattr(self, '_global_opportunity_count'):
            return self._global_opportunity_count
        from .curation import coverage_presence_counts
        return coverage_presence_counts().get(self.pk, 0)

    @property
    def global_relevant_opportunity_presence(self):
        # Derive from the current reviewed pool, never a stale imported total.
        if self.known_relevant_opportunity_count:
            return 'known_relevant_opportunity_exists'
        return 'none_verified_in_declared_scope' if self.status == 'verified_none_in_declared_scope' else 'unknown'

    def get_global_presence_display(self):
        return dict(self.GLOBAL_PRESENCE_CHOICES)[self.global_relevant_opportunity_presence]

    class Meta:
        ordering = ['qs_rank', 'official_name']


class Organization(models.Model):
    key = models.SlugField(unique=True)
    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=[('university', '大学 / 科研机构'), ('lab', '课题组')], default='university')
    country = models.CharField(max_length=60, blank=True)
    url = models.URLField(max_length=1000, blank=True, validators=[safe_url])
    parent = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True)
    note = models.TextField(blank=True)
    external_metadata = models.JSONField(default=dict, blank=True)
    coverage = models.ForeignKey(CoverageInstitution, null=True, blank=True, on_delete=models.PROTECT)

    def clean(self):
        from .priority20 import guard
        guard(type(self), self)

    def __str__(self):
        return self.name


class Person(models.Model):
    name = models.CharField(max_length=200)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    url = models.URLField(max_length=1000, validators=[safe_url])
    evidence = models.TextField(blank=True)
    verified_at = models.DateTimeField()
    external_metadata = models.JSONField(default=dict, blank=True)

    def clean(self):
        from .priority20 import guard
        guard(type(self), self)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['name', 'organization'], name='unique_person_in_org')]

    def __str__(self):
        return self.name


class Program(models.Model):
    key = models.SlugField(unique=True)
    title = models.CharField(max_length=250)
    institution = models.ForeignKey(Organization, on_delete=models.PROTECT)
    url = models.URLField(max_length=1000, validators=[safe_url])
    mentors = models.ManyToManyField(Person, blank=True)

    def __str__(self):
        return self.title


class Opportunity(models.Model):
    TYPES = [('position', '博士招聘岗位'), ('program_round', '博士项目申请轮次'),
             ('scholarship', '奖学金 / 资助'), ('postdoc', '博士后'), ('visiting', '正式访问机会'),
             ('research_funding', '机构科研资助'), ('doctoral_notice', '实验室博士招募意向（须核录取与轮次）'),
             ('doctoral_program', '博士项目路径（轮次未确认）'), ('visiting_route', '公开访问路径（名额未确认）')]
    STATUSES = [('open', '开放申请'), ('upcoming', '未来轮次'), ('unknown', '状态待核验'), ('closed', '已关闭')]
    key = models.SlugField(unique=True)
    type = models.CharField(max_length=20, choices=TYPES)
    identity = models.CharField(max_length=64, unique=True, blank=True)
    title = models.CharField(max_length=350)
    title_zh = models.CharField(max_length=350)
    country = models.CharField(max_length=60)
    region = models.CharField(max_length=40)
    discipline = models.CharField(max_length=80)
    institution = models.ForeignKey(Organization, on_delete=models.PROTECT)
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    url = models.URLField(max_length=1000, validators=[safe_url])
    application_url = models.URLField(max_length=1000, blank=True, validators=[safe_url])
    program = models.ForeignKey(Program, on_delete=models.PROTECT, null=True, blank=True)
    round_key = models.CharField(max_length=150, blank=True)
    round_label = models.CharField(max_length=200, blank=True)
    related_funding = models.ManyToManyField('self', symmetrical=False, blank=True,
        limit_choices_to={'type': 'scholarship'}, related_name='funded_opportunities')
    mentors = models.ManyToManyField(Person, blank=True)
    status = models.CharField(max_length=20, choices=STATUSES, default='unknown')
    status_note = models.TextField(blank=True)
    review_status = models.CharField(max_length=20, choices=[('pending', '待审核'), ('verified', '已审核')], default='pending')
    is_test = models.BooleanField(default=False)
    is_published = models.BooleanField(default=False)
    summary_zh = models.TextField(blank=True)
    eligibility = models.TextField(blank=True)
    language_requirements = models.TextField(blank=True)
    employment_type = models.CharField(max_length=20, choices=[('unknown', unknown), ('employee', '雇佣'),
        ('student', '学生身份'), ('scholarship', '奖学金身份')], default='unknown')
    funding_status = models.CharField(max_length=20, choices=[('unknown', '资助待核验'), ('funded', '有资助'),
        ('partial', '部分资助'), ('self_funded', '自费')], default='unknown')
    funding_source = models.TextField(blank=True)
    tuition_waiver = models.TextField(blank=True)
    work_fraction = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(0), MaxValueValidator(100)])
    payment_schedule = models.TextField(blank=True)
    translation_method = models.CharField(max_length=60, default='人工审核式中文整理；模型 API 未配置')
    fingerprint = models.CharField(max_length=64, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    official_id = models.CharField(max_length=180, blank=True)
    identity_discriminator = models.CharField(max_length=180, blank=True)
    deadline_mode = models.CharField(max_length=20, default='unknown', choices=[
        ('fixed', '固定截止'), ('rolling', '全年 / 滚动受理'), ('not_applicable', '不适用'), ('unknown', '截止待核验')])
    fetched_at = models.DateTimeField(null=True, blank=True)
    source_published_at = models.DateTimeField(null=True, blank=True)
    translated_at = models.DateTimeField(null=True, blank=True)
    translation_status = models.CharField(max_length=20, default='ready', choices=[
        ('ready', '中文已整理'), ('stale', '中文及判断待更新'), ('pending', '中文待审核')])
    pending_change = models.BooleanField(default=False)
    review_method = models.CharField(max_length=40, default='codex_assisted_review')
    curation = models.JSONField(default=dict, blank=True)

    IDENTITY_FIT = [('directly_applicable','当前博士身份适用'),('potentially_applicable','可能适用 · 须核身份'),
        ('future_only','后续阶段 / 新学位申请'),('not_applicable','当前身份不适用'),('unknown','身份适配未知')]
    identity_fit = models.CharField(max_length=30,choices=IDENTITY_FIT,default='unknown',db_index=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['program', 'round_key'],
            condition=models.Q(type='program_round'), name='unique_program_round')]
        ordering = ['-verified_at', 'pk']

    def get_type_display(self):
        if self.type == 'doctoral_notice' and self.curation.get('source_review',{}).get('project_options'):
            return '博士资助项目公告'
        return dict(self.TYPES).get(self.type,self.type)

    def clean(self):
        if self.identity_discriminator and self.identity_discriminator != self.official_id:
            raise ValidationError('共享公告页的身份区分必须等于有来源的官方公告编号。')
        if self.type == 'program_round' and (not self.program_id or not self.round_key):
            raise ValidationError('项目申请轮次必须关联项目并有独立轮次标识。')
        if self.type != 'program_round' and self.round_key:
            raise ValidationError('非项目轮次记录不能带项目轮次标识。')
        from .ingest import opportunity_identity
        self.identity = opportunity_identity(self)
        from .priority20 import guard
        guard(type(self), self)

    @property
    def effective_status(self):
        if self.status != 'closed':
            for deadline in self.deadlines.all():
                if deadline.effect == 'hard_close' and (deadline.kind == 'application' or self.type in ('scholarship', 'research_funding') and deadline.kind == 'funding') and deadline.expired:
                    return 'closed'
        if self.status == 'upcoming':
            openings = [d for d in self.deadlines.all() if d.effect == 'opening' and d.status == 'verified' and d.date]
            if openings and all(d.expired for d in openings):
                return 'unknown'  # An elapsed opening date alone does not confirm current eligibility/open status.
        return self.status

    @property
    def status_label(self):
        if self.pending_change: return '有变化待审核'
        return dict(self.STATUSES)[self.effective_status]

    @property
    def primary_deadline(self):
        dates = [d for d in self.deadlines.all() if d.status == 'verified' and d.date and d.effect == 'hard_close' and
                 (d.kind == 'application' or self.type in ('scholarship', 'research_funding') and d.kind == 'funding')]
        return min(dates, key=lambda d: d.date) if dates else None

    @property
    def next_action(self):
        dates = [d for d in self.deadlines.all() if d.status == 'verified' and d.date and
                 d.effect in ('prerequisite', 'priority', 'hard_close') and not d.expired]
        return min(dates, key=lambda d: d.boundary) if dates else None

    @property
    def prerequisite_warning(self):
        return [d for d in self.deadlines.all() if d.effect == 'prerequisite' and d.expired]

    @property
    def cash_facts(self):
        return [f for f in self.financials.all() if f.kind in ('salary', 'stipend')]

    def __str__(self):
        return self.title_zh


class Deadline(models.Model):
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name='deadlines')
    kind = models.CharField(max_length=30, choices=[('application', '申请截止'), ('funding', '资助截止'),
        ('consent', '导师同意截止'), ('opening', '申请开放'), ('document', '材料截止'), ('other', '其他')], default='application')
    label = models.CharField(max_length=150)
    date = models.DateField(null=True, blank=True)
    time = models.TimeField(null=True, blank=True)
    timezone = models.CharField(max_length=80, blank=True)
    original = models.TextField(blank=True)
    status = models.CharField(max_length=20, choices=[('verified', '已核验'), ('unknown', '待核验')], default='unknown')
    effect = models.CharField(max_length=20, default='hard_close', choices=[('hard_close', '最终硬截止'),
        ('priority', '优先 / 充分考虑'), ('prerequisite', '前置步骤'), ('opening', '开放起日'),
        ('rolling', '滚动受理'), ('informational','信息事件 / 程序'), ('unknown', '性质待核验')])
    precision = models.CharField(max_length=20, default='date', choices=[('date', '日期精度'),
        ('time', '时刻精度'), ('unknown', '精度待核验')])
    step_id = models.CharField(max_length=180, blank=True)
    requirement_version = models.CharField(max_length=64, blank=True)

    @property
    def completion_key(self):
        from .ingest import digest
        version = self.requirement_version or digest([self.kind, str(self.date), str(self.time), self.timezone, self.effect])[:16]
        return f'step:{self.step_id or self.kind}:{version}'

    def clean(self):
        if self.timezone:
            try:
                ZoneInfo(self.timezone)
            except ZoneInfoNotFoundError:
                raise ValidationError({'timezone': '必须为有效 IANA 时区。'})
        if self.time and not self.date:
            raise ValidationError('有时间必须有日期。')

    @property
    def boundary(self):
        if not self.date:
            return None
        if self.time and self.timezone:
            return datetime.combine(self.date, self.time, ZoneInfo(self.timezone))
        elif self.timezone:
            return datetime.combine(self.date + timedelta(days=1), time.min, ZoneInfo(self.timezone))
        else:
            # Unknown zone: conservative outer bound, not an invented official time.
            return datetime.combine(self.date + timedelta(days=1), time(12), dt_timezone.utc)

    @property
    def expired(self):
        return bool(self.status == 'verified' and self.boundary and timezone.now() >= self.boundary)


class FinancialFact(models.Model):
    KINDS = [('salary', '个人薪酬'), ('stipend', '个人津贴'), ('tuition_waiver', '学费减免'),
             ('tuition_fee', '学费'), ('application_fee', '申请费'), ('total_grant', '资助总额'), ('other', '其他费用 / 资助')]
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name='financials')
    kind = models.CharField(max_length=30, choices=KINDS)
    amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    amount_max = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True, validators=[MinValueValidator(0)])
    currency = models.CharField(max_length=3, blank=True, validators=[RegexValidator(r'^[A-Z]{3}$', '使用 ISO 三字母币种')])
    pay_period = models.CharField(max_length=20, choices=[('month', '月'), ('year', '年'), ('week', '周'),
        ('total', '总计'), ('once', '一次性'), ('unknown', '周期未说明')], default='unknown')
    tax_basis = models.CharField(max_length=20, choices=[('gross', '税前'), ('net', '税后'), ('exempt', '明确免税'), ('unknown', '税口径未说明')], default='unknown')
    note = models.TextField(blank=True)
    applicable_year = models.CharField(max_length=200, blank=True)
    applicability_basis = models.CharField(max_length=20, default='year', choices=[('year','官方年度'),('advertisement','本公告现行报价')])
    applicability_scope = models.URLField(max_length=1000, blank=True, validators=[safe_url])
    scope_observed_at = models.DateTimeField(null=True, blank=True)
    work_basis = models.CharField(max_length=20, default='unknown', choices=[('actual', '实际已折算工时'),
        ('full_time', '全职基准'), ('not_applicable', '津贴非雇佣工时'), ('unknown', '工时口径未知')])
    payments_per_year = models.PositiveIntegerField(null=True, blank=True)
    conditional_amount = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    condition = models.TextField(blank=True)
    comparability_verified = models.BooleanField(default=False)
    currency_evidence = models.TextField(blank=True)
    verification_note = models.TextField(blank=True)

    def clean(self):
        if self.amount is not None and not self.currency and not self.note:
            raise ValidationError('币种未说明的数值必须有原文事实及币种待核验备注。')
        if self.amount_max is not None and (self.amount is None or self.amount_max < self.amount):
            raise ValidationError('金额上限必须不小于下限。')


class Evidence(models.Model):
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE, related_name='evidence')
    field = models.CharField(max_length=100)
    quote = models.TextField()
    url = models.URLField(max_length=1000, validators=[safe_url])
    verified_at = models.DateTimeField()
    method = models.CharField(max_length=30, default='manual_review')

    @property
    def field_label(self):
        return {'status':'招聘状态','type':'机会类型','application':'申请入口','deadline':'期限',
            'eligibility':'资格条件','eligibility_language':'资格与语言','language':'语言要求',
            'funding':'资金依据','salary':'工资依据','project':'项目内容','research':'研究内容',
            'research_connection':'研究连接','scope':'适用范围','location':'实际地点'}.get(self.field,'字段原文')


class UserRecord(models.Model):
    STAGES = [('none', '尚未开始'), ('preparing', '准备材料'), ('submitted', '已提交'),
              ('interview', '面试中'), ('offer', '获得录取'), ('rejected', '未录取'), ('withdrawn', '已撤回')]
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    opportunity = models.ForeignKey(Opportunity, on_delete=models.CASCADE)
    saved = models.BooleanField(default=False)
    stage = models.CharField(max_length=20, choices=STAGES, default='none')
    note = models.TextField(blank=True, max_length=3000)
    updated_at = models.DateTimeField(auto_now=True)
    completed_steps = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'opportunity'], name='unique_user_opportunity')]


class ImportRun(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    method = models.CharField(max_length=40)
    input_name = models.CharField(max_length=250)
    fingerprint = models.CharField(max_length=64)
    created = models.PositiveIntegerField(default=0)
    updated = models.PositiveIntegerField(default=0)
    unchanged = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)


class SourceAuthorization(models.Model):
    source = models.ForeignKey(Source, on_delete=models.PROTECT, related_name='authorizations')
    allowed = models.BooleanField(default=False)
    scope = models.JSONField(default=dict)
    policy_url = models.URLField(max_length=1000, validators=[safe_url])
    evidence = models.TextField()
    method = models.CharField(max_length=40, default='documented_policy_review')
    created_at = models.DateTimeField(auto_now_add=True)


class VisitPath(models.Model):
    key = models.SlugField(max_length=180, unique=True)
    institution = models.ForeignKey(Organization, on_delete=models.PROTECT)
    name = models.CharField(max_length=300)
    url = models.URLField(max_length=1000, validators=[safe_url])
    identity_types = models.JSONField(default=list)
    details = models.JSONField(default=dict)
    evidence = models.JSONField(default=list)
    verified_at = models.DateTimeField()
    is_published = models.BooleanField(default=False)


class ResearchTarget(models.Model):
    RECEIVING = [('open', '明确开放招募'), ('institutional_path', '制度存在 · 名额未知'),
        ('external_funding', '明确欢迎特定外部资助'), ('historical', '历史接收证据'),
        ('unknown', '接收未确认'), ('closed', '明确关闭')]
    ROLE_CLASSES = [('independent_pi','独立 PI'),('faculty_supervisor','教师 / 研究指导者'),
        ('group_leader','课题组负责人'),('independent_fellow','独立研究会士'),('research_staff','研究人员'),
        ('postdoc','博士后'),('emeritus_retired','荣休 / 退休'),('former','曾任'),('unknown','角色尚未确认')]
    HOST_CAPABILITIES = [('confirmed_by_official_policy_or_profile','正式指导 / Host能力有据'),
        ('plausible_but_unverified','具备可能性 · 尚未确认'),('restricted','Host能力受限'),
        ('not_applicable','不适用'),('unknown','Host能力未知')]
    CONTACT_RECEIVING = [('explicitly_open','明确开放接收'),('inquiries_welcome','明确欢迎咨询'),
        ('current_program_open','相关项目当前开放'),('unknown_contact_to_confirm','当前接收待联系确认'),
        ('not_receiving','明确不接收'),('not_applicable','不适用')]
    ACTIONABILITIES = [('contact_candidate','精选可联系导师'),('research_watchlist','研究观察名单'),
        ('reference_only','参考科研人员')]
    key = models.SlugField(max_length=180, unique=True)
    institution = models.ForeignKey(Organization, on_delete=models.PROTECT)
    group = models.ForeignKey(Organization, on_delete=models.PROTECT, related_name='research_targets')
    mentors = models.ManyToManyField(Person)
    paths = models.ManyToManyField(VisitPath)
    region = models.CharField(max_length=40)
    country = models.CharField(max_length=60)
    themes = models.JSONField(default=list)
    url = models.URLField(max_length=1000, validators=[safe_url])
    receiving_status = models.CharField(max_length=30, choices=RECEIVING, default='unknown')
    eligibility_status = models.CharField(max_length=20, default='unknown')
    funding_type = models.CharField(max_length=20, default='unknown')
    match_category = models.CharField(max_length=20, default='unknown')
    details = models.JSONField(default=dict)
    evidence = models.JSONField(default=list)
    verified_at = models.DateTimeField()
    pending_change = models.BooleanField(default=False)
    is_published = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)
    curation = models.JSONField(default=dict, blank=True)
    role_class = models.CharField(max_length=30,choices=ROLE_CLASSES,default='unknown',db_index=True)
    host_capability = models.CharField(max_length=50,choices=HOST_CAPABILITIES,default='unknown')
    contact_receiving_status = models.CharField(max_length=35,choices=CONTACT_RECEIVING,default='unknown_contact_to_confirm')
    actionability = models.CharField(max_length=30,choices=ACTIONABILITIES,default='research_watchlist',db_index=True)
    classification_evidence = models.JSONField(default=dict,blank=True)

    def clean(self):
        from .priority20 import guard
        guard(type(self), self)

    @property
    def effective_actionability(self):
        if self.actionability=='contact_candidate' and (self.pending_change or self.curation.get('review_state')=='source_changed'
                or self.classification_evidence.get('status')=='source_changed'):
            return 'research_watchlist'
        return self.actionability

    def __str__(self):
        return self.group.name


class TargetRecord(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    target = models.ForeignKey(ResearchTarget, on_delete=models.PROTECT)
    saved = models.BooleanField(default=True)
    note = models.TextField(blank=True, max_length=3000)
    stage = models.CharField(max_length=30, default='researching')
    completed_steps = models.JSONField(default=list)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'target'], name='unique_user_target')]


class SavedFilter(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    name = models.CharField(max_length=80)
    path = models.CharField(max_length=100, default='/targets/')
    query = models.JSONField(default=dict)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'name'], name='unique_user_filter')]


class LocalProfile(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    data = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)


class SourceAlias(models.Model):
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    record_id = models.CharField(max_length=220)
    original_url = models.URLField(max_length=1000, blank=True, validators=[safe_url])
    opportunity = models.ForeignKey(Opportunity, null=True, blank=True, on_delete=models.PROTECT)
    person = models.ForeignKey(Person, null=True, blank=True, on_delete=models.PROTECT)
    organization = models.ForeignKey(Organization, null=True, blank=True, on_delete=models.PROTECT)
    target = models.ForeignKey(ResearchTarget, null=True, blank=True, on_delete=models.PROTECT)
    path = models.ForeignKey(VisitPath, null=True, blank=True, on_delete=models.PROTECT)
    checked_at = models.DateTimeField(null=True, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['source', 'record_id'], name='unique_source_record')]

    def clean(self):
        if sum(bool(getattr(self, field + '_id')) for field in ('opportunity', 'person', 'organization', 'target', 'path')) != 1:
            raise ValidationError('来源别名必须关联一个明确实体。')


class EntityRevision(models.Model):
    entity_type = models.CharField(max_length=30)
    entity_key = models.CharField(max_length=220)
    sequence = models.PositiveIntegerField()
    old = models.JSONField(default=dict)
    new = models.JSONField(default=dict)
    source = models.ForeignKey(Source, null=True, blank=True, on_delete=models.PROTECT)
    source_record_id = models.CharField(max_length=220, blank=True)
    source_revision = models.CharField(max_length=100, blank=True)
    observed_at = models.DateTimeField()
    applied_at = models.DateTimeField(auto_now_add=True)
    reason = models.TextField()
    application_version = models.CharField(max_length=40, default='G2-20261001')
    class Meta:
        constraints = [models.UniqueConstraint(fields=['entity_type', 'entity_key', 'sequence'], name='unique_entity_revision')]


class UpdateRun(models.Model):
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    started_at = models.DateTimeField(auto_now_add=True)
    fetched_at = models.DateTimeField(null=True, blank=True)
    url = models.URLField(max_length=1000, validators=[safe_url])
    fingerprint = models.CharField(max_length=64, blank=True)
    method = models.CharField(max_length=40, default='live_api')
    status = models.CharField(max_length=30, default='started')
    parsed = models.PositiveIntegerField(default=0)
    proposed = models.PositiveIntegerField(default=0)
    unchanged = models.PositiveIntegerField(default=0)
    applied = models.PositiveIntegerField(default=0)
    error = models.TextField(blank=True)


class UpdateProposal(models.Model):
    run = models.ForeignKey(UpdateRun, on_delete=models.PROTECT, related_name='proposals')
    source = models.ForeignKey(Source, on_delete=models.PROTECT)
    record_id = models.CharField(max_length=220)
    kind = models.CharField(max_length=40)
    payload = models.JSONField()
    diff = models.JSONField(default=dict)
    fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=30, default='pending', choices=[('pending', '待审核'),
        ('applied', '已应用'), ('conflict', '冲突'), ('rejected', '已拒绝')])
    observed_at = models.DateTimeField()
    source_revision = models.CharField(max_length=100, blank=True)
    applied_at = models.DateTimeField(null=True, blank=True)
    review_note = models.TextField(blank=True)
    base_fingerprint = models.CharField(max_length=64, blank=True)
    revision_type = models.CharField(max_length=20, default='opaque')
    applied_entity_fingerprint = models.CharField(max_length=64, blank=True)


class DecisionRecord(models.Model):
    """Versioned private comparison, budget or contact preparation; never public data."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    target = models.ForeignKey(ResearchTarget, null=True, blank=True, on_delete=models.PROTECT)
    path = models.ForeignKey(VisitPath, null=True, blank=True, on_delete=models.PROTECT)
    kind = models.CharField(max_length=20, choices=[('comparison', '目标比较'), ('budget', '预算方案'), ('contact', '联系准备')])
    inputs = models.JSONField(default=dict)
    result = models.JSONField(default=dict)
    versions = models.JSONField(default=dict)
    public_paths = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self,*args,**kwargs):
        if self._state.adding and not self.public_paths:
            choices=self.inputs.get('choices',[]) if self.kind=='comparison' else [{'target':self.target_id,'path':self.path_id}]
            from django.utils import timezone
            for choice in choices:
                target=ResearchTarget.objects.filter(pk=choice['target'],is_published=True).first()
                path=target.paths.filter(pk=choice['path'],is_published=True).first() if target else None
                if path: self.public_paths.append({'target':target.pk,'path':path.pk,'name':path.name,'url':path.url,'published_checked_at':timezone.now().isoformat()})
        super().save(*args,**kwargs)


class PathSupport(models.Model):
    """Private confirmation for one host, formal route and application round."""
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    target = models.ForeignKey(ResearchTarget, on_delete=models.PROTECT)
    path = models.ForeignKey(VisitPath, on_delete=models.PROTECT)
    host_support = models.BooleanField(null=True, blank=True)
    agreement_ready = models.BooleanField(null=True, blank=True)
    evidence_note = models.TextField(blank=True)
    valid_until = models.DateField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['user', 'target', 'path'], name='unique_user_target_path_support')]


class FundingRelation(models.Model):
    MODES = [('independent', '独立申请'), ('same_application', '同一申请内选择'),
        ('automatic_consideration', '自动考虑'), ('nomination', '提名申请'), ('unknown', '申请关系待核验')]
    opportunity = models.ForeignKey(Opportunity, on_delete=models.PROTECT, related_name='funding_relations')
    award = models.ForeignKey(Opportunity, on_delete=models.PROTECT, related_name='award_relations')
    mode = models.CharField(max_length=30, choices=MODES, default='unknown')
    conditions = models.TextField(blank=True)
    evidence = models.JSONField(default=list, blank=True)
    exclusive_group = models.CharField(max_length=100, blank=True)
    class Meta:
        constraints = [models.UniqueConstraint(fields=['opportunity', 'award'], name='unique_funding_relation')]

    def clean(self):
        if self.mode != 'unknown' and not self.evidence:
            raise ValidationError('已确认的申请关系必须保留正式来源证据')


class ExchangeSnapshot(models.Model):
    date = models.DateField()
    base = models.CharField(max_length=3, default='EUR')
    rates = models.JSONField()
    url = models.URLField(max_length=1000, validators=[safe_url])
    version = models.CharField(max_length=64, unique=True)
    fetched_at = models.DateTimeField()

