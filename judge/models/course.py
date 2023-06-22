from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models, transaction
from django.db.models import CASCADE, Q
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.translation import gettext_lazy as _
from jsonfield import JSONField
from lupa import LuaRuntime

from judge import contest_format
from judge.models.problem import Problem
from judge.models.profile import Class, Organization, Profile
from judge.models.submission import Submission
from judge.ratings import rate_course

__all__ = ['Course', 'CourseTag', 'CourseParticipation', 'CourseProblem', 'CourseSubmission', 'TheoryPost', 'TheoryPostGroup', 'CourseRating']


class MinValueOrNoneValidator(MinValueValidator):
    def compare(self, a, b):
        return a is not None and b is not None and super().compare(a, b)


class CourseTag(models.Model):
    color_validator = RegexValidator('^#(?:[A-Fa-f0-9]{3}){1,2}$', _('Invalid colour.'))

    name = models.CharField(max_length=20, verbose_name=_('tag name'), unique=True,
                            validators=[RegexValidator(r'^[a-z-]+$', message=_('Lowercase letters and hyphens only.'))])
    color = models.CharField(max_length=7, verbose_name=_('tag colour'), validators=[color_validator])
    description = models.TextField(verbose_name=_('tag description'), blank=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('course_tag', args=[self.name])

    @property
    def text_color(self, cache={}):
        if self.color not in cache:
            if len(self.color) == 4:
                r, g, b = [ord(bytes.fromhex(i * 2)) for i in self.color[1:]]
            else:
                r, g, b = [i for i in bytes.fromhex(self.color[1:])]
            cache[self.color] = '#000' if 299 * r + 587 * g + 144 * b > 140000 else '#fff'
        return cache[self.color]

    class Meta:
        verbose_name = _('course tag')
        verbose_name_plural = _('course tags')


class TestPost(models.Model):
    title = models.CharField(verbose_name=_('test title'), max_length=100)
    # <iframe src="https://docs.google.com/forms/d/e/1FAIpQLSc22xFNhH_BweIoXR30kacF09azZkPPBftMj3jDG_0gx7NrXg/viewform?embedded=true" width="640" height="382" frameborder="0" marginheight="0" marginwidth="0">Завантаження…</iframe>
    # https://docs.google.com/forms/d/e/1FAIpQLSc22xFNhH_BweIoXR30kacF09azZkPPBftMj3jDG_0gx7NrXg/viewform?usp=sf_link
    form = models.URLField(verbose_name=_('form'))
    # regex for get form id(group 5): (https):\/\/([\w_-]+(?:(?:\.[\w_-]+)+))([\w.,@?^=%&:\/~+#-]*[\w@?^=%&\/~+#-])(/forms/d/e/)([\w.,@?^=%&:\/~+#-]*[\w@?^=%&\/~+#-])\/(viewform)
    authors = models.ManyToManyField(Profile, verbose_name=_('authors'), blank=True)

    def is_editable_by(self, user):
        if not user.is_authenticated:
            return False
        if user.has_perm('judge.edit_all_post'):
            return True
        return user.has_perm('judge.change_blogpost') and self.authors.filter(id=user.profile.id).exists()

    class Meta:
        permissions = (
            ('edit_all_post', _('Edit all posts')),
            ('change_post_visibility', _('Edit post visibility')),
        )
        verbose_name = _('test post')
        verbose_name_plural = _('test posts')


class TheoryPost(models.Model):
    title = models.CharField(verbose_name=_('theory title'), max_length=100)
    authors = models.ManyToManyField(Profile, verbose_name=_('authors'), blank=True)
    slug = models.SlugField(verbose_name=_('slug'))
    visible = models.BooleanField(verbose_name=_('public visibility'), default=False)
    sticky = models.BooleanField(verbose_name=_('sticky'), default=False)
    publish_on = models.DateTimeField(verbose_name=_('publish after'))
    content = models.TextField(verbose_name=_('theory content'))
    summary = models.TextField(verbose_name=_('theory summary'), blank=True)
    og_image = models.CharField(verbose_name=_('OpenGraph image'), default='', max_length=150, blank=True)

    @classmethod
    def get_public_posts(cls, user):
        return cls.objects.filter(visible=True).defer('content')

    def __str__(self):
        return self.title

    def get_absolute_url(self):
        return reverse('theory_post', args=(self.id, self.slug))

    def can_see(self, user):
        if self.visible and self.publish_on <= timezone.now():
            return True
        return self.is_editable_by(user)

    def is_editable_by(self, user):
        if not user.is_authenticated:
            return False
        if user.has_perm('judge.edit_all_post'):
            return True
        return user.has_perm('judge.change_blogpost') and self.authors.filter(id=user.profile.id).exists()

    class Meta:
        permissions = (
            ('edit_all_post', _('Edit all posts')),
            ('change_post_visibility', _('Edit post visibility')),
        )
        verbose_name = _('theory post')
        verbose_name_plural = _('theory posts')


class Course(models.Model):
    SCOREBOARD_VISIBLE = 'V'
    SCOREBOARD_AFTER_CONTEST = 'C'
    SCOREBOARD_AFTER_PARTICIPATION = 'P'
    SCOREBOARD_HIDDEN = 'H'
    SCOREBOARD_VISIBILITY = (
        (SCOREBOARD_VISIBLE, _('Visible')),
        (SCOREBOARD_AFTER_CONTEST, _('Hidden for duration of contest')),
        (SCOREBOARD_AFTER_PARTICIPATION, _('Hidden for duration of participation')),
        (SCOREBOARD_HIDDEN, _('Hidden permanently')),
    )
    key = models.CharField(max_length=20, verbose_name=_('course id'), unique=True,
                           validators=[RegexValidator('^[a-z0-9]+$', _('Course id must be ^[a-z0-9]+$'))])
    name = models.CharField(max_length=100, verbose_name=_('course name'), db_index=True)
    authors = models.ManyToManyField(Profile, verbose_name=_('authors'),
                                     help_text=_('These users will be able to edit the course.'),
                                     related_name='authored_courses')
    curators = models.ManyToManyField(Profile, verbose_name=_('curators'),
                                      help_text=_('These users will be able to edit the course, '
                                                  'but will not be listed as authors.'),
                                      related_name='curated_courses', blank=True)
    testers = models.ManyToManyField(Profile, verbose_name=_('testers'),
                                     help_text=_('These users will be able to view the course, but not edit it.'),
                                     blank=True, related_name='tested_courses')
    tester_see_scoreboard = models.BooleanField(verbose_name=_('testers see scoreboard'), default=False,
                                                help_text=_('If testers can see the scoreboard.'))
    tester_see_submissions = models.BooleanField(verbose_name=_('testers see submissions'), default=False,
                                                 help_text=_('If testers can see in-course submissions.'))
    spectators = models.ManyToManyField(Profile, verbose_name=_('spectators'),
                                        help_text=_('These users will be able to spectate the course, '
                                                    'but not see the problems ahead of time.'),
                                        blank=True, related_name='spectated_courses')
    description = models.TextField(verbose_name=_('description'), blank=True)
    # Linked items
    problems = models.ManyToManyField(Problem, verbose_name=_('problems'), through='CourseProblem') # Changed through

    theory = models.ManyToManyField(TheoryPost, verbose_name=_('theory'), through='CourseTheory') #I add it

    tests = models.ManyToManyField(TestPost, verbose_name=_('tests'), through='CourseTest') #I add it

    start_time = models.DateTimeField(verbose_name=_('start time'), db_index=True)
    end_time = models.DateTimeField(verbose_name=_('end time'), db_index=True)

    time_limit = models.DurationField(verbose_name=_('time limit'), blank=True, null=True)
    is_visible = models.BooleanField(verbose_name=_('publicly visible'), default=False,
                                     help_text=_('Should be set even for organization-private courses, where it '
                                                 'determines whether the course is visible to members of the '
                                                 'specified organizations.'))

    is_rated = models.BooleanField(verbose_name=_('course rated'), help_text=_('Whether this course can be rated.'),
                                   default=False)

    view_course_scoreboard = models.ManyToManyField(Profile, verbose_name=_('view course scoreboard'), blank=True,
                                                     related_name='view_course_scoreboard',
                                                     help_text=_('These users will be able to view the scoreboard.'))
    view_course_submissions = models.ManyToManyField(Profile, verbose_name=_('can see course submissions'),
                                                      blank=True, related_name='view_course_submissions',
                                                      help_text=_('These users will be able '
                                                                  'to see in-course submissions.'))
    scoreboard_visibility = models.CharField(verbose_name=_('scoreboard visibility'), default=SCOREBOARD_VISIBLE,
                                             help_text=_('Scoreboard visibility through the duration of the course.'),
                                             max_length=1, choices=SCOREBOARD_VISIBILITY)
    use_clarifications = models.BooleanField(verbose_name=_('no comments'),
                                             help_text=_('Use clarification system instead of comments.'),
                                             default=True)

    rating_floor = models.IntegerField(verbose_name=_('rating floor'),
                                       help_text=_('Do not rate users who have a lower rating.'), null=True, blank=True)
    rating_ceiling = models.IntegerField(verbose_name=_('rating ceiling'),
                                         help_text=_('Do not rate users who have a higher rating.'),
                                         null=True, blank=True)
    rate_all = models.BooleanField(verbose_name=_('rate all'), help_text=_('Rate all users who joined.'), default=False)
    rate_exclude = models.ManyToManyField(Profile, verbose_name=_('exclude from ratings'), blank=True,
                                          related_name='rate_exclude+')

    is_private = models.BooleanField(verbose_name=_('private to specific users'), default=False)
    private_members = models.ManyToManyField(Profile, blank=True, verbose_name=_('private members'),
                                                 help_text=_('If non-empty, only these users may see the course.'),
                                                 related_name='private_members+')
    hide_problem_tags = models.BooleanField(verbose_name=_('hide problem tags'),
                                            help_text=_('Whether problem tags should be hidden by default.'),
                                            default=False)
    hide_problem_authors = models.BooleanField(verbose_name=_('hide problem authors'),
                                               help_text=_('Whether problem authors should be hidden by default.'),
                                               default=False)
    run_pretests_only = models.BooleanField(verbose_name=_('run pretests only'),
                                            help_text=_('Whether judges should grade pretests only, versus all '
                                                        'testcases. Commonly set during a course, then unset '
                                                        'prior to rejudging user submissions when the course ends.'),
                                            default=False)
    show_short_display = models.BooleanField(verbose_name=_('show short form settings display'),
                                             help_text=_('Whether to show a section containing course settings '
                                                         'on the course page or not.'),
                                             default=False)
    is_organization_private = models.BooleanField(verbose_name=_('private to organizations'), default=False)
    organizations = models.ManyToManyField(Organization, blank=True, verbose_name=_('organizations'),
                                           help_text=_('If non-empty, only these organizations may see the course.'))
    limit_join_organizations = models.BooleanField(verbose_name=_('limit organizations that can join'), default=False)
    join_organizations = models.ManyToManyField(Organization, blank=True, verbose_name=_('join organizations'),
                                                help_text=_('If non-empty, only these organizations may join '
                                                            'the course.'), related_name='join_only_courses')
    classes = models.ManyToManyField(Class, blank=True, verbose_name=_('classes'),
                                     help_text=_('If organization private, only these classes may see the course.'))
    og_image = models.CharField(verbose_name=_('OpenGraph image'), default='', max_length=150, blank=True)
    logo_override_image = models.CharField(verbose_name=_('logo override image'), default='', max_length=150,
                                           blank=True,
                                           help_text=_('This image will replace the default site logo for users '
                                                       'inside the course.'))
    tags = models.ManyToManyField(CourseTag, verbose_name=_('course tags'), blank=True, related_name='courses')
    user_count = models.IntegerField(verbose_name=_('the amount of live participants'), default=0)
    summary = models.TextField(blank=True, verbose_name=_('course summary'),
                               help_text=_('Plain-text, shown in meta description tag, e.g. for social media.'))
    access_code = models.CharField(verbose_name=_('access code'), blank=True, default='', max_length=255,
                                   help_text=_('An optional code to prompt course before they are allowed '
                                               'to join the course. Leave it blank to disable.'))
    banned_users = models.ManyToManyField(Profile, verbose_name=_('personae non gratae'), blank=True,
                                          help_text=_('Bans the selected users from joining this course.'))
    format_name = models.CharField(verbose_name=_('contest format'), default='default', max_length=32,
                                   choices=contest_format.choices(), help_text=_('The contest format module to use.'))
    format_config = JSONField(verbose_name=_('contest format configuration'), null=True, blank=True,
                              help_text=_('A JSON object to serve as the configuration for the chosen contest format '
                                          'module. Leave empty to use None. Exact format depends on the contest format '
                                          'selected.'))
    problem_label_script = models.TextField(verbose_name=_('contest problem label script'), blank=True,
                                            help_text=_('A custom Lua function to generate problem labels. Requires a '
                                                        'single function with an integer parameter, the zero-indexed '
                                                        'contest problem index, and returns a string, the label.'))
    locked_after = models.DateTimeField(verbose_name=_('contest lock'), null=True, blank=True,
                                        help_text=_('Prevent submissions from this contest '
                                                    'from being rejudged after this date.'))
    points_precision = models.IntegerField(verbose_name=_('precision points'), default=3,
                                           validators=[MinValueValidator(0), MaxValueValidator(10)],
                                           help_text=_('Number of digits to round points to.'))

    @cached_property
    def format_class(self):
        return contest_format.formats[self.format_name]

    @cached_property
    def format(self):
        return self.format_class(self, self.format_config)

    @cached_property
    def get_label_for_problem(self):
        if not self.problem_label_script:
            return self.format.get_label_for_problem

        def DENY_ALL(obj, attr_name, is_setting):
            raise AttributeError()
        lua = LuaRuntime(attribute_filter=DENY_ALL, register_eval=False, register_builtins=False)
        return lua.eval(self.problem_label_script)

    def clean(self):
        # Django will complain if you didn't fill in start_time or end_time, so we don't have to.
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError('What is this? A course that ended before it starts?')
        self.format_class.validate(self.format_config)

        try:
            # a contest should have at least one problem, with contest problem index 0
            # so test it to see if the script returns a valid label.
            label = self.get_label_for_problem(0)
        except Exception as e:
            raise ValidationError('Course problem label script: %s' % e)
        else:
            if not isinstance(label, str):
                raise ValidationError('Course problem label script: script should return a string.')

    def is_in_course(self, user):
        if user.is_authenticated:
            profile = user.profile
            return profile and profile.current_course is not None and profile.current_course.course == self # TOOD add functinality about current course
        return False

    def can_see_own_scoreboard(self, user):
        if self.can_see_full_scoreboard(user):
            return True
        if not self.started:
            return False
        if not self.show_scoreboard and not self.is_in_course(user) and not self.has_completed_course(user):
            return False
        return True

    def can_see_full_scoreboard(self, user):
        if self.show_scoreboard:
            return True
        if not user.is_authenticated:
            return False
        if user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest'):
            return True
        if user.profile.id in self.editor_ids:
            return True
        if self.tester_see_scoreboard and user.profile.id in self.tester_ids:
            return True
        if self.started and user.profile.id in self.spectator_ids:
            return True
        if self.view_course_scoreboard.filter(id=user.profile.id).exists():
            return True
        if self.scoreboard_visibility == self.SCOREBOARD_AFTER_PARTICIPATION and self.has_completed_course(user):
            return True
        return False

    def has_completed_course(self, user):
        if user.is_authenticated:
            participation = self.users.filter(virtual=CourseParticipation.LIVE, user=user.profile).first()
            if participation and participation.ended:
                return True
        return False

    @cached_property
    def show_scoreboard(self):
        if not self.started:
            return False
        if (self.scoreboard_visibility in (self.SCOREBOARD_AFTER_CONTEST, self.SCOREBOARD_AFTER_PARTICIPATION) and
                not self.ended):
            return False
        return self.scoreboard_visibility != self.SCOREBOARD_HIDDEN

    @property
    def course_window_length(self):
        return self.end_time - self.start_time

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @cached_property
    def started(self):
        return self.start_time <= self._now

    @property
    def time_before_start(self):
        if self.start_time >= self._now:
            return self.start_time - self._now
        else:
            return None

    @property
    def time_before_end(self):
        if self.end_time >= self._now:
            return self.end_time - self._now
        else:
            return None

    @cached_property
    def ended(self):
        return self.end_time < self._now

    @cached_property
    def author_ids(self):
        return Course.authors.through.objects.filter(course=self).values_list('profile_id', flat=True)

    @cached_property
    def editor_ids(self):
        return self.author_ids.union(
            Course.curators.through.objects.filter(course=self).values_list('profile_id', flat=True))

    @cached_property
    def tester_ids(self):
        return Course.testers.through.objects.filter(course=self).values_list('profile_id', flat=True)

    @cached_property
    def spectator_ids(self):
        return Course.spectators.through.objects.filter(course=self).values_list('profile_id', flat=True)

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('course_view', args=(self.key,))

    def update_user_count(self):
        self.user_count = self.users.filter(virtual=0).count()
        self.save()

    update_user_count.alters_data = True

    class Inaccessible(Exception):
        pass

    class PrivateCourse(Exception):
        pass

    def access_check(self, user):
        # Do unauthenticated check here so we can skip authentication checks later on.
        if not user.is_authenticated:
            # Unauthenticated users can only see visible, non-private contests
            if not self.is_visible:
                raise self.Inaccessible()
            if self.is_private or self.is_organization_private:
                raise self.PrivateCourse()
            return

        # If the user can view or edit all contests
        if user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest'):
            return

        # User is organizer or curator for contest
        if user.profile.id in self.editor_ids:
            return

        # User is tester for contest
        if user.profile.id in self.tester_ids:
            return

        # User is spectator for contest
        if user.profile.id in self.spectator_ids:
            return

        # Contest is not publicly visible
        if not self.is_visible:
            raise self.Inaccessible()

        # Contest is not private
        if not self.is_private and not self.is_organization_private:
            return

        if self.view_course_scoreboard.filter(id=user.profile.id).exists():
            return

        in_org = (self.organizations.filter(id__in=user.profile.organizations.all()).exists() or
                  self.classes.filter(id__in=user.profile.classes.all()).exists())
        in_users = self.private_members.filter(id=user.profile.id).exists()

        if not self.is_private and self.is_organization_private:
            if in_org:
                return
            raise self.PrivateCourse()

        if self.is_private and not self.is_organization_private:
            if in_users:
                return
            raise self.PrivateCourse()

        if self.is_private and self.is_organization_private:
            if in_org and in_users:
                return
            raise self.PrivateCourse()

    # Assumes the user can access, to avoid the cost again
    def is_live_joinable_by(self, user):
        if not self.started:
            return False

        if not user.is_authenticated:
            return False

        # Can be populated using annotate for performance in list views.
        editor_or_tester = getattr(self, 'editor_or_tester', None)
        if editor_or_tester is None:
            editor_or_tester = user.profile.id in self.editor_ids or user.profile.id in self.tester_ids

        if editor_or_tester:
            return False

        completed_course = getattr(self, 'completed_course', None)
        if completed_course is None:
            completed_course = self.has_completed_course(user)

        if completed_course:
            return False

        if self.limit_join_organizations:
            return self.join_organizations.filter(id__in=user.profile.organizations.all()).exists()
        return True

    # Also skips access check
    def is_spectatable_by(self, user):
        if not user.is_authenticated:
            return False

        if user.profile.id in self.editor_ids or user.profile.id in self.tester_ids:
            return True

        if self.limit_join_organizations:
            return self.join_organizations.filter(id__in=user.profile.organizations.all()).exists()
        return True

    def is_accessible_by(self, user):
        try:
            self.access_check(user)
        except (self.Inaccessible, self.PrivateCourse):
            return False
        else:
            return True

    def is_editable_by(self, user):
        # If the user can edit all contests
        if user.has_perm('judge.edit_all_contest'):
            return True

        # If the user is a contest organizer or curator
        if user.has_perm('judge.edit_own_contest') and user.profile.id in self.editor_ids:
            return True

        return False

    @classmethod
    def get_visible_courses(cls, user):
        if not user.is_authenticated:
            return cls.objects.filter(is_visible=True, is_organization_private=False, is_private=False) \
                              .defer('description').distinct()

        queryset = cls.objects.defer('description')
        if not (user.has_perm('judge.see_private_contest') or user.has_perm('judge.edit_all_contest')):
            org_check = (Q(organizations__in=user.profile.organizations.all()) |
                         Q(classes__in=user.profile.classes.all()))
            q = Q(is_visible=True)
            q &= (
                Q(view_course_scoreboard=user.profile) |
                Q(is_organization_private=False, is_private=False) |
                Q(is_organization_private=False, is_private=True, private_contestants=user.profile) |
                (Q(is_organization_private=True, is_private=False) & org_check) |
                (Q(is_organization_private=True, is_private=True, private_contestants=user.profile) & org_check)
            )

            q |= Q(authors=user.profile)
            q |= Q(curators=user.profile)
            q |= Q(testers=user.profile)
            q |= Q(spectators=user.profile)
            queryset = queryset.filter(q)
        return queryset.distinct()

    def rate(self):
        with transaction.atomic():
            CourseRating.objects.filter(course__end_time__range=(self.end_time, self._now)).delete()
            for course in Course.objects.filter(
                is_rated=True, end_time__range=(self.end_time, self._now),
            ).order_by('end_time'):
                rate_course(course)

    class Meta:
        permissions = (
            ('see_private_contest', _('See private contests')),
            ('edit_own_contest', _('Edit own contests')),
            ('edit_all_contest', _('Edit all contests')),
            ('clone_contest', _('Clone contest')),
            ('moss_contest', _('MOSS contest')),
            ('contest_rating', _('Rate contests')),
            ('contest_access_code', _('Contest access codes')),
            ('create_private_contest', _('Create private contests')),
            ('change_contest_visibility', _('Change contest visibility')),
            ('contest_problem_label', _('Edit contest problem label script')),
            ('lock_contest', _('Change lock status of contest')),
        )
        verbose_name = _('course')
        verbose_name_plural = _('courses')


class CourseParticipation(models.Model):
    LIVE = 0
    SPECTATE = -1

    course = models.ForeignKey(Course, verbose_name=_('associated course'), related_name='users', on_delete=CASCADE)
    user = models.ForeignKey(Profile, verbose_name=_('user'), related_name='course_history', on_delete=CASCADE)
    real_start = models.DateTimeField(verbose_name=_('start time'), default=timezone.now, db_column='start')
    score = models.FloatField(verbose_name=_('score'), default=0, db_index=True)
    cumtime = models.PositiveIntegerField(verbose_name=_('cumulative time'), default=0)
    is_disqualified = models.BooleanField(verbose_name=_('is disqualified'), default=False,
                                          help_text=_('Whether this participation is disqualified.'))
    tiebreaker = models.FloatField(verbose_name=_('tie-breaking field'), default=0.0)
    virtual = models.IntegerField(verbose_name=_('virtual participation id'), default=LIVE,
                                  help_text=_('0 means non-virtual, otherwise the n-th virtual participation.'))
    format_data = JSONField(verbose_name=_('contest format specific data'), null=True, blank=True)

    def recompute_results(self):
        with transaction.atomic():
            self.course.format.update_participation(self)
            if self.is_disqualified:
                self.score = -9999
                self.cumtime = 0
                self.tiebreaker = 0
                self.save(update_fields=['score', 'cumtime', 'tiebreaker'])
    recompute_results.alters_data = True

    def set_disqualified(self, disqualified):
        self.is_disqualified = disqualified
        self.recompute_results()
        if self.course.is_rated and self.course.course_ratings.exists():
            self.course.rate()
        if self.is_disqualified:
            if self.user.current_course == self:
                self.user.remove_course()
            self.course.banned_users.add(self.user)
        else:
            self.course.banned_users.remove(self.user)
    set_disqualified.alters_data = True

    @property
    def live(self):
        return self.virtual == self.LIVE

    @property
    def spectate(self):
        return self.virtual == self.SPECTATE

    @cached_property
    def start(self):
        course = self.course
        return course.start_time if course.time_limit is None and (self.live or self.spectate) else self.real_start

    @cached_property
    def end_time(self):
        course = self.course
        if self.spectate:
            return course.end_time
        if self.virtual:
            if course.time_limit:
                return self.real_start + course.time_limit
            else:
                return self.real_start + (course.end_time - course.start_time)
        return course.end_time if course.time_limit is None else \
            min(self.real_start + course.time_limit, course.end_time)

    @cached_property
    def _now(self):
        # This ensures that all methods talk about the same now.
        return timezone.now()

    @property
    def ended(self):
        return self.end_time is not None and self.end_time < self._now

    @property
    def time_remaining(self):
        end = self.end_time
        if end is not None and end >= self._now:
            return end - self._now

    def __str__(self):
        if self.spectate:
            return _('%(user)s spectating in %(course)s') % {'user': self.user.username, 'course': self.course.name}
        if self.virtual:
            return _('%(user)s in %(course)s, v%(id)d') % {
                'user': self.user.username, 'course': self.course.name, 'id': self.virtual,
            }
        return _('%(user)s in %(course)s') % {'user': self.user.username, 'course': self.course.name}

    class Meta:
        verbose_name = _('course participation')
        verbose_name_plural = _('course participations')

        unique_together = ('course', 'user', 'virtual')


class CourseProblem(models.Model):
    problem = models.ForeignKey(Problem, verbose_name=_('problem'), related_name='courses', on_delete=CASCADE)
    course = models.ForeignKey(Course, verbose_name=_('course'), related_name='course_problems', on_delete=CASCADE)
    points = models.IntegerField(verbose_name=_('points'))
    partial = models.BooleanField(default=True, verbose_name=_('partial'))
    is_pretested = models.BooleanField(default=False, verbose_name=_('is pretested'))
    order = models.PositiveIntegerField(db_index=True, verbose_name=_('order'))
    output_prefix_override = models.IntegerField(verbose_name=_('output prefix length override'),
                                                 default=0, null=True, blank=True)
    max_submissions = models.IntegerField(verbose_name=_('max submissions'),
                                          help_text=_('Maximum number of submissions for this problem, '
                                                      'or leave blank for no limit.'),
                                          default=None, null=True, blank=True,
                                          validators=[MinValueOrNoneValidator(1, _('Why include a problem you '
                                                                                   "can't submit to?"))])

    class Meta:
        unique_together = ('problem', 'course')
        verbose_name = _('course problem')
        verbose_name_plural = _('course problems')
        ordering = ('order',)


class CourseSubmission(models.Model):
    submission = models.OneToOneField(Submission, verbose_name=_('submission'),
                                      related_name='course', on_delete=CASCADE)
    problem = models.ForeignKey(CourseProblem, verbose_name=_('problem'), on_delete=CASCADE,
                                related_name='submissions', related_query_name='submission')
    participation = models.ForeignKey(CourseParticipation, verbose_name=_('participation'), on_delete=CASCADE,
                                      related_name='submissions', related_query_name='submission')
    points = models.FloatField(default=0.0, verbose_name=_('points'))
    is_pretest = models.BooleanField(verbose_name=_('is pretested'),
                                     help_text=_('Whether this submission was ran only on pretests.'),
                                     default=False)

    class Meta:
        verbose_name = _('course submission')
        verbose_name_plural = _('course submissions')


class TheoryPostGroup(models.Model):
    name = models.CharField(max_length=20, verbose_name=_('theory group ID'), unique=True)
    full_name = models.CharField(max_length=100, verbose_name=_('theory group name'))

    def __str__(self):
        return self.full_name

    class Meta:
        ordering = ['full_name']
        verbose_name = _('theory group')
        verbose_name_plural = _('theory groups')


class CourseTheory(models.Model):
    theory = models.ForeignKey(TheoryPost, verbose_name=_('theory'), related_name='courses', on_delete=CASCADE)
    course = models.ForeignKey(Course, verbose_name=_('course'), related_name='course_theorys', on_delete=CASCADE)
    order = models.PositiveIntegerField(db_index=True, verbose_name=_('order'))

    class Meta:
        unique_together = ('theory', 'course')
        verbose_name = _('course theory')
        verbose_name_plural = _('course theorys')
        ordering = ('order',)


class CourseTest(models.Model):
    test = models.ForeignKey(TestPost, verbose_name=_('test'), related_name='courses', on_delete=CASCADE)
    course = models.ForeignKey(Course, verbose_name=_('course'), related_name='course_tests', on_delete=CASCADE)
    order = models.PositiveIntegerField(db_index=True, verbose_name=_('order'))

    class Meta:
        unique_together = ('test', 'course')
        verbose_name = _('course test')
        verbose_name_plural = _('course tests')
        ordering = ('order',)


class CourseRating(models.Model):
    user = models.ForeignKey(Profile, verbose_name=_('user'), related_name='course_ratings', on_delete=CASCADE)
    course = models.ForeignKey(Course, verbose_name=_('course'), related_name='course_ratings', on_delete=CASCADE)
    participation = models.OneToOneField(CourseParticipation, verbose_name=_('participation'),
                                         related_name='course_rating', on_delete=CASCADE)
    rank = models.IntegerField(verbose_name=_('rank'))
    rating = models.IntegerField(verbose_name=_('rating'))
    mean = models.FloatField(verbose_name=_('raw rating'))
    performance = models.FloatField(verbose_name=_('course performance'))
    last_rated = models.DateTimeField(db_index=True, verbose_name=_('last rated'))

    class Meta:
        unique_together = ('user', 'course')
        verbose_name = _('course rating')
        verbose_name_plural = _('course ratings')
