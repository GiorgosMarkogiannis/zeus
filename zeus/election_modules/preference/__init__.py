import json
import os
import zipfile
import logging
import StringIO

from django.utils.translation import ugettext_lazy as _
from django.forms.formsets import formset_factory
from django.forms import ValidationError
from django.http import HttpResponseRedirect
from django.conf import settings

from zeus.election_modules import ElectionModuleBase, election_module
from zeus.views.utils import set_menu

from helios.view_utils import render_template
from zeus.core import gamma_decode, to_absolute_answers
from zeus.election_modules.preference.schulze import count as schulze_count

@election_module
class PreferencesElection(ElectionModuleBase):

    def get_count_method(self):
        return schulze_count

    module_id = 'preference'
    description = _('Preferential voting')
    messages = {
        'answer_title': _('Candidate'),
        'question_title': _('Candidates List')
    }
    auto_append_answer = True
    count_empty_question = False
    booth_questions_tpl = 'question_preference'
    no_questions_added_message = _('No candidates set')
    manage_questions_title = _('Manage candidates')
    override_question_title = _('Candidates List')
    module_params = {
        'ranked': True
    }

    results_template = "election_modules/preference/election_results.html"

    pdf_result = True
    csv_result = True
    json_result = True

    def questions_update_view(self, request, election, poll):
        from zeus.utils import poll_reverse
        from zeus.forms import PreferencesForm, DEFAULT_ANSWERS_COUNT, \
                MAX_QUESTIONS_LIMIT

        if not poll.questions_data:
            poll.questions_data = [{}]

        initial = poll.questions_data

        extra = 1
        if poll.questions_data:
            extra = 0

        questions_formset = formset_factory(PreferencesForm, extra=extra,
                                            can_delete=True, can_order=True)


        if request.method == 'POST':
            formset = questions_formset(request.POST, initial=initial)

            if formset.is_valid():
                questions_data = []

                for question in formset.cleaned_data:
                    if not question:
                        continue
                    # force sort of answers by extracting index from answer key.
                    # cast answer index to integer, otherwise answer_10 would
                    # be placed before answer_2
                    answer_index = lambda a: int(a[0].replace('answer_', ''))
                    isanswer = lambda a: a[0].startswith('answer_')

                    answer_values = filter(isanswer, question.iteritems())
                    sorted_answers = sorted(answer_values, key=answer_index)

                    answers = [x[1] for x in sorted_answers]

                    question['answers'] = answers
                    for k in question.keys():
                        if k in ['DELETE', 'ORDER']:
                            del question[k]

                    questions_data.append(question)

                poll.questions_data = questions_data
                poll.update_answers()
                poll.logger.info("Poll ballot updated")
                poll.save()

                url = poll_reverse(poll, 'questions')
                return HttpResponseRedirect(url)
        else:
            formset = questions_formset(initial=initial)

        context = {
            'default_answers_count': DEFAULT_ANSWERS_COUNT,
            'formset': formset,
            'max_questions_limit': 1,
            'election': election,
            'poll': poll,
            'module': self
        }
        set_menu('questions', context)

        tpl = 'election_modules/preference/election_poll_questions_manage'
        return render_template(request, tpl, context)

    def update_answers(self):
        answers = []
        questions_data = self.poll.questions_data or []
        prepend_empty_answer = True
        if self.auto_append_answer:
            prepend_empty_answer = True
        for index, q in enumerate(questions_data):
            q_answers = ["%s" % (ans,) for ans in \
                         q['answers']]
            group = index
            if prepend_empty_answer:
                #remove params and q questions
                params_max = len(q_answers)
                params_min = 0
                if self.count_empty_question:
                    params_min = 0
                params = "%d-%d, %d" % (params_min, params_max, group)
                q_answers.insert(0, "%s: %s" % (q.get('question'), params))
            answers = answers + q_answers
        answers = questions_data[0]['answers']
        self.poll._init_questions(len(answers))
        self.poll.questions[0]['answers'] = answers

    def generate_result_docs(self, lang):
        poll_data = [
            (self.poll.name, self.poll.zeus.get_results(), self.poll.questions,
             self.poll.voters.all())
            ]
        from zeus.results_report import build_preferences_doc
        results_json = self.poll.zeus.get_results()
        build_preferences_doc(_(u'Results'), self.election.name,
                    self.election.institution.name,
                    self.election.voting_starts_at, self.election.voting_ends_at,
                    self.election.voting_extended_until,
                    poll_data,
                    lang,
                    self.get_poll_result_file_path('pdf', 'pdf', lang[0]))

    def generate_election_result_docs(self, lang):
        from zeus.results_report import build_preferences_doc
        pdfpath = self.get_election_result_file_path('pdf', 'pdf', lang[0])
        polls_data = []

        for poll in self.election.polls.filter():
            polls_data.append((poll.name, poll.zeus.get_results(), poll.questions, poll.voters.all()))

        build_preferences_doc(_(u'Results'), self.election.name,
            self.election.institution.name,
            self.election.voting_starts_at, self.election.voting_ends_at,
            self.election.voting_extended_until,
            polls_data,
            lang,
            self.get_election_result_file_path('pdf', 'pdf', lang[0]))

    def compute_election_results(self):
        for lang in settings.LANGUAGES:
            self.generate_election_result_docs(lang)
            self.generate_election_csv_file(lang)
            self.generate_election_zip_file(lang)

    def compute_results(self):
        candidates = self.poll.questions_data[0]['answers']
        candidates_count =  len(candidates)
        count_id = 0

        ballots_data = self.poll.result[0]
        ballots = []
        for ballot in ballots_data:
            if not ballot:
                continue

            decoded = gamma_decode(ballot, candidates_count, candidates_count)
            ballot = to_absolute_answers(decoded, candidates_count)
            ballots.append(ballot)

        computed = self.get_count_method()(ballots, range(candidates_count))
        results = {
            'wins_and_beats': computed[1],
            'ballots': ballots,
            'blanks': len(filter(lambda x: len(x) == 0, ballots))
        }

        self.poll.stv_results = json.dumps(results)
        self.poll.save()

        # build docs
        self.generate_json_file()
        for lang in settings.LANGUAGES:
            self.generate_result_docs(lang)
            self.generate_csv_file(lang)

    def get_booth_template(self, request):
        raise NotImplemented
