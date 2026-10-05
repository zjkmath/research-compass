from django.contrib import admin
from .models import Source, Organization, Person, Program, Opportunity, Deadline, FinancialFact, Evidence, ImportRun, ResearchTarget, VisitPath, EntityRevision, SourceAuthorization, UpdateRun, UpdateProposal, FundingRelation, ExchangeSnapshot

admin.site.site_header = '研途 · 来源与数据审核'
admin.site.site_title = '研途审核后台'
admin.site.index_title = 'G2 数据核验工作台'


@admin.register(Source)
class SourceAdmin(admin.ModelAdmin):
    list_display = ['name', 'region', 'access_method', 'automation_allowed', 'last_success_at', 'last_error']
    list_filter = ['region', 'automation_allowed', 'access_method']
    search_fields = ['name', 'policy_note']
    readonly_fields = ['automation_allowed', 'policy_checked_at', 'adapter', 'scope', 'url_rules', 'last_attempt_at', 'last_success_at', 'last_error', 'retry_after_at']

    def save_model(self, request, obj, form, change):
        if change:
            from .ingest import canonical_url
            previous = Source.objects.get(pk=obj.pk)
            if previous.automation_allowed and canonical_url(previous.url) != canonical_url(obj.url):
                obj.automation_allowed = False
                SourceAuthorization.objects.create(source=previous, allowed=False, scope=previous.scope,
                    policy_url=previous.terms_url or previous.url, evidence='审核后台改变接口范围；原授权失效。')
        super().save_model(request, obj, form, change)


class DeadlineInline(admin.TabularInline):
    model = Deadline
    extra = 0


class FinancialInline(admin.TabularInline):
    model = FinancialFact
    extra = 0


class EvidenceInline(admin.TabularInline):
    model = Evidence
    extra = 0


@admin.register(Opportunity)
class OpportunityAdmin(admin.ModelAdmin):
    list_display = ['title_zh', 'type', 'country', 'status', 'review_status', 'verified_at', 'is_test']
    list_filter = ['type', 'region', 'status', 'review_status', 'is_test']
    search_fields = ['title', 'title_zh', 'institution__name']
    inlines = [DeadlineInline, FinancialInline, EvidenceInline]
    readonly_fields = ['identity', 'fingerprint', 'updated_at']
    filter_horizontal = ['mentors', 'related_funding']
    actions = ['verify_with_evidence', 'mark_pending']

    @admin.action(description='核验有证据的选中记录')
    def verify_with_evidence(self, request, queryset):
        verified = 0
        for item in queryset:
            if item.evidence.exists() and item.status_note and item.verified_at:
                item.review_status = 'verified'
                item.save(update_fields=['review_status'])
                verified += 1
        self.message_user(request, f'已审核 {verified} 条；缺证据记录保留待审核状态。')

    @admin.action(description='标为待审核（从公众列表移除）')
    def mark_pending(self, request, queryset):
        queryset.update(review_status='pending')

    def save_model(self, request, obj, form, change):
        from .ingest import opportunity_identity
        from .history import public_snapshot
        request._radar_old_snapshot = public_snapshot(Opportunity.objects.get(pk=obj.pk)) if change else {}
        obj.identity = opportunity_identity(obj)
        # Editing a fact requires a new explicit review, avoiding silent public drift.
        obj.review_status = 'pending'
        super().save_model(request, obj, form, change)

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        from .history import remember
        remember(form.instance, request._radar_old_snapshot, source=form.instance.source, reason='admin fact/inline edit; pending review')


@admin.register(ImportRun)
class ImportRunAdmin(admin.ModelAdmin):
    list_display = ['created_at', 'method', 'input_name', 'created', 'updated', 'unchanged']
    readonly_fields = ['created_at', 'method', 'input_name', 'fingerprint', 'created', 'updated', 'unchanged', 'error']

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False


admin.site.register([Organization, Person, Program])


@admin.register(EntityRevision, SourceAuthorization, UpdateRun, UpdateProposal)
class AuditAdmin(admin.ModelAdmin):
    def get_readonly_fields(self, request, obj=None):
        return [f.name for f in self.model._meta.fields]
    def has_add_permission(self, request): return False
    def has_delete_permission(self, request, obj=None): return False
    def has_change_permission(self, request, obj=None): return False

admin.site.register([ResearchTarget, VisitPath, FundingRelation, ExchangeSnapshot])
