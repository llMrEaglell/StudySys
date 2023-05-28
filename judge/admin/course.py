from adminsortable2.admin import SortableInlineAdminMixin
from django.contrib import admin
from django.core.exceptions import PermissionDenied
from django.db import connection, transaction
from django.db.models import Q, TextField
from django.forms import ModelForm, ModelMultipleChoiceField
from django.http import Http404, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import path, reverse, reverse_lazy
from django.utils import timezone
from django.utils.html import format_html
from django.utils.translation import gettext_lazy as _, ngettext
from reversion.admin import VersionAdmin

from django_ace import AceWidget
from judge.models import Class, Profile, Rating, Submission, Course, \
    CourseProblem
from judge.models.course import CourseSubmission, CourseTheory, CourseTest
from judge.utils.views import NoBatchDeleteMixin
from judge.widgets import AdminHeavySelect2MultipleWidget, AdminHeavySelect2Widget, AdminMartorWidget, \
    AdminSelect2MultipleWidget, AdminSelect2Widget


class AdminHeavySelect2Widget(AdminHeavySelect2Widget):
    @property
    def is_hidden(self):
        return False


class CourseTagForm(ModelForm):
    courses = ModelMultipleChoiceField(
        label=_('Included courses'),
        queryset=Course.objects.all(),
        required=False,
        widget=AdminHeavySelect2MultipleWidget(data_view='course_select2'))


class CourseTagAdmin(admin.ModelAdmin):
    fields = ('name', 'color', 'description', 'courses')
    list_display = ('name', 'color')
    actions_on_top = True
    actions_on_bottom = True
    form = CourseTagForm
    formfield_overrides = {
        TextField: {'widget': AdminMartorWidget},
    }

    def save_model(self, request, obj, form, change):
        super(CourseTagAdmin, self).save_model(request, obj, form, change)
        obj.courses.set(form.cleaned_data['courses'])

    def get_form(self, request, obj=None, **kwargs):
        form = super(CourseTagAdmin, self).get_form(request, obj, **kwargs)
        if obj is not None:
            form.base_fields['courses'].initial = obj.courses.all()
        return form


class CourseProblemInlineForm(ModelForm):
    class Meta:
        widgets = {'problem': AdminHeavySelect2Widget(data_view='problem_select2')}


class CourseTheoryInlineForm(ModelForm):
    class Meta:
        widgets = {'theory': AdminHeavySelect2Widget(data_view='theory_select2')}


class CourseTestInlineForm(ModelForm):
    class Meta:
        widgets = {'test': AdminHeavySelect2Widget(data_view='test_select2')}


class CourseProblemInline(SortableInlineAdminMixin, admin.TabularInline):
    model = CourseProblem
    verbose_name = _('Problem')
    verbose_name_plural = _('Problems')
    fields = ('problem', 'points', 'partial', 'is_pretested', 'max_submissions', 'output_prefix_override', 'order',
              'rejudge_column')
    readonly_fields = ('rejudge_column',)
    form = CourseProblemInlineForm

    def rejudge_column(self, obj):
        if obj.id is None:
            return ''
        return format_html('<a class="button rejudge-link" href="{0}">{1}</a>',
                           reverse('admin:judge_contest_rejudge', args=(obj.course.id, obj.id)), _('Rejudge'))
    rejudge_column.short_description = ''


class CourseTheoryInline(SortableInlineAdminMixin, admin.TabularInline):
    model = CourseTheory
    verbose_name = _('Theory')
    verbose_name_plural = _('Theorys')
    fields = ('theory', 'order',)
    form = CourseTheoryInlineForm


class CourseTestInline(SortableInlineAdminMixin, admin.TabularInline):
    model = CourseTest
    verbose_name = _('Test')
    verbose_name_plural = _('Tests')
    fields = ('test', 'order',)
    form = CourseTestInlineForm


class CourseForm(ModelForm):
    def __init__(self, *args, **kwargs):
        super(CourseForm, self).__init__(*args, **kwargs)
        self.fields['banned_users'].widget.can_add_related = False
        self.fields['view_course_scoreboard'].widget.can_add_related = False

    def clean(self):
        cleaned_data = super(CourseForm, self).clean()
        cleaned_data['banned_users'].filter(current_course__course=self.instance).update(current_course=None)

    class Meta:
        widgets = {
            'authors': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'curators': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'testers': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'spectators': AdminHeavySelect2MultipleWidget(data_view='profile_select2'),
            'private_members': AdminHeavySelect2MultipleWidget(data_view='profile_select2',
                                                                   attrs={'style': 'width: 100%'}),
            'organizations': AdminHeavySelect2MultipleWidget(data_view='organization_select2'),
            'classes': AdminHeavySelect2MultipleWidget(data_view='class_select2'),
            'join_organizations': AdminHeavySelect2MultipleWidget(data_view='organization_select2'),
            'tags': AdminSelect2MultipleWidget,
            'banned_users': AdminHeavySelect2MultipleWidget(data_view='profile_select2',
                                                            attrs={'style': 'width: 100%'}),
            'view_course_scoreboard': AdminHeavySelect2MultipleWidget(data_view='profile_select2',
                                                                       attrs={'style': 'width: 100%'}),
            'view_course_submissions': AdminHeavySelect2MultipleWidget(data_view='profile_select2',
                                                                        attrs={'style': 'width: 100%'}),
            'description': AdminMartorWidget(attrs={'data-markdownfy-url': reverse_lazy('course_preview')}),
        }


