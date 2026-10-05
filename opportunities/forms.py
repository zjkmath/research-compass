from django import forms
from django.contrib.auth.forms import UserCreationForm
from .models import UserRecord, TargetRecord, CoverageInstitution, ResearchTarget, Opportunity
from .curation import REASONS, POOLS, FIT_TIERS, DOSSIER_FIELDS

class RegistrationForm(UserCreationForm):
    pass

class RecordForm(forms.ModelForm):
    completed_steps = forms.MultipleChoiceField(required=False, label='已完成前置步骤', widget=forms.CheckboxSelectMultiple)
    class Meta:
        model = UserRecord
        fields = ['stage', 'note', 'completed_steps']
        labels = {'stage': '申请状态', 'note': '私人备注'}
        widgets = {'note': forms.Textarea(attrs={'rows': 3})}
    def __init__(self, *args, steps=(), **kwargs):
        super().__init__(*args, **kwargs)
        known = {value for value, label in steps}
        legacy = [(value, '旧完成记录保留 · 要求变更后须重新确认') for value in self.instance.completed_steps if value not in known] if self.instance.pk else []
        self.fields['completed_steps'].choices = list(steps) + legacy

class TargetRecordForm(forms.ModelForm):
    completed_steps = forms.MultipleChoiceField(required=False, label='已完成的路径步骤', widget=forms.CheckboxSelectMultiple)
    stage = forms.ChoiceField(label='跟进状态', choices=[('researching','研究中'),('eligibility','核查身份与制度'),
        ('contact_ready','准备联系'),('contacted','已自行联系'),('invited','已获邀请'),('paused','暂缓')])
    class Meta:
        model = TargetRecord
        fields = ['stage', 'note', 'completed_steps']
        labels = {'note': '私人备注'}
        widgets = {'note': forms.Textarea(attrs={'rows': 3})}
    def __init__(self,*args,steps=(),**kwargs):
        super().__init__(*args,**kwargs)
        known={v for v,label in steps}
        legacy=[(v,'旧完成记录保留 · 要求变化后须重新核对') for v in self.instance.completed_steps if v not in known] if self.instance.pk else []
        self.fields['completed_steps'].choices=list(steps)+legacy

