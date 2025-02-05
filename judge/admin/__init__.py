from django.contrib import admin
from django.contrib.admin.models import LogEntry
from django.contrib.flatpages.models import FlatPage

from judge.admin.comments import CommentAdmin
from judge.admin.contest import ContestAdmin, ContestParticipationAdmin, ContestTagAdmin
from judge.admin.course import CourseAdmin, CourseParticipationAdmin, CourseTagAdmin
from judge.admin.interface import BlogPostAdmin, FlatPageAdmin, LicenseAdmin, LogEntryAdmin, NavigationBarAdmin, \
    TheoryPostAdmin, TestPostAdmin
from judge.admin.organization import ClassAdmin, OrganizationAdmin, OrganizationRequestAdmin
from judge.admin.problem import ProblemAdmin, ProblemPointsVoteAdmin
from judge.admin.profile import ProfileAdmin
from judge.admin.runtime import JudgeAdmin, LanguageAdmin
from judge.admin.submission import SubmissionAdmin
from judge.admin.taxon import ProblemGroupAdmin, ProblemTypeAdmin, TheoryPostGroupAdmin
from judge.admin.ticket import TicketAdmin
from judge.models import BlogPost, Class, Comment, CommentLock, Contest, ContestParticipation, \
    ContestTag, Judge, Language, License, MiscConfig, NavigationBar, Organization, \
    OrganizationRequest, Problem, ProblemGroup, ProblemPointsVote, ProblemType, Profile, Submission, Ticket
from judge.models.course import CourseTag, TheoryPost, TestPost, TheoryPostGroup, Course, CourseParticipation

admin.site.register(BlogPost, BlogPostAdmin)
admin.site.register(TheoryPost, TheoryPostAdmin)
admin.site.register(TestPost, TestPostAdmin)
admin.site.register(TheoryPostGroup, TheoryPostGroupAdmin)
admin.site.register(Comment, CommentAdmin)
admin.site.register(CommentLock)

admin.site.register(Contest, ContestAdmin)
admin.site.register(ContestParticipation, ContestParticipationAdmin)
admin.site.register(ContestTag, ContestTagAdmin)

admin.site.register(Course, CourseAdmin)
admin.site.register(CourseParticipation, CourseParticipationAdmin)
admin.site.register(CourseTag, CourseTagAdmin)

admin.site.unregister(FlatPage)
admin.site.register(FlatPage, FlatPageAdmin)
admin.site.register(Judge, JudgeAdmin)
admin.site.register(Language, LanguageAdmin)
admin.site.register(License, LicenseAdmin)
admin.site.register(LogEntry, LogEntryAdmin)
admin.site.register(MiscConfig)
admin.site.register(NavigationBar, NavigationBarAdmin)
admin.site.register(Class, ClassAdmin)
admin.site.register(Organization, OrganizationAdmin)
admin.site.register(OrganizationRequest, OrganizationRequestAdmin)
admin.site.register(Problem, ProblemAdmin)
admin.site.register(ProblemGroup, ProblemGroupAdmin)
admin.site.register(ProblemPointsVote, ProblemPointsVoteAdmin)
admin.site.register(ProblemType, ProblemTypeAdmin)
admin.site.register(Profile, ProfileAdmin)
admin.site.register(Submission, SubmissionAdmin)
admin.site.register(Ticket, TicketAdmin)