class CourseAdmin(NoBatchDeleteMixin, VersionAdmin):
    # Add other field
    fieldsets = (
        (None, {'fields': ('key', 'name', 'authors', 'curators', 'testers', 'tester_see_submissions',
                           'tester_see_scoreboard', 'spectators')}),
        (_('Settings'), {'fields': ('is_visible', 'use_clarifications', 'hide_problem_tags', 'hide_problem_authors',
                                    'show_short_display', 'run_pretests_only', 'locked_after', 'scoreboard_visibility',
                                    'points_precision')}),
        (_('Scheduling'), {'fields': ('start_time', 'end_time', 'time_limit')}),
        (_('Details'), {'fields': ('description', 'og_image', 'logo_override_image', 'tags', 'summary')}),
        (_('Format'), {'fields': ('format_name', 'format_config', 'problem_label_script')}),
        (_('Access'), {'fields': ('access_code', 'private_members', 'organizations', 'classes',
                                  'join_organizations', 'view_course_scoreboard', 'view_course_submissions')}),
        (_('Justice'), {'fields': ('banned_users',)}),
    )
    list_display = ('key', 'name', 'is_visible', 'locked_after', 'start_time', 'end_time', 'time_limit',
                    'user_count')
    search_fields = ('key', 'name')
    inlines = [CourseProblemInline, CourseTestInline, CourseTheoryInline]
    actions_on_top = True
    actions_on_bottom = True
    form = CourseForm
    change_list_template = 'admin/judge/contest/change_list.html'
    date_hierarchy = 'start_time'

    def get_actions(self, request):
        actions = super(CourseAdmin, self).get_actions(request)

        if request.user.has_perm('judge.change_contest_visibility') or \
                request.user.has_perm('judge.create_private_contest'):
            for action in ('make_visible', 'make_hidden'):
                actions[action] = self.get_action(action)

        if request.user.has_perm('judge.lock_contest'):
            for action in ('set_locked', 'set_unlocked'):
                actions[action] = self.get_action(action)

        return actions

    def get_queryset(self, request):
        queryset = Course.objects.all()
        if request.user.has_perm('judge.edit_all_contest'):
            return queryset
        else:
            return queryset.filter(Q(authors=request.profile) | Q(curators=request.profile)).distinct()

    def get_readonly_fields(self, request, obj=None):
        readonly = []
        if not request.user.has_perm('judge.lock_contest'):
            readonly += ['locked_after']
        if not request.user.has_perm('judge.contest_access_code'):
            readonly += ['access_code']
        if not request.user.has_perm('judge.create_private_contest'):
            readonly += ['private_members', 'organizations']
            if not request.user.has_perm('judge.change_contest_visibility'):
                readonly += ['is_visible']
        if not request.user.has_perm('judge.contest_problem_label'):
            readonly += ['problem_label_script']
        return readonly

    def save_model(self, request, obj, form, change):
        # `private_contestants` and `organizations` will not appear in `cleaned_data` if user cannot edit it
        if form.changed_data:
            if 'private_members' in form.changed_data:
                obj.is_private = bool(form.cleaned_data['private_members'])
            if 'organizations' in form.changed_data or 'classes' in form.changed_data:
                obj.is_organization_private = bool(form.cleaned_data['organizations'] or form.cleaned_data['classes'])
            if 'join_organizations' in form.cleaned_data:
                obj.limit_join_organizations = bool(form.cleaned_data['join_organizations'])

        # `is_visible` will not appear in `cleaned_data` if user cannot edit it
        if form.cleaned_data.get('is_visible') and not request.user.has_perm('judge.change_contest_visibility'):
            if not obj.is_private and not obj.is_organization_private:
                raise PermissionDenied
            if not request.user.has_perm('judge.create_private_contest'):
                raise PermissionDenied

        super().save_model(request, obj, form, change)
        # We need this flag because `save_related` deals with the inlines, but does not know if we have already rescored
        self._rescored = False
        if form.changed_data and any(f in form.changed_data for f in ('format_config', 'format_name')):
            self._rescore(obj.key)
            self._rescored = True

        if form.changed_data and 'locked_after' in form.changed_data:
            self.set_locked_after(obj, form.cleaned_data['locked_after'])

    def save_related(self, request, form, formsets, change):
        super().save_related(request, form, formsets, change)
        # Only rescored if we did not already do so in `save_model`
        if not self._rescored and any(formset.has_changed() for formset in formsets):
            self._rescore(form.cleaned_data['key'])

    def has_change_permission(self, request, obj=None):
        if not request.user.has_perm('judge.edit_own_contest'):
            return False
        if obj is None:
            return True
        return obj.is_editable_by(request.user)

    def _rescore(self, course_key):
        from judge.tasks import rescore_course
        transaction.on_commit(rescore_course.s(course_key).delay)

    def make_visible(self, request, queryset):
        if not request.user.has_perm('judge.change_contest_visibility'):
            queryset = queryset.filter(Q(is_private=True) | Q(is_organization_private=True))
        count = queryset.update(is_visible=True)
        self.message_user(request, ngettext('%d course successfully marked as visible.',
                                            '%d courses successfully marked as visible.',
                                            count) % count)
    make_visible.short_description = _('Mark courses as visible')

    def make_hidden(self, request, queryset):
        if not request.user.has_perm('judge.change_contest_visibility'):
            queryset = queryset.filter(Q(is_private=True) | Q(is_organization_private=True))
        count = queryset.update(is_visible=True)
        self.message_user(request, ngettext('%d course successfully marked as hidden.',
                                            '%d courses successfully marked as hidden.',
                                            count) % count)
    make_hidden.short_description = _('Mark courses as hidden')

    def set_locked(self, request, queryset):
        for row in queryset:
            self.set_locked_after(row, timezone.now())
        count = queryset.count()
        self.message_user(request, ngettext('%d course successfully locked.',
                                            '%d courses successfully locked.',
                                            count) % count)
    set_locked.short_description = _('Lock contest submissions')

    def set_unlocked(self, request, queryset):
        for row in queryset:
            self.set_locked_after(row, None)
        count = queryset.count()
        self.message_user(request, ngettext('%d course successfully unlocked.',
                                            '%d courses successfully unlocked.',
                                            count) % count)
    set_unlocked.short_description = _('Unlock course submissions')

    def set_locked_after(self, course, locked_after):
        with transaction.atomic():
            course.locked_after = locked_after
            course.save()
            Submission.objects.filter(course_object=course,
                                      course__participation__virtual=0).update(locked_after=locked_after)

    def get_urls(self):
        return [
            path('<int:course_id>/judge/<int:problem_id>/', self.rejudge_view, name='judge_contest_rejudge'),
        ] + super(CourseAdmin, self).get_urls()

    def rejudge_view(self, request, course_id, problem_id):
        queryset = CourseSubmission.objects.filter(problem_id=problem_id).select_related('submission')
        for model in queryset:
            model.submission.judge(rejudge=True, rejudge_user=request.user)

        self.message_user(request, ngettext('%d submission was successfully scheduled for rejudging.',
                                            '%d submissions were successfully scheduled for rejudging.',
                                            len(queryset)) % len(queryset))
        return HttpResponseRedirect(reverse('admin:judge_contest_change', args=(course_id,)))


    def get_form(self, request, obj=None, **kwargs):
        form = super(CourseAdmin, self).get_form(request, obj, **kwargs)
        if 'problem_label_script' in form.base_fields:
            # form.base_fields['problem_label_script'] does not exist when the user has only view permission
            # on the model.
            form.base_fields['problem_label_script'].widget = AceWidget(
                mode='lua', theme=request.profile.resolved_ace_theme,
            )

        perms = ('edit_own_contest', 'edit_all_contest')
        form.base_fields['curators'].queryset = Profile.objects.filter(
            Q(user__is_superuser=True) |
            Q(user__groups__permissions__codename__in=perms) |
            Q(user__user_permissions__codename__in=perms),
        ).distinct()
        form.base_fields['classes'].queryset = Class.get_visible_classes(request.user)
        return form


