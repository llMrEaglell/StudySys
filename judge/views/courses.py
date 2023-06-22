import json
from calendar import Calendar, SUNDAY
from collections import defaultdict, namedtuple
from datetime import date, datetime, time, timedelta
from functools import partial
from itertools import chain
from operator import attrgetter, itemgetter

from django import forms
from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import ImproperlyConfigured, ObjectDoesNotExist
from django.db import IntegrityError
from django.db.models import BooleanField, Case, Count, F, FloatField, IntegerField, Max, Min, Q, Sum, Value, When
from django.db.models.expressions import CombinedExpression, Exists, OuterRef
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseRedirect
from django.shortcuts import get_object_or_404, render
from django.template.defaultfilters import date as date_filter
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.timezone import make_aware
from judge.utils.ranker import ranker
from django.utils.translation import gettext as _, gettext_lazy
from django.views.generic import ListView, TemplateView
from django.views.generic.detail import DetailView, SingleObjectMixin, View
from django.views.generic.list import BaseListView
from icalendar import Calendar as ICalendar, Event
from reversion import revisions

from judge import event_poster as event
from judge.comments import CommentedDetailView
from judge.forms import CourseCloneForm
from judge.models import Submission, Problem, Profile
from judge.models.course import Course, CourseParticipation, CourseProblem, CourseTheory, CourseTest, \
    CourseTag
from judge.utils.opengraph import generate_opengraph
from judge.utils.problems import _get_result_data
from judge.utils.stats import get_bar_chart, get_pie_chart
from judge.utils.views import DiggPaginatorMixin, QueryStringSortMixin, SingleObjectFormView, TitleMixin, \
    generic_message

__all__ = ['CourseList', 'CourseDetail', 'CourseJoin', 'CourseLeave', 'CourseCalendar',
           'CourseClone', 'CourseStats', 'CourseParticipationDisqualify', 'course_ranking_ajax',
           'CourseParticipationList', 'get_course_ranking_list',
           'base_course_ranking_list']


def _find_course(request, key, private_check=True):
    try:
        course = Course.objects.get(key=key)
        if private_check and not course.is_accessible_by(request.user):
            raise ObjectDoesNotExist()
    except ObjectDoesNotExist:
        return generic_message(request, _('No such course'),
                               _('Could not find a course with the key "%s".') % key, status=404), False
    return course, True


class CourseListMixin(object):
    def get_queryset(self):
        return Course.get_visible_courses(self.request.user)


class CourseList(QueryStringSortMixin, DiggPaginatorMixin, TitleMixin, CourseListMixin, ListView):
    model = Course
    paginate_by = 20
    template_name = 'courses/list.html'
    title = gettext_lazy('Courses')
    context_object_name = 'past_courses'
    all_sorts = frozenset(('name', 'user_count', 'start_time'))
    default_desc = frozenset(('name', 'user_count'))
    default_sort = '-start_time'

    @cached_property
    def _now(self):
        return timezone.now()

    def _get_queryset(self):
        queryset = super().get_queryset().prefetch_related(
            'tags',
            'organizations',
            'authors',
            'curators',
            'testers',
            'spectators',
            'classes',
        )

        profile = self.request.profile
        if not profile:
            return queryset

        return queryset.annotate(
            editor_or_tester=Exists(Course.authors.through.objects.filter(course=OuterRef('pk'), profile=profile))
            .bitor(Exists(Course.curators.through.objects.filter(course=OuterRef('pk'), profile=profile)))
            .bitor(Exists(Course.testers.through.objects.filter(course=OuterRef('pk'), profile=profile))),
            completed_course=Exists(CourseParticipation.objects.filter(course=OuterRef('pk'), user=profile,
                                                                       virtual=CourseParticipation.LIVE)),
        )

    def get_queryset(self):
        return self._get_queryset().order_by(self.order, 'key').filter(end_time__lt=self._now)

    def get_paginator(self, queryset, per_page, orphans=0, allow_empty_first_page=True, **kwargs):
        return super().get_paginator(queryset, per_page, orphans, allow_empty_first_page,
                                     count=self.get_queryset().values('id').count(), **kwargs)

    def get_context_data(self, **kwargs):
        context = super(CourseList, self).get_context_data(**kwargs)
        present, active, future = [], [], []
        finished = set()
        for course in self._get_queryset().exclude(end_time__lt=self._now):
            if course.start_time > self._now:
                future.append(course)
            else:
                present.append(course)

        if self.request.user.is_authenticated:
            for participation in (
                    CourseParticipation.objects.filter(virtual=0, user=self.request.profile, course_id__in=present)
                            .select_related('course')
                            .prefetch_related('course__authors', 'course__curators', 'course__testers',
                                              'course__spectators')
                            .annotate(key=F('course__key'))
            ):
                if participation.ended:
                    finished.add(participation.course.key)
                else:
                    active.append(participation)
                    present.remove(participation.course)

        active.sort(key=attrgetter('end_time', 'key'))
        present.sort(key=attrgetter('end_time', 'key'))
        future.sort(key=attrgetter('start_time'))
        context['active_participations'] = active
        context['current_courses'] = present
        context['future_courses'] = future
        context['finished_courses'] = finished
        context['now'] = self._now
        context['first_page_href'] = '.'
        context['page_suffix'] = '#past-courses'
        context.update(self.get_sort_context())
        context.update(self.get_sort_paginate_context())
        return context