class FilterForm(forms.Form):
    layer = forms.ChoiceField(required=False,label='导师资料层',choices=[('','精选可联系导师'),('contact','精选可联系导师'),
        ('watchlist','研究观察名单'),('reference','参考科研人员'),('coverage','相关科研人员覆盖库'),('deep','重点深档')])
    role_class = forms.MultipleChoiceField(required=False,label='当前角色',choices=ResearchTarget.ROLE_CLASSES,widget=forms.CheckboxSelectMultiple)
    host_capability = forms.MultipleChoiceField(required=False,label='Host能力',choices=ResearchTarget.HOST_CAPABILITIES,widget=forms.CheckboxSelectMultiple)
    contact_receiving = forms.MultipleChoiceField(required=False,label='当前接收证据',choices=ResearchTarget.CONTACT_RECEIVING,widget=forms.CheckboxSelectMultiple)
    host_csc = forms.MultipleChoiceField(required=False,label='Host接受CSC访学',choices=[('accepted','有明确接受依据'),('restricted','明确限制'),('unknown','尚未确认')],widget=forms.CheckboxSelectMultiple)
    identity_scope = forms.ChoiceField(required=False,label='当前身份视图',choices=[('','适合我当前博士身份'),('phd_enrolled','适合我当前博士身份'),('all','全部身份 / 后续阶段')])
    identity_fit = forms.MultipleChoiceField(required=False,label='博士在读身份适配',choices=Opportunity.IDENTITY_FIT,widget=forms.CheckboxSelectMultiple)
    verified_support = forms.MultipleChoiceField(required=False,label='分项支持证据',choices=[('visitor_cash','访客现金有据'),('fee_waiver','费用减免有据'),
        ('housing','住房支持'),('travel','差旅支持'),('csc_stipend','CSC条件资助'),('yuanhang_support','远航条件资助'),('host_topup','Host补足规则')],widget=forms.CheckboxSelectMultiple)
    priority20 = forms.BooleanField(required=False, label='仅 Priority-20 接收机构')
    priority_institution = forms.MultipleChoiceField(required=False, label='Priority-20 机构', widget=forms.SelectMultiple)
    priority_channel = forms.MultipleChoiceField(required=False, label='重点通道', choices=[('CSC','CSC框架精选'),('YUANHANG','远航合作机构')], widget=forms.CheckboxSelectMultiple)
    current_program = forms.BooleanField(required=False, label='具体通道项目已核实（可已截止）')
    channel_open = forms.BooleanField(required=False, label='通道本轮申请已确认开放')
    formal_visit = forms.BooleanField(required=False, label='已收录正式访问制度')
    dossier_level = forms.MultipleChoiceField(required=False, label='档案阅读深度', choices=[('basic','基础科研证据'),('deep','重点深档')], widget=forms.CheckboxSelectMultiple)
    fit_tier = forms.MultipleChoiceField(required=False,label='研究匹配层',choices=FIT_TIERS,widget=forms.CheckboxSelectMultiple)
    dossier = forms.MultipleChoiceField(required=False,label='档案至少具备以下任一证据',choices=DOSSIER_FIELDS,widget=forms.CheckboxSelectMultiple)
    pool = forms.ChoiceField(required=False,label='数据范围',choices=[('','精选（默认）')]+POOLS)
    qs_top100 = forms.BooleanField(required=False,label='仅 QS 2027 综合前100')
    audit_scope_status = forms.MultipleChoiceField(required=False,label='QS审计入口状态',choices=CoverageInstitution.AUDIT_SCOPE_CHOICES,widget=forms.CheckboxSelectMultiple)
    global_presence = forms.MultipleChoiceField(required=False,label='机构全局机会存在情况',choices=CoverageInstitution.GLOBAL_PRESENCE_CHOICES,widget=forms.CheckboxSelectMultiple)
    coverage_key = forms.ChoiceField(required=False,label='实际接收机构',widget=forms.HiddenInput)
    inclusion = forms.MultipleChoiceField(required=False,label='纳入依据',choices=REASONS,widget=forms.CheckboxSelectMultiple)
    high_support = forms.BooleanField(required=False,label='符合 Research Compass 产品高支持规则')
    language = forms.MultipleChoiceField(required=False,label='语言要求证据',choices=[('known','有明确要求'),('english','明确英语要求'),('unknown','尚无确证')],widget=forms.CheckboxSelectMultiple)
    freshness = forms.ChoiceField(required=False,label='事实核查时间',choices=[('','不限'),('7','7日内'),('30','30日内'),('90','90日内')])
    q = forms.CharField(required=False, max_length=200, label='中文 / 原文关键词', widget=forms.TextInput(attrs={'placeholder':'学校、导师、研究方向'}))
    sort = forms.ChoiceField(required=False, label='排序', choices=[('','直接匹配优先'),('match','研究匹配在前'),('completeness','已证档案维度在前'),
        ('deadline','下一行动日期在前'),('salary','高薪资在前'),('support','实际支持高 → 低'),('qs','QS排名'),('impact','学术影响证据'),('newest','最新加入'),('changed','最近来源变化'),('reviewed','最近事实核验')])
    basis = forms.ChoiceField(required=False, label='资金比较组', choices=[('','税前工资'),('gross_salary','税前工资'),
        ('net_salary','已明确税后工资'),('stipend','名义津贴（不估税后）')])
    region = forms.MultipleChoiceField(required=False, label='区域', widget=forms.CheckboxSelectMultiple)
    country = forms.MultipleChoiceField(required=False, label='国家', widget=forms.CheckboxSelectMultiple)
    discipline = forms.MultipleChoiceField(required=False, label='学科', widget=forms.CheckboxSelectMultiple)
    type = forms.MultipleChoiceField(required=False, label='机会类型 / 访问身份', widget=forms.CheckboxSelectMultiple)
    funding = forms.MultipleChoiceField(required=False, label='资助方式', widget=forms.CheckboxSelectMultiple)
    status = forms.MultipleChoiceField(required=False, label='状态', widget=forms.CheckboxSelectMultiple,
        choices=[('open','开放申请'),('upcoming','未来轮次'),('unknown','待核验'),('closed','已关闭'),('all','含历史')])
    theme = forms.MultipleChoiceField(required=False, label='研究主题', widget=forms.CheckboxSelectMultiple)
    receiving = forms.MultipleChoiceField(required=False, label='接收状态', widget=forms.CheckboxSelectMultiple)
    source = forms.MultipleChoiceField(required=False, label='来源', widget=forms.CheckboxSelectMultiple)
    deadline_from = forms.DateField(required=False, label='行动日期从', widget=forms.DateInput(attrs={'type':'date'}))
    deadline_to = forms.DateField(required=False, label='行动日期至', widget=forms.DateInput(attrs={'type':'date'}))
    known_date = forms.BooleanField(required=False, label='仅日期已知')
    physiology = forms.BooleanField(required=False, label='有具体生理机制依据')
    external_proof = forms.BooleanField(required=False, label='明确欢迎外部资助访问（非仅制度要求）')
    cash_only = forms.BooleanField(required=False, label='仅已核验周期现金')
    eligibility_verified = forms.BooleanField(required=False, label='个人资格已核验')
    receiving_verified = forms.BooleanField(required=False, label='明确访学名额')
    history = forms.BooleanField(required=False, label='包含历史 / 关闭记录')
    include_unknown = forms.BooleanField(required=False, label='日期 / 资金 / 接收筛选允许未知')
    def __init__(self, *args, choices, targets=False, **kwargs):
        super().__init__(*args, **kwargs)
        from .priority20 import registry
        self.fields['priority_institution'].choices=[(r['key'],r['official_name']) for r in registry()['institutions']]
        self.fields['sort'].choices += [('actionable','可联系证据在前'),('priority_institution','Priority-20 机构顺序')]
        self.fields['coverage_key'].choices=[('','不限机构')]+list(CoverageInstitution.objects.values_list('key','official_name'))
        for field, options in choices.items(): self.fields[field].choices = options
        if targets:
            self.fields['pool'].label='科研收录范围'
            self.fields['pool'].choices=[('','当前科研覆盖（默认）')]+POOLS
            self.fields.pop('discipline'); self.fields.pop('status'); self.fields.pop('source')
            self.fields.pop('identity_scope');self.fields.pop('identity_fit')
        else:
            for field in ('receiving','physiology','external_proof','eligibility_verified','receiving_verified','dossier','dossier_level',
                    'layer','role_class','host_capability','contact_receiving'):
                self.fields.pop(field)
            self.fields['sort'].choices=[r for r in self.fields['sort'].choices if r[0]!='completeness']
        for name, field in self.fields.items():
            if name not in ('q','sort') and not isinstance(field.widget,forms.CheckboxSelectMultiple):
                field.widget.attrs['aria-label']=field.label
    def clean(self):
        data = super().clean()
        if data.get('deadline_from') and data.get('deadline_to') and data['deadline_from'] > data['deadline_to']:
            raise forms.ValidationError('日期起点不能晚于终点')
        return data


