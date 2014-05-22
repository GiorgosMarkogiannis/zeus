import datetime
import json


from random import choice
from datetime import timedelta
from django.contrib.auth.hashers import make_password
from django.test import TestCase
from django.test.client import Client

from zeus.core import to_relative_answers, gamma_encode, prove_encryption
from helios.views import ELGAMAL_PARAMS
from helios.crypto import algs
from helios.crypto.elgamal import *
from zeus.models.zeus_models import Institution
from heliosauth.models import User
from helios.models import * 

class SetUpAdminAndClientMixin():
        
    def setUp(self):
        institution = Institution.objects.create(name="test_inst")
        self.admin = User.objects.create(user_type="password", user_id="test_admin",
                                         info={"password": make_password("test_admin")},
                                         admin_p=True, institution=institution)
        self.locations = {
                          'home': '/',
                          'logout': '/auth/auth/logout',
                          'login':'/auth/auth/login',
                          'create': '/elections/new',
                         }
        self.login_data = {'username': 'test_admin', 'password': 'test_admin'}
        self.c = Client()


#subclass order is significant
class TestUsersWithClient(SetUpAdminAndClientMixin, TestCase):

    def setUp(self):
        super(TestUsersWithClient, self).setUp()

    def test_user_on_login_page(self):
        r = self.c.get('/', follow=True)
        self.assertEqual(r.status_code, 200)

    def test_admin_login_with_creds(self):
        r = self.c.post(self.locations['login'], self.login_data, follow=True)
        self.assertEqual(r.status_code, 200)
        #user has no election so it redirects from /admin to /elections/new
        self.assertRedirects(r, self.locations['create'])

    def test_forbid_logged_admin_to_login(self):
        self.c.post(self.locations['login'], self.login_data)
        r = self.c.post(self.locations['login'], self.login_data)
        self.assertEqual(r.status_code, 403)

    def test_admin_login_wrong_creds(self):
        wrong_creds = {'username': 'wrong_admin', 'password': 'wrong_password'}
        r = self.c.post(self.locations['login'], wrong_creds)
        #if code is 200 user failed to login and wasn't redirected
        self.assertEqual(r.status_code, 200)

    def test_logged_admin_can_logout(self):
        self.c.post(self.locations['login'], self.login_data)
        r = self.c.get(self.locations['logout'], follow=True)
        self.assertRedirects(r, self.locations['home'])


