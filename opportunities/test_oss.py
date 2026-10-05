"""Synthetic integration oracles, independent of the live maintenance data."""
import json, secrets
from pathlib import Path
from io import StringIO
from unittest.mock import patch
from datetime import timedelta
from django.conf import settings
from django.core.management import call_command
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from .models import (ResearchTarget, Opportunity, VisitPath, Source, UpdateRun,
                     UpdateProposal, TargetRecord, UserRecord, SavedFilter,
                     DecisionRecord, ExchangeSnapshot, FinancialFact)
from .priority20 import activate
from .updates import stage, apply_proposal, reject_proposal
from .money import comparable
from .curation import dossier_coverage

@override_settings(ALLOWED_HOSTS=['testserver','localhost','127.0.0.1'])
class SyntheticIntegration(TestCase):
    fixtures=['data/demo/synthetic_demo.json']

    def setUp(self):
        activate()
        self.user=get_user_model().objects.create_user('synthetic_user',password=secrets.token_urlsafe(32))
        self.client.force_login(self.user)

    def test_demo_seed_is_fictional_account_free_and_unauthorized(self):
        rows=json.loads((settings.BASE_DIR/'data/demo/synthetic_demo.json').read_text('utf8'))
        self.assertTrue(all(r['model'].startswith('opportunities.') for r in rows))
        forbidden={'userrecord','targetrecord','savedfilter','localprofile','decisionrecord','pathsupport','sourceauthorization','discoverypolicy'}
        self.assertFalse({r['model'].split('.')[1] for r in rows}&forbidden)
        self.assertFalse(Source.objects.filter(automation_allowed=True).exists())
        for m in (Opportunity,ResearchTarget,VisitPath):
            self.assertTrue(all('.invalid/' in v for v in m.objects.values_list('url',flat=True)))

    def test_real_views_demo_layers_and_dossier(self):
        for url in ('/','/priority20/','/priority20/demo-01/','/targets/','/targets/?layer=watchlist',
                    '/targets/?layer=reference','/targets/?layer=deep','/targets/?layer=coverage',
                    '/targets/1/','/opportunities/1/','/profile/','/compare/',
                    '/targets/1/budget/','/targets/1/contact/','/sources/','/workspace/','/coverage/'):
            response=self.client.get(url)
            self.assertEqual(response.status_code,200,url)
            self.assertContains(response,'Synthetic demo')
        facts=dossier_coverage(ResearchTarget.objects.get(pk=1))
        self.assertEqual((facts['first_known'],facts['public_alumni_total']),(1,2))
        self.assertTrue(facts['awards'] and facts['projects'] and facts['lineage'])
        self.assertFalse(facts['support_conditions'])

    def test_private_save_filter_budget_and_other_user_isolation(self):
        self.assertEqual(self.client.post('/targets/1/record/',{'stage':'researching','note':'Synthetic private note'}).status_code,302)
        self.assertEqual(self.client.post('/opportunities/1/record/',{'stage':'preparing','note':'Synthetic application note'}).status_code,302)
        self.assertEqual(self.client.post('/filters/save/',{'name':'Synthetic selection','return_to':'/targets/?fit_tier=A'}).status_code,302)
        r=self.client.post('/targets/1/budget/',{'action':'save','path':1,'months':'2','currency':'EUR','income_confirmed':'1000','living':'300','housing':'400',
            'insurance':'50','transport':'20','deposit':'500','prepaid':'0','support_once':'0','available_cash':'2000'})
        self.assertEqual(r.status_code,200)
        self.assertTrue(DecisionRecord.objects.filter(user=self.user,kind='budget').exists())
        other=get_user_model().objects.create_user('synthetic_other')
        self.client.force_login(other)
        self.assertNotContains(self.client.get('/workspace/'),'Synthetic private note')
        self.assertEqual((TargetRecord.objects.count(),UserRecord.objects.count(),SavedFilter.objects.count()),(1,1,1))

    def test_and_filters_conflicts_and_expired_online_page(self):
        r=self.client.get('/targets/?layer=coverage&fit_tier=A&priority_institution=demo-01')
        self.assertContains(r,'Demo Researcher A')
        self.assertNotContains(r,'Demo Researcher B')
        t=ResearchTarget.objects.get(pk=1);t.pending_change=True;t.save(update_fields=['pending_change'])
        self.assertEqual(t.effective_actionability,'research_watchlist')
        self.assertNotContains(self.client.get('/targets/'),'Demo Researcher A')
        self.assertEqual(Opportunity.objects.get(pk=4).effective_status,'closed')
        self.assertEqual(self.client.get('/opportunities/4/').status_code,200)

    def test_salary_bases_unknown_and_grant_exclusion(self):
        snap=ExchangeSnapshot.objects.create(date=timezone.localdate(),base='EUR',rates={'USD':'1.2'},url='https://fx.example.invalid/',version='synthetic-fx',fetched_at=timezone.now())
        fact=FinancialFact.objects.get(pk=1)
        value,error=comparable(fact,snap)
        self.assertEqual(value['monthly_original'],2500)
        self.assertIsNone(comparable(fact,snap,'net_salary')[0])
        fact.kind='total_grant';self.assertIsNone(comparable(fact,snap)[0])
        fact.kind='salary';fact.tax_basis='unknown';self.assertIsNone(comparable(fact,snap)[0])
        fact.tax_basis='gross';fact.opportunity.pending_change=True;self.assertIsNone(comparable(fact,snap)[0])

    def observe(self,payload,revision):
        source=Source.objects.get(pk=1)
        run=UpdateRun.objects.create(source=source,url=source.url,method='synthetic_offline')
        return stage(run,'demo-path-1','path',payload,revision,timezone.now())

    def test_a_b_a_current_version_not_historical_fingerprint(self):
        a={'key':'demo-path-1','details_patch':{'synthetic_condition':'A'}}
        b={'key':'demo-path-1','details_patch':{'synthetic_condition':'B'}}
        first=self.observe(a,'1');apply_proposal(first,'Synthetic condition A explicitly reviewed.')
        second=self.observe(b,'2');apply_proposal(second,'Synthetic condition B explicitly reviewed.')
        returned=self.observe(a,'3')
        self.assertEqual(returned.status,'pending')
        self.assertNotEqual(returned.pk,first.pk)
        self.assertEqual(VisitPath.objects.get(pk=1).details['synthetic_condition'],'B')
        apply_proposal(returned,'Synthetic return to A reviewed against current B.')
        self.assertEqual(VisitPath.objects.get(pk=1).details['synthetic_condition'],'A')

    def test_conflict_reject_apply_rescan_history(self):
        a={'key':'demo-path-1','details_patch':{'synthetic_condition':'A'}}
        b={'key':'demo-path-1','details_patch':{'synthetic_condition':'B'}}
        first=self.observe(a,'1');apply_proposal(first,'Independent synthetic A approval.')
        conflict=self.observe(b,'1');self.assertEqual(conflict.status,'conflict')
        self.assertTrue(UpdateProposal.objects.filter(status='conflict').exists())
        reject_proposal(conflict,'Synthetic same-version conflicting observation rejected.')
        self.assertEqual(self.observe(b,'1').status,'rejected')
        newer=self.observe(b,'2');self.assertEqual(newer.status,'rejected')
        changed={**b,'details_patch':{'synthetic_condition':'C'}}
        fresh=self.observe(changed,'3');self.assertEqual(fresh.status,'pending')
        apply_proposal(fresh,'Independent changed synthetic condition C approval.')
        self.assertEqual(self.observe(changed,'4').status,'applied')

    def test_due_preview_never_fetches_or_changes_fact_clocks(self):
        before=list(ResearchTarget.objects.values_list('pk','verified_at'))
        out=StringIO()
        with patch('opportunities.updates.request_json',side_effect=AssertionError('No network allowed')):
            call_command('refresh_due',dry_run=True,limit=25,stdout=out)
        queue=json.loads(out.getvalue());self.assertEqual(queue['network_requests'],0)
        self.assertFalse(queue['scheduler_installed'])
        self.assertEqual(list(ResearchTarget.objects.values_list('pk','verified_at')),before)