class ProfileForm(forms.Form):
    formal_identity = forms.ChoiceField(label='拟核查的正式身份', required=False, choices=[('','尚未确认'),('visiting_student','访问学生'),('visiting_researcher','访问研究人员')])
    enrolled = forms.TypedChoiceField(label='在其他机构正式在读', required=False, choices=[('','未知'),('true','是'),('false','否')], coerce=lambda v:v=='true', empty_value=None)
    higher_degree = forms.TypedChoiceField(label='正在攻读高等学位', required=False, choices=[('','未知'),('true','是'),('false','否')], coerce=lambda v:v=='true', empty_value=None)
    doctoral_enrolled = forms.TypedChoiceField(label='在其他机构正式博士在读', required=False, choices=[('','未知'),('true','是'),('false','否')], coerce=lambda v:v=='true', empty_value=None)
    independent = forms.TypedChoiceField(label='正式独立研究任职', required=False, choices=[('','未知'),('true','是'),('false','否')], coerce=lambda v:v=='true', empty_value=None)
    funds_confirmed = forms.TypedChoiceField(label='已确认所需经费及其来源', required=False, choices=[('','未知'),('true','是'),('false','否')], coerce=lambda v:v=='true', empty_value=None)
    home_endorsement = forms.TypedChoiceField(label='已获原机构批准或支持证明', required=False, choices=[('','未知'),('true','是'),('false','否')], coerce=lambda v:v=='true', empty_value=None)
    self_funded = forms.TypedChoiceField(label='拟用本人/家庭自筹资金', required=False, choices=[('','未知'),('true','是'),('false','否')], coerce=lambda v:v=='true', empty_value=None)
    stipend_required = forms.TypedChoiceField(label='本次计划必须获得现金津贴', required=False, choices=[('','未知'),('true','是'),('false','否')], coerce=lambda v:v=='true', empty_value=None)
    external_funds_share = forms.DecimalField(label='非本人/家庭资金比例%（未确定留空）', required=False, min_value=0,max_value=100,decimal_places=2)
    months = forms.DecimalField(label='拟访学月数（本人填写或演示假设）', required=False, min_value=0.01, max_value=36, decimal_places=2)
    start_date = forms.DateField(label='拟起始日期', required=False, widget=forms.DateInput(attrs={'type':'date'}))
    end_date = forms.DateField(label='拟结束日期', required=False, widget=forms.DateInput(attrs={'type':'date'}))
    themes = forms.MultipleChoiceField(label='研究主题偏好', required=False, choices=[('neural_dynamics','神经动力学'),('control','控制'),('physiology','生理机制'),('world_models','世界模型'),('embodied','具身'),('bci','脑机接口'),('population_decoding','群体解码')], widget=forms.CheckboxSelectMultiple)
    def clean(self):
        data = super().clean()
        if data.get('start_date') and data.get('end_date') and data['start_date'] > data['end_date']: raise forms.ValidationError('起始不能晚于结束')
        return data

