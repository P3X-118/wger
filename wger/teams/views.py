# This file is part of wger Workout Manager.
#
# wger Workout Manager is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# wger Workout Manager is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License

# Standard Library
import datetime
import logging

# Django
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.db.models import (
    Count,
    Max,
    Q,
)
from django.http import HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST
from django.views.generic import (
    DetailView,
    ListView,
)

# wger
from wger.teams.forms import AssignmentForm
from wger.teams.models import (
    RoutineAssignment,
    Team,
    TeamMembership,
)
from wger.teams.services import materialize_assignment


logger = logging.getLogger(__name__)


def _teams_enabled():
    return settings.WGER_SETTINGS.get('TEAMS_ENABLED', False)


def _is_coach(user):
    """
    Anyone allowed into the coach console: staff, wger gym trainers (the role
    the OIDC coach group maps onto) or an explicit coach team membership.
    """
    return (
        user.is_staff
        or user.is_superuser
        or user.has_perm('gym.gym_trainer')
        or TeamMembership.objects.filter(user=user, is_coach=True).exists()
    )


def _is_team_coach(user, team):
    if user.is_staff or user.is_superuser:
        return True
    return TeamMembership.objects.filter(team=team, user=user, is_coach=True).exists()


class CoachAccessMixin(LoginRequiredMixin):
    def dispatch(self, request, *args, **kwargs):
        if request.user.is_authenticated:
            if not _teams_enabled() or not _is_coach(request.user):
                raise PermissionDenied()
        return super().dispatch(request, *args, **kwargs)


class TeamsOverviewView(CoachAccessMixin, ListView):
    model = Team
    context_object_name = 'teams'
    template_name = 'teams/overview.html'

    def get_queryset(self):
        user = self.request.user
        if user.is_staff or user.is_superuser:
            queryset = Team.objects.all()
        else:
            queryset = Team.objects.filter(
                memberships__user=user,
                memberships__is_coach=True,
            )
        return queryset.annotate(
            player_count=Count(
                'memberships',
                filter=Q(memberships__is_coach=False),
                distinct=True,
            ),
            active_assignment_count=Count(
                'assignments',
                filter=Q(assignments__active=True),
                distinct=True,
            ),
        )


class TeamDetailView(CoachAccessMixin, DetailView):
    model = Team
    context_object_name = 'team'
    template_name = 'teams/team_detail.html'

    def get_object(self, queryset=None):
        team = super().get_object(queryset)
        if not _is_team_coach(self.request.user, team):
            raise PermissionDenied()
        return team

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        team = self.object
        week_ago = timezone.localdate() - datetime.timedelta(days=7)

        players = (
            User.objects.filter(
                team_memberships__team=team,
                team_memberships__is_coach=False,
            )
            .select_related('userprofile')
            .annotate(
                last_session=Max('workoutsession__date'),
                sessions_7d=Count(
                    'workoutsession',
                    filter=Q(workoutsession__date__gte=week_ago),
                    distinct=True,
                ),
                active_programs=Count(
                    'assigned_routines',
                    filter=Q(
                        assigned_routines__assignment__active=True,
                        assigned_routines__routine__isnull=False,
                    ),
                    distinct=True,
                ),
            )
            .order_by('username')
        )

        context.update(
            {
                'players': players,
                'coaches': User.objects.filter(
                    team_memberships__team=team,
                    team_memberships__is_coach=True,
                ).order_by('username'),
                'assignments': team.assignments.select_related(
                    'template',
                    'assigned_by',
                ),
                'individual_assignments': RoutineAssignment.objects.filter(
                    user__team_memberships__team=team,
                    active=True,
                ).select_related('template', 'user'),
                'form': AssignmentForm(coach=self.request.user, team=team),
                'trainer_login_possible': self.request.user.has_perm('gym.gym_trainer'),
            }
        )
        return context


def _check_coach_or_403(request, team=None):
    if not _teams_enabled() or not _is_coach(request.user):
        raise PermissionDenied()
    if team is not None and not _is_team_coach(request.user, team):
        raise PermissionDenied()


def _can_manage_assignment(user, assignment):
    if user.is_staff or user.is_superuser:
        return True
    if assignment.team_id:
        return TeamMembership.objects.filter(
            team_id=assignment.team_id,
            user=user,
            is_coach=True,
        ).exists()
    return TeamMembership.objects.filter(
        user=user,
        is_coach=True,
        team__memberships__user=assignment.user_id,
    ).exists()


def _redirect_back(request, assignment):
    next_url = request.POST.get('next')
    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return HttpResponseRedirect(next_url)
    if assignment.team_id:
        return HttpResponseRedirect(reverse('teams:detail', kwargs={'pk': assignment.team_id}))
    return HttpResponseRedirect(reverse('teams:overview'))


@login_required
@require_POST
def assignment_create(request, team_pk):
    team = get_object_or_404(Team, pk=team_pk)
    _check_coach_or_403(request, team)

    form = AssignmentForm(request.POST, coach=request.user, team=team)
    if not form.is_valid():
        for field, errors in form.errors.items():
            messages.error(request, f'{field}: {" ".join(errors)}')
        return HttpResponseRedirect(reverse('teams:detail', kwargs={'pk': team.pk}))

    player = form.cleaned_data['player']
    assignment = RoutineAssignment.objects.create(
        template=form.cleaned_data['template'],
        team=None if player else team,
        user=player or None,
        start=form.cleaned_data['start'],
        note=form.cleaned_data['note'],
        assigned_by=request.user,
    )
    created = materialize_assignment(assignment)
    messages.success(
        request,
        _('Assigned "%(name)s" to %(target)s (%(count)s new routines created)')
        % {
            'name': assignment.template.name,
            'target': assignment.target,
            'count': created,
        },
    )
    return HttpResponseRedirect(reverse('teams:detail', kwargs={'pk': team.pk}))


@login_required
@require_POST
def assignment_toggle(request, pk):
    assignment = get_object_or_404(RoutineAssignment, pk=pk)
    _check_coach_or_403(request)
    if not _can_manage_assignment(request.user, assignment):
        raise PermissionDenied()

    assignment.active = not assignment.active
    assignment.save(update_fields=['active'])
    if assignment.active:
        created = materialize_assignment(assignment)
        messages.success(
            request,
            _('Assignment activated (%(count)s new routines created)') % {'count': created},
        )
    else:
        messages.success(request, _('Assignment deactivated'))
    return _redirect_back(request, assignment)


@login_required
@require_POST
def assignment_delete(request, pk):
    assignment = get_object_or_404(RoutineAssignment, pk=pk)
    _check_coach_or_403(request)
    if not _can_manage_assignment(request.user, assignment):
        raise PermissionDenied()

    assignment.delete()
    messages.success(
        request,
        _('Assignment deleted. Routines already in player accounts were kept.'),
    )
    return _redirect_back(request, assignment)