class PrivateCourseError(Exception):
    def __init__(self, name, is_private, is_organization_private, orgs, classes):
        self.name = name
        self.is_private = is_private
        self.is_organization_private = is_organization_private
        self.orgs = orgs
        self.classes = classes


class CourseMixin(object):
    context_object_name = 'course'
    model = Course
    slug_field = 'key'
    slug_url_kwarg = 'course'

    @cached_property
    def is_editor(self):
        if not self.request.user.is_authenticated:
            return False
        return self.request.profile.id in self.object.editor_ids

    @cached_property
    def is_tester(self):
        if not self.request.user.is_authenticated:
            return False
        return self.request.profile.id in self.object.tester_ids

    @cached_property
    def is_spectator(self):
        if not self.request.user.is_authenticated:
            return False
        return self.request.profile.id in self.object.spectator_ids

    @cached_property
    def can_edit(self):
        return self.object.is_editable_by(self.request.user)

    def get_context_data(self, **kwargs):
        context = super(CourseMixin, self).get_context_data(**kwargs)
        if self.request.user.is_authenticated:
            try:
                context['live_participation'] = (
                    self.request.profile.course_history.get(
                        course=self.object,
                        virtual=CourseParticipation.LIVE,
                    )
                )
            except CourseParticipation.DoesNotExist:
                context['live_participation'] = None
                context['has_joined'] = False
            else:
                context['has_joined'] = True
        else:
            context['live_participation'] = None
            context['has_joined'] = False

        context['now'] = timezone.now()
        context['is_editor'] = self.is_editor
        context['is_tester'] = self.is_tester
        context['is_spectator'] = self.is_spectator
        context['can_edit'] = self.can_edit

        if not self.object.og_image or not self.object.summary:
            metadata = generate_opengraph('generated-meta-course:%d' % self.object.id,
                                          self.object.description, 'course')
        context['meta_description'] = self.object.summary or metadata[0]
        context['og_image'] = self.object.og_image or metadata[1]
        context['has_moss_api_key'] = settings.MOSS_API_KEY is not None
        context['logo_override_image'] = self.object.logo_override_image
        if not context['logo_override_image'] and self.object.organizations.count() == 1:
            context['logo_override_image'] = self.object.organizations.first().logo_override_image

        return context

    def get_object(self, queryset=None):
        course = super(CourseMixin, self).get_object(queryset)

        profile = self.request.profile
        if (profile is not None and
                CourseParticipation.objects.filter(id=profile.current_course_id, course_id=course.id).exists()):
            return course

        try:
            course.access_check(self.request.user)
        except Course.PrivateCourse:
            raise PrivateCourseError(course.name, course.is_private, course.is_organization_private,
                                     course.organizations.all(), course.classes.all())
        except Course.Inaccessible:
            raise Http404()
        else:
            return course

    def dispatch(self, request, *args, **kwargs):
        try:
            return super(CourseMixin, self).dispatch(request, *args, **kwargs)
        except Http404:
            key = kwargs.get(self.slug_url_kwarg, None)
            if key:
                return generic_message(request, _('No such course'),
                                       _('Could not find a course with the key "%s".') % key)
            else:
                return generic_message(request, _('No such course'),
                                       _('Could not find such course.'))
        except PrivateCourseError as e:
            return render(request, 'courses/private.html', {
                'error': e, 'title': _('Access to course "%s" denied') % e.name,
            }, status=403)


