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

# Django
from django.contrib.auth.models import User
from django.db import models
from django.db.models import Q


class Team(models.Model):
    """
    A training team/cohort, mirrored from an identity-provider group.

    Rows are created automatically the first time a login's OIDC ``groups``
    claim mentions a group matching ``OIDC_TEAM_GROUP_PREFIX``; membership is
    claim-authoritative and re-synced on every SSO login
    (see :func:`wger.teams.services.sync_user_teams`).
    """

    class Meta:
        ordering = ['name']

    slug = models.SlugField(
        max_length=80,
        unique=True,
    )

    name = models.CharField(
        max_length=60,
    )

    authentik_group = models.CharField(
        max_length=150,
        unique=True,
    )
    """Full group name in the identity provider, e.g. 'team-14u'"""

    is_active = models.BooleanField(
        default=True,
    )

    created = models.DateTimeField(
        auto_now_add=True,
    )

    def __str__(self):
        return self.name


class TeamMembership(models.Model):
    """
    A user's membership of a team, mirrored from the identity provider.

    ``is_coach`` is derived from the OIDC coach role group(s), not stored per
    team in the identity provider: a coach is a coach of all their teams.
    """

    class Meta:
        unique_together = ('team', 'user')

    team = models.ForeignKey(
        Team,
        on_delete=models.CASCADE,
        related_name='memberships',
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='team_memberships',
    )

    is_coach = models.BooleanField(
        default=False,
    )

    created = models.DateTimeField(
        auto_now_add=True,
    )

    def __str__(self):
        return f'{self.user.username} in {self.team.name}'


class RoutineAssignment(models.Model):
    """
    A coach's decision to put a routine template in front of a player or team.

    Materialized as per-player routine copies (:class:`AssignedRoutine`) so
    players see a plain wger routine in every client, including the mobile app.
    Team assignments also reach members who join (or first log in) later, via
    the login-time self-heal in :func:`wger.teams.services.materialize_for_user`.
    """

    class Meta:
        ordering = ['-created']
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(team__isnull=False, user__isnull=True)
                    | Q(team__isnull=True, user__isnull=False)
                ),
                name='teams_assignment_exactly_one_target',
            ),
        ]

    template = models.ForeignKey(
        'manager.Routine',
        on_delete=models.PROTECT,
        related_name='team_assignments',
        limit_choices_to={'is_template': True},
    )
    """
    PROTECT: a template with assignments (even inactive ones) cannot be
    deleted; delete the assignments first (player copies survive that).
    """

    team = models.ForeignKey(
        Team,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='assignments',
    )

    user = models.ForeignKey(
        User,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name='routine_assignments',
    )

    start = models.DateField()

    note = models.CharField(
        max_length=200,
        blank=True,
    )

    assigned_by = models.ForeignKey(
        User,
        null=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )

    active = models.BooleanField(
        default=True,
    )

    created = models.DateTimeField(
        auto_now_add=True,
    )

    @property
    def target(self):
        return self.team if self.team_id else self.user

    def __str__(self):
        return f'"{self.template.name}" for {self.target}'


class AssignedRoutine(models.Model):
    """
    Per-player materialization of a :class:`RoutineAssignment`.

    ``routine`` is the player's own copy. When the player deletes that copy the
    row remains as a tombstone (``routine=None``) so the login-time self-heal
    never force-recreates a routine the player explicitly removed.
    """

    class Meta:
        unique_together = ('assignment', 'user')

    assignment = models.ForeignKey(
        RoutineAssignment,
        on_delete=models.CASCADE,
        related_name='instances',
    )

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name='assigned_routines',
    )

    routine = models.ForeignKey(
        'manager.Routine',
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name='+',
    )

    copied_at = models.DateTimeField(
        auto_now_add=True,
    )

    def __str__(self):
        return f'{self.assignment} -> {self.user.username}'
