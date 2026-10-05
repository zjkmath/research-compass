from django.apps import AppConfig
from django.db.models.signals import pre_save


class OpportunitiesConfig(AppConfig):
    name = 'opportunities'
    default_auto_field = 'django.db.models.BigAutoField'

    def ready(self):
        from .models import Opportunity, ResearchTarget, Organization, Person
        from .priority20 import guard
        for model in (Opportunity, ResearchTarget, Organization, Person):
            pre_save.connect(guard, sender=model, dispatch_uid='priority20.' + model._meta.model_name)