class CourseDetail(CourseMixin, TitleMixin, CommentedDetailView):
    template_name = 'courses/course.html'

    def get_comment_page(self):
        return 'c:%s' % self.object.key

    def get_title(self):
        return self.object.name

    def get_context_data(self, **kwargs):
        context = super(CourseDetail, self).get_context_data(**kwargs)
        # TODO check this
        context['course_problems'] = Problem.objects.filter(courses__course=self.object) \
            .order_by('courses__order').defer('description') \
            .annotate(has_public_editorial=Case(
            When(solution__is_public=True, solution__publish_on__lte=timezone.now(), then=True),
            default=False,
            output_field=BooleanField(),
        )) \
            .add_i18n_name(self.request.LANGUAGE_CODE)
        context['metadata'] = {
            'has_public_editorials': any(
                problem.is_public and problem.has_public_editorial for problem in context['course_problems']
            ),
        }
        context['metadata'].update(
            **self.object.course_problems
            .annotate(
                partials_enabled=F('partial').bitand(F('problem__partial')),
                pretests_enabled=F('is_pretested').bitand(F('course__run_pretests_only')),
            )
            .aggregate(
                has_partials=Sum('partials_enabled'),
                has_pretests=Sum('pretests_enabled'),
                has_submission_cap=Sum('max_submissions'),
                problem_count=Count('id'),
            ),
        )
        return context


class CourseClone(CourseMixin, PermissionRequiredMixin, TitleMixin, SingleObjectFormView):
    title = gettext_lazy('Clone Course')
    template_name = 'courses/clone.html'
    form_class = CourseCloneForm
    permission_required = 'judge.clone_course'

    def form_valid(self, form):
        course = self.object

        tags = course.tags.all()
        organizations = course.organizations.all()
        private_members = course.private_members.all()
        view_course_scoreboard = course.view_course_scoreboard.all()
        course_problems = course.course_problems.all()
        # I add it
        course_theory = course.theory.all()
        course_tests = course.tests.all()

        old_key = course.key

        course.pk = None
        course.is_visible = False
        course.user_count = 0
        course.locked_after = None
        course.key = form.cleaned_data['key']
        with revisions.create_revision(atomic=True):
            course.save()
            course.tags.set(tags)
            course.organizations.set(organizations)
            course.private_members.set(private_members)
            course.view_course_scoreboard.set(view_course_scoreboard)
            course.authors.add(self.request.profile)

            for problem in course_problems:
                problem.course = course
                problem.pk = None

            for theory in course_theory:
                theory.course = course
                theory.pk = None

            for test in course_tests:
                test.course = course
                test.pk = None

            CourseProblem.objects.bulk_create(course_problems)
            CourseTheory.objects.bulk_create(course_theory)  # TODO dont remember implemt it
            CourseTest.objects.bulk_create(course_tests)

            revisions.set_user(self.request.user)
            revisions.set_comment(_('Cloned course from %s') % old_key)

        return HttpResponseRedirect(reverse('admin:judge_course_change', args=(course.id,)))


class CourseAccessDenied(Exception):
    pass


class CourseAccessCodeForm(forms.Form):
    access_code = forms.CharField(max_length=255)

    def __init__(self, *args, **kwargs):
        super(CourseAccessCodeForm, self).__init__(*args, **kwargs)
        self.fields['access_code'].widget.attrs.update({'autocomplete': 'off'})


