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
from django.contrib.auth import login as django_login
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
from django.shortcuts import (
    get_object_or_404,
    render,
)
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
from wger.gym.helpers import is_same_gym
from wger.teams.forms import BatchAssignmentForm
from wger.teams.models import (
    RoutineAssignment,
    Team,
    TeamMembership,
)
from wger.teams.services import materialize_many


logger = logging.getLogger(__name__)

AUTH_BACKEND = 'django.contrib.auth.backends.ModelBackend'


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


def _coach_teams(user):
    """Teams this user may manage (staff: all)"""
    if user.is_staff or user.is_superuser:
        return Team.objects.filter(is_active=True)
    return Team.objects.filter(
        is_active=True,
        memberships__user=user,
        memberships__is_coach=True,
    )


def _team_players(teams):
    return (
        User.objects.filter(
            team_memberships__team__in=teams,
            team_memberships__is_coach=False,
        )
        .distinct()
        .order_by('username')
    )


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
                # ordered by template for {% regroup %} in the sidebar
                'individual_assignments': RoutineAssignment.objects.filter(
                    user__team_memberships__team=team,
                    active=True,
                )
                .select_related('template', 'user')
                .order_by('template__name', 'user__username'),
                'form': BatchAssignmentForm(coach=self.request.user),
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


def _create_batch(request, form, allowed_teams):
    """
    Turn a valid BatchAssignmentForm into assignment rows + routine copies.

    Player picks already covered by a selected team are dropped (the team
    assignment reaches them); a player on two selected teams still gets exactly
    one copy (materialize_many shares it across the assignments).
    """
    selected_teams = list(form.cleaned_data['teams'])
    selected_players = list(form.cleaned_data['players'])
    covered = set()
    if selected_teams:
        covered = set(
            User.objects.filter(
                team_memberships__team__in=selected_teams,
                team_memberships__is_coach=False,
            ).values_list('pk', flat=True)
        )
    direct_players = [player for player in selected_players if player.pk not in covered]
    skipped = len(selected_players) - len(direct_players)

    assignments = []
    for template in form.cleaned_data['templates']:
        for team in selected_teams:
            assignments.append(
                RoutineAssignment.objects.create(
                    template=template,
                    team=team,
                    start=form.cleaned_data['start'],
                    note=form.cleaned_data['note'],
                    assigned_by=request.user,
                )
            )
        for player in direct_players:
            assignments.append(
                RoutineAssignment.objects.create(
                    template=template,
                    user=player,
                    start=form.cleaned_data['start'],
                    note=form.cleaned_data['note'],
                    assigned_by=request.user,
                )
            )

    copies = materialize_many(assignments)
    message = _('%(assignments)s assignments created, %(copies)s routines delivered.') % {
        'assignments': len(assignments),
        'copies': copies,
    }
    if skipped:
        message += ' ' + _(
            '%(count)s player picks were already covered by a selected team.'
        ) % {'count': skipped}
    messages.success(request, message)


@login_required
@require_POST
def assignment_create(request, team_pk):
    """Assign from a team page: checked players, or the whole team if none"""
    team = get_object_or_404(Team, pk=team_pk)
    _check_coach_or_403(request, team)

    data = request.POST.copy()
    if not data.getlist('players'):
        data.setlist('teams', [str(team.pk)])
    else:
        data.setlist('teams', [])

    form = BatchAssignmentForm(
        data,
        coach=request.user,
        teams=Team.objects.filter(pk=team.pk),
        players=_team_players([team]),
    )
    if not form.is_valid():
        for field, errors in form.errors.items():
            messages.error(request, f'{field}: {" ".join(errors)}')
        return HttpResponseRedirect(reverse('teams:detail', kwargs={'pk': team.pk}))

    _create_batch(request, form, allowed_teams=Team.objects.filter(pk=team.pk))
    return HttpResponseRedirect(reverse('teams:detail', kwargs={'pk': team.pk}))


@login_required
def assign_hub(request):
    """
    Cross-team assignment: templates x (teams and/or players) in one action.
    """
    _check_coach_or_403(request)
    coach_teams = _coach_teams(request.user)
    players = _team_players(coach_teams)

    if request.method == 'POST':
        form = BatchAssignmentForm(
            request.POST,
            coach=request.user,
            teams=coach_teams,
            players=players,
        )
        if form.is_valid():
            _create_batch(request, form, allowed_teams=coach_teams)
            return HttpResponseRedirect(reverse('teams:assign-hub'))
        for field, errors in form.errors.items():
            messages.error(request, f'{field}: {" ".join(errors)}')
    else:
        form = BatchAssignmentForm(coach=request.user, teams=coach_teams, players=players)

    # players grouped by team for the target picker
    team_rosters = [
        {
            'team': team,
            'players': User.objects.filter(
                team_memberships__team=team,
                team_memberships__is_coach=False,
            ).order_by('username'),
        }
        for team in coach_teams.order_by('name')
    ]

    return render(
        request,
        'teams/assign.html',
        {
            'form': form,
            'team_rosters': team_rosters,
            'templates': form.fields['templates'].queryset,
        },
    )


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
        created = materialize_many([assignment])
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


def _switch_target_allowed(trainer, target):
    """Mirror trainer_login's rules + team scope for direct player switching"""
    if not trainer.has_perm('gym.gym_trainer'):
        return False
    if (
        target.has_perm('gym.gym_trainer')
        or target.has_perm('gym.manage_gym')
        or target.has_perm('gym.manage_gyms')
    ):
        return False
    if not is_same_gym(trainer, target):
        return False
    return TeamMembership.objects.filter(
        user=target,
        is_coach=False,
        team__memberships__user=trainer,
        team__memberships__is_coach=True,
    ).exists() or trainer.is_staff


@require_POST
def switch_player(request, user_pk):
    """
    While impersonating a player, jump straight to a teammate without the
    log-out/log-in bounce. Same safety rules as wger's trainer login, plus the
    target must be on a team the original coach actually coaches.
    """
    trainer_identity_pk = request.session.get('trainer.identity')
    if not _teams_enabled() or not trainer_identity_pk:
        raise PermissionDenied()

    trainer = get_object_or_404(User, pk=trainer_identity_pk)
    target = get_object_or_404(User, pk=user_pk)
    if not _switch_target_allowed(trainer, target):
        raise PermissionDenied()

    # Re-authenticate as the coach (flushes the session), then into the target.
    django_login(request, trainer, AUTH_BACKEND)
    django_login(request, target, AUTH_BACKEND)
    request.session['trainer.identity'] = trainer.pk
    return HttpResponseRedirect(reverse('core:dashboard'))


@require_POST
def coach_return(request):
    """
    End impersonation and land back where coaching happens (the upstream
    switch-back always redirects to the gym member list instead).
    """
    trainer_identity_pk = request.session.get('trainer.identity')
    if not _teams_enabled() or not trainer_identity_pk:
        raise PermissionDenied()

    trainer = get_object_or_404(User, pk=trainer_identity_pk)
    next_url = request.POST.get('next')
    django_login(request, trainer, AUTH_BACKEND)

    if next_url and url_has_allowed_host_and_scheme(
        next_url,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return HttpResponseRedirect(next_url)
    return HttpResponseRedirect(reverse('teams:overview'))