class BudgetForm(forms.Form):
    path = forms.ChoiceField(label='同一正式访问路径')
    months = forms.DecimalField(label='情景时长（月）', min_value=0.01, max_value=36, decimal_places=2)
    currency = forms.ChoiceField(label='原币', choices=[('','请选择原币')]+[(c,c) for c in ('CHF','USD','GBP','JPY','SGD','AUD','EUR')])
    display_currency = forms.ChoiceField(label='参考展示币种',required=False,choices=[('','保留原币')]+[(c,c) for c in ('EUR','CHF','USD','GBP','JPY','SGD','AUD')])
    fx_version = forms.ChoiceField(label='参考汇率快照',required=False)
    historical_fx = forms.BooleanField(label='明确使用已选历史汇率（不冒称当前排序）',required=False)
    semesters = forms.IntegerField(label='实际涉及学期数（学期计费时填写）', required=False, min_value=1, max_value=12)
    def __init__(self, *args, paths=(), **kwargs):
        super().__init__(*args, **kwargs); self.fields['path'].choices = [(p.pk,p.name) for p in paths]
        from .models import ExchangeSnapshot
        self.fields['fx_version'].choices=[('','当前最近官方快照')]+[(s.version,f'{s.date} · {s.base} · {s.version[:12]}') for s in ExchangeSnapshot.objects.order_by('-date')[:10]]
        for key,label in [('income_confirmed','确认可得月现金收入'),('living','月生活费'),('housing','月住房费'),('insurance','月保险费'),('transport','月交通费'),('deposit','可退押金'),('prepaid','需垫付的报销款'),('support_once','确认一次现金支持'),('available_cash','可动用现金')]:
            self.fields[key] = forms.DecimalField(label=label, required=False, min_value=0, max_value=100000000, decimal_places=2)
        self.fields['research_mode']=forms.ChoiceField(label='研究费用情景（本人假设）',required=False,choices=[('','未知 / 不适用'),('lab_based','实验类'),('non_lab_based','非实验类')])
        for key,label in [('STP_application','涉及Student Pass申请'),('STP_issuance','涉及Student Pass签发'),('multiple_journey_visa_if_applicable','涉及多次入境签证')]:
            self.fields[key]=forms.TypedChoiceField(label=label,required=False,choices=[('','未知'),('true','是'),('false','否')],coerce=lambda v:v=='true',empty_value=None)

class ContactForm(forms.Form):
    path = forms.ChoiceField(label='正式访问路径')
    draft = forms.CharField(label='仅本地联系准备稿', max_length=12000, widget=forms.Textarea(attrs={'rows':16}))
    outline = forms.CharField(label='研究意向提纲 / 中文核查说明', max_length=6000, required=False, widget=forms.Textarea(attrs={'rows':8}))
    def __init__(self,*args,paths=(),**kwargs):
        super().__init__(*args,**kwargs); self.fields['path'].choices=[(p.pk,p.name) for p in paths]


class PathSupportForm(forms.Form):
    host_support = forms.TypedChoiceField(label='这位主办导师支持',required=False,choices=[('','未知'),('true','确认是'),('false','确认否')],coerce=lambda v:v=='true',empty_value=None)
    agreement_ready = forms.TypedChoiceField(label='这条路径的正式协议',required=False,choices=[('','未知'),('true','已完成'),('false','未完成')],coerce=lambda v:v=='true',empty_value=None)
    evidence_note = forms.CharField(label='私人核查依据（日期、范围及事实；不上传文件）',required=False,max_length=3000,widget=forms.Textarea(attrs={'rows':3}))
    valid_until = forms.DateField(label='确认有效至（未知留空）',required=False,widget=forms.DateInput(attrs={'type':'date'}))
    def clean(self):
        data=super().clean()
        if any(data.get(k) is not None for k in ('host_support','agreement_ready')) and not data.get('evidence_note'):
            self.add_error('evidence_note','确认状态需要记录本人核查依据；没有依据请保持未知')
        return data