class CourseJoin(LoginRequiredMixin, CourseMixin, SingleObjectMixin, View):
    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        return self.ask_for_access_code()

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        try:
            return self.join_course(request)
        except CourseAccessDenied:
            if request.POST.get('access_code'):
                return self.ask_for_access_code(CourseAccessCodeForm(request.POST))
            else:
                return HttpResponseRedirect(request.path)

    def join_course(self, request, access_code=None):
        course = self.object

        if not course.started and not (self.is_editor or self.is_tester):
            return generic_message(request, _('Course not ongoing'),
                                   _('"%s" is not currently ongoing.') % course.name)

        profile = request.profile

        if not request.user.is_superuser and course.banned_users.filter(id=profile.id).exists():
            return generic_message(request, _('Banned from joining'),
                                   _('You have been declared persona non grata for this course. '
                                     'You are permanently barred from joining this course.'))

        requires_access_code = (not self.can_edit and course.access_code and access_code != course.access_code)
        if course.ended:
            if requires_access_code:
                raise CourseAccessDenied()

            while True:
                virtual_id = max((CourseParticipation.objects.filter(course=course, user=profile)
                                  .aggregate(virtual_id=Max('virtual'))['virtual_id'] or 0) + 1, 1)
                try:
                    participation = CourseParticipation.objects.create(
                        course=course, user=profile, virtual=virtual_id,
                        real_start=timezone.now(),
                    )
                # There is obviously a race condition here, so we keep trying until we win the race.
                except IntegrityError:
                    pass
                else:
                    break
        else:
            SPECTATE = CourseParticipation.SPECTATE
            LIVE = CourseParticipation.LIVE

            if course.is_live_joinable_by(request.user):
                participation_type = LIVE
            elif course.is_spectatable_by(request.user):
                participation_type = SPECTATE
            else:
                return generic_message(request, _('Cannot enter'),
                                       _('You are not able to join this course.'))
            try:
                participation = CourseParticipation.objects.get(
                    course=course, user=profile, virtual=participation_type,
                )
            except CourseParticipation.DoesNotExist:
                if requires_access_code:
                    raise CourseAccessDenied()

                participation = CourseParticipation.objects.create(
                    course=course, user=profile, virtual=participation_type,
                    real_start=timezone.now(),
                )
            else:
                if participation.ended:
                    participation = CourseParticipation.objects.get_or_create(
                        course=course, user=profile, virtual=SPECTATE,
                        defaults={'real_start': timezone.now()},
                    )[0]

        profile.current_course = participation
        profile.save()
        course._updating_stats_only = True
        course.update_user_count()
        return HttpResponseRedirect(reverse('problem_list'))

    def ask_for_access_code(self, form=None):
        course = self.object
        wrong_code = False
        if form:
            if form.is_valid():
                if form.cleaned_data['access_code'] == course.access_code:
                    return self.join_course(self.request, form.cleaned_data['access_code'])
                wrong_code = True
        else:
            form = CourseAccessCodeForm()
        return render(self.request, 'courses/access_code.html', {
            'form': form, 'wrong_code': wrong_code,
            'title': _('Enter access code for "%s"') % course.name,
        })


class CourseLeave(LoginRequiredMixin, CourseMixin, SingleObjectMixin, View):
    def post(self, request, *args, **kwargs):
        course = self.get_object()

        profile = request.profile
        if profile.current_course is None or profile.current_course.course_id != course.id:
            return generic_message(request, _('No such course'),
                                   _('You are not in course "%s".') % course.key, 404)

        profile.remove_course()
        return HttpResponseRedirect(reverse('course_view', args=(course.key,)))


CourseDay = namedtuple('CourseDay', 'date is_pad is_today starts ends oneday')


