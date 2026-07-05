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
from django import template
from django.contrib.auth.models import User

# wger
from wger.teams.models import Team


register = template.Library()


@register.inclusion_tag('teams/coach_bar.html', takes_context=True)
def teams_coach_bar(context):
    """
    Context for the impersonation coach bar: which teams the coach and the
    impersonated player share, and the teammates the coach may switch to.
    """
    trainer_pk = context.get('trainer_identity')
    player = context.get('user')
    if not trainer_pk or player is None or not player.is_authenticated:
        return {'show': False}

    trainer = User.objects.filter(pk=trainer_pk).first()
    if trainer is None:
        return {'show': False}

    if trainer.is_staff or trainer.is_superuser:
        shared_teams = Team.objects.filter(
            is_active=True,
            memberships__user=player,
            memberships__is_coach=False,
        ).distinct()
    else:
        shared_teams = Team.objects.filter(
            is_active=True,
            memberships__user=player,
            memberships__is_coach=False,
        ).filter(
            memberships__user=trainer,
            memberships__is_coach=True,
        ).distinct()

    teammates = (
        User.objects.filter(
            team_memberships__team__in=shared_teams,
            team_memberships__is_coach=False,
        )
        .exclude(pk=player.pk)
        .distinct()
        .order_by('username')
    )

    return {
        'show': True,
        'player': player,
        'teams': shared_teams,
        'teammates': teammates,
        'csrf_token': context.get('csrf_token'),
    }