class TestElectionBase(SetUpAdminAndClientMixin, TestCase):
    
    def setUp(self):
        super(TestElectionBase, self).setUp()
        trustees = ("test1 trustee1, test_trustee1@mail.com\n"
                    "test2 trustee2, test_trustee2@mail.com")
        start_time = datetime.datetime.now()
        end_time = datetime.datetime.now() + timedelta(hours=2)
        date1, date2 = datetime.datetime.now() + timedelta(hours=48),datetime.datetime.now() + timedelta(hours=56)
        self.election_form = {
                              'trial': True,
                              'name': 'test_election',
                              'description': 'testing_election',
                              'trustees': trustees,
                              'voting_starts_at_0': date1.strftime('%Y-%m-%d'),
                              'voting_starts_at_1': date1.strftime('%H:%M'),
                              'voting_ends_at_0': date2.strftime('%Y-%m-%d'),
                              'voting_ends_at_1': date2.strftime('%H:%M'),
                              'help_email': 'test@test.com',
                              'help_phone': 6988888888,
                              'communication_language': 'el',
                              'random_value': 49
                              }
    def prepare_trustees(self,e_uuid):
        e = Election.objects.get(uuid=e_uuid)
        pks = {}
        for t in e.trustees.all():
            if not t.secret_key:
                login_url = t.get_login_url()
                self.c.get(self.locations['logout'])
                r = self.c.get(login_url)
                self.assertEqual(r.status_code, 302)
                t1_kp = ELGAMAL_PARAMS.generate_keypair()
                pk = algs.EGPublicKey.from_dict(dict(p=t1_kp.pk.p, 
                                                     q=t1_kp.pk.q,
                                                     g=t1_kp.pk.g,
                                                     y=t1_kp.pk.y))
                pok = t1_kp.sk.prove_sk(DLog_challenge_generator)
                post_data = {
                             'public_key_json':[json.dumps({'public_key': pk.toJSONDict(), 
                             'pok': {'challenge': pok.challenge,
                             'commitment': pok.commitment,
                             'response': pok.response}})]}
                             
                r = self.c.post('/elections/%s/trustee/upload_pk' %
                               (e_uuid), post_data, follow=True)
                self.assertEqual(r.status_code, 200)
                t = Trustee.objects.get(pk=t.pk)
                t.last_verified_key_at = datetime.datetime.now()
                t.save()
                pks[t.uuid] = t1_kp
        return pks
    
    def freeze_election(self):
        e = Election.objects.get(uuid=self.e_uuid)
        self.c.get(self.locations['logout'])
        r = self.c.post(self.locations['login'], self.login_data)
        freeze_location = '/elections/%s/freeze' % self.e_uuid
        r = self.c.post(freeze_location, follow=True)
        e = Election.objects.get(uuid=self.e_uuid)
        if e.frozen_at:
            return True
            
    def create_poll(self):
        self.c.get(self.locations['logout'])
        self.c.post(self.locations['login'], self.login_data)
        e = Election.objects.all()[0]
        #there shouldn't be any polls before we create them
        self.assertEqual(e.polls.all().count(), 0)
        location = '/elections/%s/polls/add' % self.e_uuid
        post_data = {'form-0-name': 'test_poll',
                     'form-TOTAL_FORMS': 1,
                     'form-INITIAL_FORMS': 0,
                     'form-MAX_NUM_FORMS': 100}
        self.c.post(location, post_data)
        e = Election.objects.all()[0]
        self.assertEqual(e.polls.all().count(), 1)
        self.p_uuid = e.polls.all()[0].uuid

    def get_voters_file(self):
        data = ("1,voter1@mail.com,v1,oter1\n"
                "2,voter2@mail.com,v2,oter2\n"
                "3,voter3@mail.com,v3,oter3")
        fname = "/tmp/voters.csv"
        fp = file(fname, 'w')
        fp.write(data)
        fp.close()
        return fname

    def submit_voters_file(self):
        voters_file = file(self.get_voters_file())
        upload_voters_location = '/elections/%s/polls/%s/voters/upload' %(self.e_uuid, self.p_uuid)
        r = self.c.post(upload_voters_location, {'voters_file': voters_file})
        r = self.c.post(upload_voters_location, {'confirm_p': 1})
        e = Election.objects.get(uuid=self.e_uuid)
        voters = e.voters.count()
        self.assertTrue(voters > 0)

    def get_voters_urls(self):
        e = Election.objects.get(uuid=self.e_uuid)
        voters = e.voters.all()
        #dict where key is the id of voter and value is the url
        voters_urls = {}
        for v in voters:
            voters_urls[v.id] = v.get_quick_login_url()
        #check if we got the urls for all voters
        self.assertEqual(len(voters_urls), len(voters))
        return voters_urls 

    def submit_vote_for_each_voter(self,voters_urls):
        pass

    def temp_cast_single_ballot(self, voters_urls):
        the_url = voters_urls[3]
        e = Election.objects.get(uuid=self.e_uuid)
        selection = e.polls.all()[0].questions_data[0]['answers']
        size = len(selection)
        random.shuffle(selection)
        selection = selection[:choice(range(len(selection)))]
        selection = selection[0]
        rel_selection = to_relative_answers(selection, size)
        encoded = gamma_encode(rel_selection, size, size)
        plaintext = algs.EGPlaintext(encoded, e.public_key)
        randomness = algs.Utils.random_mpz_lt(e.public_key.q)
        cipher = e.public_key.encrypt_with_r(plaintext, randomness, True)
        modulus, generator, order = e.zeus.do_get_cryptosystem()
        enc_proof = prove_encryption(modulus, generator, order, cipher.alpha,
                                     cipher.beta, randomness)
        self.c.get(the_url, follow=True)

        cast_data = {}
        ##############
        ballot = {
                  'election_hash': 'foobar',
                  'election_uuid': e.uuid,
                  'answers': [{
                               'encryption_proof':enc_proof,
                               'choices':[{'alpha': cipher.alpha, 'beta': cipher.beta}]
                              }]
                 }
        ################
        enc_vote = datatypes.LDObject.fromDict(ballot,
                type_hint='phoebus/EncryptedVote').wrapped_obj
        print enc_vote

class TestSimpleElection(TestElectionBase):

    def setUp(self):
        super(TestSimpleElection, self).setUp()

    def admin_can_submit_election_form(self):
        #login with admin
        self.c.post(self.locations['login'], self.login_data)

        #fill rest of form according to simple election needs
        self.election_form['election_module'] = 'simple'
        r = self.c.post(self.locations['create'], self.election_form, follow=True) 
        e = Election.objects.all()[0]
        self.e_uuid = e.uuid
        self.assertIsInstance(e, Election)

    def create_questions_for_simple(self):
        
        post_data = {'form-TOTAL_FORMS': 1,
                     'form-INITIAL_FORMS': 1,
                     'form-MAX_NUM_FORMS': "",
                     'form-0-choice_type': 'choice',
                     'form-0-question': 'test_question',
                     'form-0-min_answers': 1,
                     'form-0-max_answers': 1,
                     'form-0-answer_0': 'test answer 0',
                     'form-0-answer_1': 'test answer 1',
                     'form-0-ORDER': 0,
                     }
        return post_data

    def submit_simple_questions(self):
        post_data = self.create_questions_for_simple()
        questions_location = '/elections/%s/polls/%s/questions/manage'%(self.e_uuid, self.p_uuid)
        r = self.c.post(questions_location, post_data)
        p = Poll.objects.get(uuid=self.p_uuid)
        self.assertTrue(p.questions_count > 0)

    def test_election_proccess(self):
        self.admin_can_submit_election_form()
        #can't freeze election before requirements are met
        self.assertEqual(self.freeze_election(), None)
        pks = self.prepare_trustees(self.e_uuid)
        item = self.get_voters_file()
        self.create_poll()
        self.submit_voters_file()
        self.submit_simple_questions()
        #now all requirements are met, issues must be empty list  
        e = Election.objects.get(uuid=self.e_uuid)
        self.assertEqual (e.election_issues_before_freeze, [])
        self.assertTrue(self.freeze_election())
        voters_urls = self.get_voters_urls() 
        self.temp_cast_single_ballot(voters_urls)