class CourseParticipationForm(ModelForm):
    class Meta:
        widgets = {
            'course': AdminSelect2Widget(),
            'user': AdminHeavySelect2Widget(data_view='profile_select2'),
        }


class CourseParticipationAdmin(admin.ModelAdmin):
    fields = ('course', 'user', 'real_start', 'virtual', 'is_disqualified')
    list_display = ('course', 'username', 'show_virtual', 'real_start', 'score', 'cumtime', 'tiebreaker')
    actions = ['recalculate_results']
    actions_on_bottom = actions_on_top = True
    search_fields = ('course__key', 'course__name', 'user__user__username')
    form = CourseParticipationForm
    date_hierarchy = 'real_start'

    def get_queryset(self, request):
        return super(CourseParticipationAdmin, self).get_queryset(request).only(
            'course__name', 'course__format_name', 'course__format_config',
            'user__user__username', 'real_start', 'score', 'cumtime', 'tiebreaker', 'virtual',
        )

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if form.changed_data and 'is_disqualified' in form.changed_data:
            obj.set_disqualified(obj.is_disqualified)

    def recalculate_results(self, request, queryset):
        count = 0
        for participation in queryset:
            participation.recompute_results()
            count += 1
        self.message_user(request, ngettext('%d participation recalculated.',
                                            '%d participations recalculated.',
                                            count) % count)
    recalculate_results.short_description = _('Recalculate results')

    def username(self, obj):
        return obj.user.username
    username.short_description = _('username')
    username.admin_order_field = 'user__user__username'

    def show_virtual(self, obj):
        return obj.virtual or '-'
    show_virtual.short_description = _('virtual')
    show_virtual.admin_order_field = 'virtual'