class CourseCalendar(TitleMixin, CourseListMixin, TemplateView):
    firstweekday = SUNDAY
    template_name = 'courses/calendar.html'

    def get(self, request, *args, **kwargs):
        try:
            self.year = int(kwargs['year'])
            self.month = int(kwargs['month'])
        except (KeyError, ValueError):
            raise ImproperlyConfigured('CourseCalendar requires integer year and month')
        self.today = timezone.now().date()
        return self.render()

    def render(self):
        context = self.get_context_data()
        return self.render_to_response(context)

    def get_course_data(self, start, end):
        end += timedelta(days=1)
        courses = self.get_queryset().filter(Q(start_time__gte=start, start_time__lt=end) |
                                             Q(end_time__gte=start, end_time__lt=end))
        starts, ends, oneday = (defaultdict(list) for i in range(3))
        for course in courses:
            start_date = timezone.localtime(course.start_time).date()
            end_date = timezone.localtime(course.end_time - timedelta(seconds=1)).date()
            if start_date == end_date:
                oneday[start_date].append(course)
            else:
                starts[start_date].append(course)
                ends[end_date].append(course)
        return starts, ends, oneday

    def get_table(self):
        calendar = Calendar(self.firstweekday).monthdatescalendar(self.year, self.month)
        starts, ends, oneday = self.get_course_data(make_aware(datetime.combine(calendar[0][0], time.min)),
                                                    make_aware(datetime.combine(calendar[-1][-1], time.min)))
        return [[CourseDay(
            date=date, is_pad=date.month != self.month,
            is_today=date == self.today, starts=starts[date], ends=ends[date], oneday=oneday[date],
        ) for date in week] for week in calendar]

    def get_context_data(self, **kwargs):
        context = super(CourseCalendar, self).get_context_data(**kwargs)

        try:
            month = date(self.year, self.month, 1)
        except ValueError:
            raise Http404()
        else:
            context['title'] = _('Courses in %(month)s') % {'month': date_filter(month, _('F Y'))}

        dates = Course.objects.aggregate(min=Min('start_time'), max=Max('end_time'))
        min_month = (self.today.year, self.today.month)
        if dates['min'] is not None:
            min_month = dates['min'].year, dates['min'].month
        max_month = (self.today.year, self.today.month)
        if dates['max'] is not None:
            max_month = max((dates['max'].year, dates['max'].month), (self.today.year, self.today.month))

        month = (self.year, self.month)
        if month < min_month or month > max_month:
            # 404 is valid because it merely declares the lack of existence, without any reason
            raise Http404()

        context['now'] = timezone.now()
        context['calendar'] = self.get_table()
        context['curr_month'] = date(self.year, self.month, 1)

        if month > min_month:
            context['prev_month'] = date(self.year - (self.month == 1), 12 if self.month == 1 else self.month - 1, 1)
        else:
            context['prev_month'] = None

        if month < max_month:
            context['next_month'] = date(self.year + (self.month == 12), 1 if self.month == 12 else self.month + 1, 1)
        else:
            context['next_month'] = None
        return context


class CourseICal(TitleMixin, CourseListMixin, BaseListView):
    def generate_ical(self):
        cal = ICalendar()
        cal.add('prodid', '-//DMOJ//NONSGML Courses Calendar//')
        cal.add('version', '2.0')

        now = timezone.now().astimezone(timezone.utc)
        domain = self.request.get_host()
        for course in self.get_queryset():  # TODO can be problem with it
            event = Event()
            event.add('uid', f'course-{course.key}@{domain}')
            event.add('summary', course.name)
            event.add('location', self.request.build_absolute_uri(course.get_absolute_url()))
            event.add('dtstart', course.start_time.astimezone(timezone.utc))
            event.add('dtend', course.end_time.astimezone(timezone.utc))
            event.add('dtstamp', now)
            cal.add_component(event)
        return cal.to_ical()

    def render_to_response(self, context, **kwargs):
        return HttpResponse(self.generate_ical(), content_type='text/calendar')


