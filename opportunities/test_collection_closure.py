"""Retained synthetic software regressions; real content ledgers are not distributed."""
import json,shutil,uuid

from pathlib import Path

from unittest.mock import patch

from datetime import timedelta

from django.conf import settings

from django.test import TestCase,override_settings

from django.utils import timezone

from opportunities.discovery import Reader,collect,candidate,doctoral,record,decimal_number

from opportunities.models import Opportunity,Source,FinancialFact,ExchangeSnapshot,UpdateProposal

from opportunities.money import comparable

class ClosureFlow(TestCase):
    def setUp(self):
        self.root=settings.BASE_DIR/('closure-test-'+uuid.uuid4().hex);self.root.mkdir();self.addCleanup(shutil.rmtree,self.root)
        self.override=override_settings(DATA_DIR=self.root,CACHE_DIR=self.root/'cache');self.override.enable();self.addCleanup(self.override.disable)
        self.c={'key':'synthetic-closure','parser':'tue','institution':'Synthetic University','institution_url':'https://public.example/',
            'country':'合成','url':'https://public.example/list/','allowed_hosts':['public.example'],'permission_note':'synthetic','terms_url':'https://public.example/terms','scope':'synthetic only'}
    def network(self,ids,fail=False):
        def raw(reader,url):
            if url.endswith('/robots.txt'):return 'User-agent: *\nAllow: /'
            if url==self.c['url']:return '<table>'+''.join('<tr><td><a href="https://public.example/what:job/jobID:'+str(i)+'/">PhD in Control '+str(i)+'</a></td><td>2099-12-20</td></tr>' for i in ids)+'</table>'
            if fail:raise TimeoutError('independent simulated timeout')
            return '<h1>PhD in Control</h1><p>We recruit a doctoral student with a Master degree to investigate feedback control.</p><a href="/apply">Apply now</a>'
        return raw
    def test_independent_title_and_body_rules(self):
        for title in ['PhD in Internet Security','Postdoctoral and PhD Positions in Neurotechnology','Doctoral researcher in sustainable supply chains']:self.assertTrue(doctoral(title))
        for title in ['Postdoctoral researcher','Post-doctoral fellow in Biology','Post-doctoral fellow in AI-based design of de novo protein biofilm systems','Internship in robotics','Professor with PhD']:self.assertFalse(doctoral(title))
        item={'id':'x','title':'Researcher','url':'https://public.example/x'}
        with self.assertRaisesRegex(ValueError,'non_doctoral_detail'):record(self.c,item,'<h1>Postdoctoral researcher</h1><p>Completed PhD required.</p>',timezone.now().isoformat())
        item['title']='Doctoral researcher in sustainable supply chain management'
        result=record(self.c,item,'<h1>'+item['title']+'</h1><p>We recruit a PhD student with a Master degree.</p><a href="/apply">Apply now</a>',timezone.now().isoformat())
        self.assertEqual(result['discipline'],'管理与社会科学')
    def test_new_list_resume_failure_retry_and_explicit_approval(self):
        with patch.object(Reader,'raw',self.network([1,2])):a=collect(self.c,1,2)
        self.assertEqual(a['remaining'],1);self.assertEqual(a['counts']['pending_review'],1);self.assertFalse(a['publishable_applied'])
        with patch.object(Reader,'raw',self.network([1,2])):b=collect(self.c,1,2)
        self.assertEqual(b['remaining'],0);self.assertEqual(b['counts']['pending_review'],2)
        with self.assertRaisesRegex(ValueError,'approve'):collect(self.c,1,2,True)
        with patch.object(Reader,'raw',self.network([1,2])):c=collect(self.c,1,2,approve_ids=['1','2'],review_note='Explicit independent source sentence review: doctoral students and Masters; exact IDs and current scan checked.')
        self.assertEqual(c['counts'],{'applied':2});self.assertEqual(Opportunity.objects.count(),2)
        with patch.object(Reader,'raw',self.network([1,2,3],fail=True)):d=collect(self.c,10,2,action='new')
        self.assertIn('3',d['discovered']);self.assertEqual(d['processed']['3']['fetch'],'retryable_failed')
        with patch.object(Reader,'raw',self.network([1,2,3])):e=collect(self.c,10,2,retry_failed=True)
        self.assertEqual(e['processed']['3']['fetch'],'success');self.assertEqual(Opportunity.objects.count(),2)
        self.assertFalse(Source.objects.get().automation_allowed)
    def test_cache_ttl_304_no_body_and_no_store(self):
        r=Reader(self.root/'cache',['public.example'],delay=0)
        calls=[]
        def first(reader,url):calls.append(url);reader.response_meta={'ETag':'A'};return 'User-agent: *\nAllow: /' if url.endswith('/robots.txt') else 'BODY A'
        with patch.object(Reader,'raw',first):self.assertEqual(r.read(self.c['url']),'BODY A');self.assertEqual(r.read(self.c['url']),'BODY A')
        self.assertEqual(len(calls),2)
        with patch.object(Reader,'raw',return_value=None):self.assertEqual(r.read(self.c['url'],refresh=True),'BODY A')
        self.assertEqual(r.last_meta['mode'],'LIVE_304')
        for p in (self.root/'cache').glob('*.html'):p.unlink()
        with patch.object(Reader,'raw',return_value=None),self.assertRaisesRegex(ValueError,'304'):r.read(self.c['url'],refresh=True)
        def no_store(reader,url):reader.response_meta={'Cache-Control':'no-store'};return 'PRIVATE CACHE CONTROL BODY'
        with patch.object(Reader,'raw',no_store):r.read(self.c['url'],refresh=True)
        self.assertFalse(list((self.root/'cache').glob('*.html')))
    def test_lock_legacy_and_atomic_failure_recovery(self):
        with patch.object(Reader,'raw',self.network([1])):a=collect(self.c,2,2)
        work=self.root/'discovery'/self.c['key'];(work/'scan.lock').write_text('synthetic other owner')
        with self.assertRaisesRegex(ValueError,'locked'):collect(self.c,2,2)
        (work/'scan.lock').unlink()
        old={'source_id':self.c['key'],'scope':'old','discovered':{'1':{'id':'1','title':'Postdoctoral Researcher','url':'https://public.example/1'}},'processed':{'1':{'status':'excluded','reason':'not_doctoral_title'}},'pages':[],'next_url':None,'endpoint_reached':True}
        (work/'scan.json').write_text(json.dumps(old))
        b=collect(self.c,2,2);self.assertEqual(b['legacy_original']['processed'],old['processed']);self.assertEqual(b['processed']['1']['review'],'excluded')
        self.assertTrue((work/'scan.rc2.json').exists())
        with patch('opportunities.discovery.os.replace',side_effect=OSError('interrupted replace')),self.assertRaises(OSError):collect(self.c,2,2,action='new')
        self.assertFalse((work/'scan.lock').exists());self.assertEqual(json.loads((work/'scan.json').read_text())['schema'],3)
    def test_decimal_regions_are_explicit(self):
        self.assertEqual(str(decimal_number('1,000.50','en')),'1000.50');self.assertEqual(str(decimal_number('1.000,50','de')),'1000.50')
        with self.assertRaises(ValueError):decimal_number('1.000,50','unknown')

    def test_reference_tokens_are_not_shared_vacancy_ids(self):
        for identifier,text in [('111','Reference number UFV-PA 2026/2345'),('222','Reference number STÖD Contact support')]:
            item={'id':identifier,'title':'PhD in Control','url':'https://public.example/'+identifier}
            result=record(self.c,item,'<h1>PhD in Control</h1><p>We recruit a PhD student with a Master degree.</p>'+text+'<a href="/apply">Apply now</a>',timezone.now().isoformat())
            self.assertEqual(result['official_id'],'UFV-PA 2026/2345' if identifier=='111' else '222')
        item={'id':'100001','official_id':'100001','title':'PhD in Control','url':'https://public.example/shared'}
        body='<h1>PhD in Control</h1><p>We recruit a PhD student with a Master degree.</p><p>Reference number 100002</p><a href="/apply">Apply now</a>'
        self.assertEqual(record(self.c,item,body,timezone.now().isoformat())['official_id'],'100002')
        with self.assertRaisesRegex(ValueError,'ambiguous_reference'):
            record(self.c,item,body+'<p>Reference number 100003</p>',timezone.now().isoformat())

    def test_rejection_requires_reconsideration_and_remains_history(self):
        from opportunities.updates import reject_proposal
        with patch.object(Reader,'raw',self.network([1])):a=collect(self.c,10,2)
        p=UpdateProposal.objects.get(pk=a['processed']['1']['proposal_id']);reject_proposal(p,'Independent synthetic rejection; no acceptance evidence')
        before=UpdateProposal.objects.count()
        with patch.object(Reader,'raw',self.network([1])):b=collect(self.c,10,2,approve_ids=['1'],review_note='Explicit review cannot silently overturn prior rejection; refusal expected.')
        self.assertEqual(Opportunity.objects.count(),0);self.assertEqual(UpdateProposal.objects.count(),before)
        with patch.object(Reader,'raw',self.network([1])):c=collect(self.c,10,2,reconsider_ids=['1'],approve_ids=['1'],review_note='Fresh source and independent qualification sentence checked; explicit reconsideration of former rejected item.')
        self.assertEqual(c['counts'],{'applied':1});p.refresh_from_db();self.assertEqual(p.status,'rejected')

    def test_429_403_absence_and_empty_parse_do_not_close_existing(self):
        from urllib.error import HTTPError
        with patch.object(Reader,'raw',self.network([1])):collect(self.c,10,2,approve_ids=['1'],review_note='Independent synthetic PhD sentence and Master qualification approved.')
        def limited(reader,url):
            if url.endswith('/robots.txt') or url==self.c['url']:return self.network([2])(reader,url)
            raise HTTPError(url,429,'Synthetic rate limit',{'Retry-After':'120'},None)
        with patch.object(Reader,'raw',limited):a=collect(self.c,10,2,action='new')
        self.assertIn('1',a['discovered']);self.assertEqual(a['discovered']['1']['purpose'],'managed_absent_recheck')
        self.assertEqual(a['processed']['1']['fetch'],'retryable_failed');self.assertTrue(a['processed']['1']['next_retry_at'])
        self.assertEqual(Opportunity.objects.get().status,'open')
        def denied(reader,url):
            if url.endswith('/robots.txt') or url==self.c['url']:return self.network([2])(reader,url)
            raise HTTPError(url,403,'Synthetic permissions',{},None)
        with patch.object(Reader,'raw',denied):b=collect(self.c,10,2,action='new')
        self.assertEqual(b['processed']['1']['fetch'],'blocked');self.assertEqual(Opportunity.objects.get().status,'open')
        with patch.object(Reader,'raw',return_value='User-agent: *\nAllow: /'):c=collect(self.c,10,2,action='new')
        self.assertFalse(c['list_done']);self.assertTrue(c['list_error']);self.assertEqual(Opportunity.objects.get().status,'open')

    def test_ttl_content_change_no_cache_and_rotation(self):
        from opportunities.discovery import qualifications
        html='<p>Department mission and degree programmes, unrelated to applicant eligibility.</p><h2>Job Requirements</h2><ul><li>A master degree in Physics.</li><li>Strong mathematical background.</li></ul><h2>Conditions of Employment</h2><p>Gross salary.</p>'
        self.assertEqual(qualifications(html),'A master degree in Physics. Strong mathematical background.')
        from opportunities.discovery import record
        advert='<h1>PhD student in Physics</h1><h2>Job Requirements</h2><p>A master degree in Physics. Good command of English (B2+ level).</p><h2>Application</h2><a href="https://public.example/apply/">Apply now</a>'
        parsed=record(self.c,{'id':'101','title':'PhD student in Physics','url':'https://public.example/jobs/101'},advert,timezone.now().isoformat())
        self.assertEqual(parsed['language_requirements'],'Good command of English (B2+ level).')
        import hashlib
        from opportunities.discovery import atomic_json
        directory=self.root/'cache';r=Reader(directory,['public.example'],delay=0);calls=[]
        def raw(reader,url):calls.append(url);reader.response_meta={'Cache-Control':'no-cache'};return 'User-agent: *\nAllow: /' if url.endswith('/robots.txt') else 'A'
        with patch.object(Reader,'raw',raw):r.read(self.c['url']);r.read(self.c['url'])
        self.assertEqual(len(calls),3)
        def changed(reader,url):reader.response_meta={};return 'B'
        with patch.object(Reader,'raw',changed):self.assertEqual(r.read(self.c['url'],ttl=0),'B')
        path=directory/(hashlib.sha256(self.c['url'].encode()).hexdigest()+'.json');meta=json.loads(path.read_text());self.assertEqual(meta['sha256'],hashlib.sha256(b'B').hexdigest())
        (directory/'rebuildable.html').write_bytes(b'x'*1000)
        with patch('opportunities.discovery.CACHE_LIMIT',1600),patch.object(Reader,'raw',changed):
            with self.assertRaisesRegex(ValueError,'capacity'):r.read(self.c['url'],refresh=True)
        self.assertFalse((directory/'rebuildable.html').exists());self.assertTrue((self.root/'cache').exists())

    def test_revalidated_content_does_not_get_a_new_fact_clock(self):
        from opportunities.discovery import atomic_json
        old='2026-09-01T12:00:00+00:00'
        with patch.object(Reader,'raw',self.network([1])):collect(self.c,10,2)
        for meta in (self.root/'cache/discovery').glob('*.json'):
            values=json.loads(meta.read_text(encoding='utf8'))
            values['observed_content_at']=old;atomic_json(meta,values)
        with patch.object(Reader,'raw',self.network([1])):state=collect(self.c,10,2,action='new')
        row=state['processed']['1']
        self.assertEqual(row['content_checked_at'],old)
        self.assertTrue(all(e['verified_at']==old for e in row['record']['evidence']))
        self.assertNotEqual(row['fetch_checked_at'],old)

    def test_p20_legacy_maintenance_never_excludes_retained_postdoc(self):
        from opportunities.priority20 import activate
        with patch.object(Reader,'raw',self.network([1])):collect(self.c,10,2,approve_ids=['1'],review_note='Independent source sentence and original doctoral identity approved.')
        obj=Opportunity.objects.get();obj.type='postdoc';obj.title='Postdoctoral neural dynamics';obj.full_clean();obj.save()
        activate()
        with patch.object(Reader,'raw',side_effect=AssertionError('Unsupported maintenance must not request a new list')):
            state=collect(self.c,10,2,action='new')
        obj.refresh_from_db()
        self.assertEqual(obj.status,'open')
        self.assertEqual(state['counts'],{'pending_review':1})
        self.assertFalse(state['endpoint_reached'])
        self.assertFalse(state['list_traversal_complete'])
        self.assertEqual(next(iter(state['processed'].values()))['reason'],'retained_non_doctoral_requires_matching_manual_review')

    def test_shared_page_listing_reference_does_not_borrow_old_identity(self):
        with patch.object(Reader,'raw',self.network([1])):collect(self.c,10,2,approve_ids=['1'],review_note='Independent old official reference and doctoral qualification verified.')
        old=Opportunity.objects.get()
        new={'id':'new-reference','official_id':'NEW-002','title':'PhD new separate advert','url':old.url}
        with patch.object(Reader,'raw',self.network([])),patch('opportunities.discovery.lists',return_value=([new],None)):
            state=collect(self.c,0,1,action='new')
        self.assertEqual(state['discovered']['new-reference']['official_id'],'NEW-002')
        self.assertNotIn('managed_key',state['discovered']['new-reference'])
        self.assertEqual(len(state['discovered']),2)

    def test_managed_detail_changed_reference_preserved_as_conflict(self):
        with patch.object(Reader,'raw',self.network([1])):state=collect(self.c,10,2,approve_ids=['1'],review_note='Independent old official reference and doctoral qualification verified.')
        old=Opportunity.objects.get();original=old.official_id
        changed=dict(state['processed']['1']['record'],official_id='CHANGED-REF')
        with patch.object(Reader,'raw',self.network([1])),patch('opportunities.discovery.record',return_value=changed):
            state=collect(self.c,10,2,action='new')
        old.refresh_from_db();row=state['processed']['1']
        self.assertEqual(old.official_id,original)
        self.assertTrue(old.pending_change)
        self.assertEqual(row['record']['official_id'],'CHANGED-REF')
        self.assertEqual(row['review'],'conflict')
