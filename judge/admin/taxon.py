from django.contrib import admin
from django.forms import ModelForm, ModelMultipleChoiceField
from django.utils.translation import gettext_lazy as _

from judge.models import Problem, TheoryPost
from judge.widgets import AdminHeavySelect2MultipleWidget


class ProblemGroupForm(ModelForm):
    problems = ModelMultipleChoiceField(
        label=_('Included problems'),
        queryset=Problem.objects.all(),
        required=False,
        help_text=_('These problems are included in this group of problems.'),
        widget=AdminHeavySelect2MultipleWidget(data_view='problem_select2'))


class ProblemGroupAdmin(admin.ModelAdmin):
    fields = ('name', 'full_name', 'problems')
    form = ProblemGroupForm

    def save_model(self, request, obj, form, change):
        super(ProblemGroupAdmin, self).save_model(request, obj, form, change)
        obj.problem_set.set(form.cleaned_data['problems'])
        obj.save()

    def get_form(self, request, obj=None, **kwargs):
        self.form.base_fields['problems'].initial = [o.pk for o in obj.problem_set.all()] if obj else []
        return super(ProblemGroupAdmin, self).get_form(request, obj, **kwargs)


class TheoryPostGroupForm(ModelForm):
    theorys = ModelMultipleChoiceField(
        label=_('Included theory'),
        queryset=TheoryPost.objects.all(),
        required=False,
        help_text=_('These theory are included in this group of theorys.'),
        widget=AdminHeavySelect2MultipleWidget(data_view='theory_select2')) # TODO MB change this


class TheoryPostGroupAdmin(admin.ModelAdmin):
    fields = ('name', 'full_name', 'theorys')
    form = TheoryPostGroupForm

    def save_model(self, request, obj, form, change):
        super(TheoryPostGroupAdmin, self).save_model(request, obj, form, change)
        obj.theorys.set(form.cleaned_data['theorys'])
        obj.save()

    def get_form(self, request, obj=None, **kwargs):
        self.form.base_fields['theorys'].initial = [o.pk for o in obj.theorys.all()] if obj else []
        return super(TheoryPostGroupAdmin, self).get_form(request, obj, **kwargs)


class ProblemTypeForm(ModelForm):
    problems = ModelMultipleChoiceField(
        label=_('Included problems'),
        queryset=Problem.objects.all(),
        required=False,
        help_text=_('These problems are included in this type of problems.'),
        widget=AdminHeavySelect2MultipleWidget(data_view='problem_select2'))


class ProblemTypeAdmin(admin.ModelAdmin):
    fields = ('name', 'full_name', 'problems')
    form = ProblemTypeForm

    def save_model(self, request, obj, form, change):
        super(ProblemTypeAdmin, self).save_model(request, obj, form, change)
        obj.problem_set.set(form.cleaned_data['problems'])
        obj.save()

    def get_form(self, request, obj=None, **kwargs):
        self.form.base_fields['problems'].initial = [o.pk for o in obj.problem_set.all()] if obj else []
        return super(ProblemTypeAdmin, self).get_form(request, obj, **kwargs)