class CourseStats(TitleMixin, CourseMixin, DetailView):
    template_name = 'courses/stats.html'

    def get_title(self):
        return _('%s Statistics') % self.object.name

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if not (self.object.ended or self.can_edit):
            raise Http404()

        queryset = Submission.objects.filter(course_object=self.object)

        ac_count = Count(Case(When(result='AC', then=Value(1)), output_field=IntegerField()))
        ac_rate = CombinedExpression(ac_count / Count('problem'), '*', Value(100.0), output_field=FloatField())

        status_count_queryset = list(
            queryset.values('problem__code', 'result').annotate(count=Count('result'))
            .values_list('problem__code', 'result', 'count'),
        )
        labels, codes = [], []
        course_problems = self.object.course_problems.order_by('order').values_list('problem__name', 'problem__code')
        if course_problems:
            labels, codes = zip(*course_problems)
        num_problems = len(labels)
        status_counts = [[] for i in range(num_problems)]
        for problem_code, result, count in status_count_queryset:
            if problem_code in codes:
                status_counts[codes.index(problem_code)].append((result, count))

        result_data = defaultdict(partial(list, [0] * num_problems))
        for i in range(num_problems):
            for category in _get_result_data(defaultdict(int, status_counts[i]))['categories']:
                result_data[category['code']][i] = category['count']

        stats = {
            'problem_status_count': {
                'labels': labels,
                'datasets': [
                    {
                        'label': name,
                        'backgroundColor': settings.DMOJ_STATS_SUBMISSION_RESULT_COLORS[name],
                        'data': data,
                    }
                    for name, data in result_data.items()
                ],
            },
            'problem_ac_rate': get_bar_chart(
                queryset.values('course__problem__order', 'problem__name').annotate(ac_rate=ac_rate)
                .order_by('course__problem__order').values_list('problem__name', 'ac_rate'),
            ),
            'language_count': get_pie_chart(
                queryset.values('language__name').annotate(count=Count('language__name'))
                .filter(count__gt=0).order_by('-count').values_list('language__name', 'count'),
            ),
            'language_ac_rate': get_bar_chart(
                queryset.values('language__name').annotate(ac_rate=ac_rate)
                .filter(ac_rate__gt=0).values_list('language__name', 'ac_rate'),
            ),
        }

        context['stats'] = mark_safe(json.dumps(stats))

        return context


CourseRankingProfile = namedtuple(
    'CourseRankingProfile',
    'id user css_class username points cumtime tiebreaker organization participation '
    'participation_rating problem_cells result_cell display_name',
)

BestSolutionData = namedtuple('BestSolutionData', 'code points time state is_pretested')


def make_course_ranking_profile(course, participation, contest_problems):
    def display_user_problem(course_problem):
        # When the contest format is changed, `format_data` might be invalid.
        # This will cause `display_user_problem` to error, so we display '???' instead.
        try:
            return course.format.display_user_problem(participation, course_problem)
        except (KeyError, TypeError, ValueError):
            return mark_safe('<td>???</td>')

    user = participation.user
    return CourseRankingProfile(
        id=user.id,
        user=user.user,
        css_class=user.css_class,
        username=user.username,
        points=participation.score,
        cumtime=participation.cumtime,
        tiebreaker=participation.tiebreaker,
        organization=user.organization,
        participation_rating=participation.rating.rating if hasattr(participation, 'rating') else None,
        problem_cells=[display_user_problem(contest_problem) for contest_problem in contest_problems],
        result_cell=course.format.display_participation_result(participation),
        participation=participation,
        display_name=user.display_name,
    )


def base_course_ranking_list(course, problems, queryset):
    return [make_course_ranking_profile(course, participation, problems) for participation in
            queryset.select_related('user__user', 'rating').defer('user__about', 'user__organizations__about')]


def course_ranking_list(contest, problems):
    return base_course_ranking_list(contest, problems, contest.users.filter(virtual=0)
                                    .prefetch_related('user__organizations')
                                    .order_by('is_disqualified', '-score', 'cumtime', 'tiebreaker'))


def get_course_ranking_list(request, course, participation=None, ranking_list=course_ranking_list,
                            show_current_virtual=True, ranker=ranker):
    problems = list(course.contest_problems.select_related('problem').defer('problem__description').order_by('order'))

    users = ranker(ranking_list(course, problems), key=attrgetter('points', 'cumtime', 'tiebreaker'))

    if show_current_virtual:
        if participation is None and request.user.is_authenticated:
            participation = request.profile.current_course
            if participation is None or participation.course_id != course.id:
                participation = None
        if participation is not None and participation.virtual:
            users = chain([('-', make_course_ranking_profile(course, participation, problems))], users)
    return users, problems


def course_ranking_ajax(request, course, participation=None):
    course, exists = _find_course(request, course)
    if not exists:
        return HttpResponseBadRequest('Invalid course', content_type='text/plain')

    if not course.can_see_full_scoreboard(request.user):
        raise Http404()

    users, problems = get_course_ranking_list(request, course, participation)
    return render(request, 'courses/ranking-table.html', {
        'users': users,
        'problems': problems,
        'contest': course,
        'has_rating': course.ratings.exists(),
    })


class CourseRankingBase(CourseMixin, TitleMixin, DetailView):
    template_name = 'courses/ranking.html'
    tab = None

    def get_title(self):
        raise NotImplementedError()

    def get_content_title(self):
        return self.object.name

    def get_ranking_list(self):
        raise NotImplementedError()

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)

        if not self.object.can_see_own_scoreboard(self.request.user):
            raise Http404()

        users, problems = self.get_ranking_list()
        context['users'] = users
        context['problems'] = problems
        context['last_msg'] = event.last()
        context['tab'] = self.tab
        return context


class CourseRanking(CourseRankingBase):
    tab = 'ranking'

    def get_title(self):
        return _('%s Rankings') % self.object.name

    def get_ranking_list(self):
        if not self.object.can_see_full_scoreboard(self.request.user):
            queryset = self.object.users.filter(user=self.request.profile, virtual=CourseParticipation.LIVE)
            return get_course_ranking_list(
                self.request, self.object,
                ranking_list=partial(base_course_ranking_list, queryset=queryset),
                ranker=lambda users, key: ((_('???'), user) for user in users),
            )

        return get_course_ranking_list(self.request, self.object)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['has_rating'] = self.object.ratings.exists()
        return context


class CourseParticipationList(LoginRequiredMixin, CourseRankingBase):
    tab = 'participation'

    def get_title(self):
        if self.profile == self.request.profile:
            return _('Your participation in %(course)s') % {'course': self.object.name}
        return _("%(user)s's participation in %(course)s") % {
            'user': self.profile.username, 'course': self.object.name,
        }

    def get_ranking_list(self):
        if not self.object.can_see_full_scoreboard(self.request.user) and self.profile != self.request.profile:
            raise Http404()

        queryset = self.object.users.filter(user=self.profile, virtual__gte=0).order_by('-virtual')
        live_link = format_html('<a href="{2}#!{1}">{0}</a>', _('Live'), self.profile.username,
                                reverse('course_ranking', args=[self.object.key]))

        return get_course_ranking_list(
            self.request, self.object, show_current_virtual=False,
            ranking_list=partial(base_course_ranking_list, queryset=queryset),
            ranker=lambda users, key: ((user.participation.virtual or live_link, user) for user in users))

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context['has_rating'] = False
        context['now'] = timezone.now()
        context['rank_header'] = _('Participation')
        return context

    def get(self, request, *args, **kwargs):
        if 'user' in kwargs:
            self.profile = get_object_or_404(Profile, user__username=kwargs['user'])
        else:
            self.profile = self.request.profile
        return super().get(request, *args, **kwargs)


class CourseParticipationDisqualify(CourseMixin, SingleObjectMixin, View):
    def get_object(self, queryset=None):
        course = super().get_object(queryset)
        if not course.is_editable_by(self.request.user):
            raise Http404()
        return course

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()

        try:
            participation = self.object.users.get(pk=request.POST.get('participation'))
        except ObjectDoesNotExist:
            pass
        else:
            participation.set_disqualified(not participation.is_disqualified)
        return HttpResponseRedirect(reverse('course_ranking', args=(self.object.key,)))


class CourseTagDetailAjax(DetailView):
    model = CourseTag
    slug_field = slug_url_kwarg = 'name'
    context_object_name = 'tag'
    template_name = 'courses/tag-ajax.html'


class CourseTagDetail(TitleMixin, CourseTagDetailAjax):
    template_name = 'courses/tag.html'

    def get_title(self):
        return _('Course tag: %s') % self.object.name
