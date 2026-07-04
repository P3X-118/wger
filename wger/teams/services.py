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
import copy
import datetime
import logging
from typing import Iterable

# Django
from django.conf import settings
from django.contrib.auth.models import (
    Group,
    Permission,
    User,
)
from django.db import transaction
from django.db.models import Q
from django.utils.text import slugify

# wger
from wger.gym.models import (
    Gym,
    GymAdminConfig,
    GymUserConfig,
)
from wger.manager.models import Routine
from wger.teams.models import (
    AssignedRoutine,
    RoutineAssignment,
    Team,
    TeamMembership,
)


logger = logging.getLogger(__name__)

TRAINER_GROUP_NAME = 'gym_trainer'

# (app_label, codename) permissions the 'groups' fixture attaches to the
# gym_trainer group. Only used when the group is missing entirely (fresh
# installs that never ran the bootstrap fixtures).
_TRAINER_GROUP_PERMS = (
    ('gym', 'gym_trainer'),
    ('gym', 'add_adminusernote'),
    ('gym', 'change_adminusernote'),
    ('gym', 'delete_adminusernote'),
    ('gym', 'change_gymadminconfig'),
    ('gym', 'change_gymuserconfig'),
    ('gym', 'add_userdocument'),
    ('gym', 'change_userdocument'),
    ('gym', 'delete_userdocument'),
)

# Every per-slot-entry config relation a routine copy must carry over. Keep in
# sync with manager.views.routine.copy_routine.
_CONFIG_RELATIONS = (
    'weightconfig_set',
    'maxweightconfig_set',
    'repetitionsconfig_set',
    'maxrepetitionsconfig_set',
    'rirconfig_set',
    'maxrirconfig_set',
    'restconfig_set',
    'maxrestconfig_set',
    'setsconfig_set',
    'maxsetsconfig_set',
)


def _unique_team_slug(base: str) -> str:
    slug = slugify(base)[:70] or 'team'
    candidate = slug
    counter = 2
    while Team.objects.filter(slug=candidate).exists():
        candidate = f'{slug}-{counter}'
        counter += 1
    return candidate


def sync_user_teams(user: User, group_names: Iterable[str]) -> None:
    """
    Mirror the identity provider's ``groups`` claim onto teams, coach role and
    the facility gym.

    Claim-authoritative: memberships, the coach flag and the gym_trainer role
    are added *and removed* to match the current login's claim, so the IdP
    stays the single source of truth.
    """
    group_names = set(group_names or [])
    prefix = getattr(settings, 'OIDC_TEAM_GROUP_PREFIX', 'team-')
    coach_groups = set(getattr(settings, 'OIDC_COACH_GROUPS', []) or [])
    claimed = {name for name in group_names if name.startswith(prefix) and name != prefix}
    is_coach = bool(coach_groups & group_names)

    for group_name in sorted(claimed):
        display_name = (group_name[len(prefix) :] or group_name)[:60]
        team = Team.objects.filter(authentik_group=group_name).first()
        if team is None:
            team = Team.objects.create(
                authentik_group=group_name,
                slug=_unique_team_slug(display_name),
                name=display_name,
            )
            logger.info('teams: created team %s from group %s', team.slug, group_name)

        membership, _ = TeamMembership.objects.get_or_create(
            team=team,
            user=user,
            defaults={'is_coach': is_coach},
        )
        if membership.is_coach != is_coach:
            membership.is_coach = is_coach
            membership.save(update_fields=['is_coach'])

    removed = TeamMembership.objects.filter(user=user).exclude(
        team__authentik_group__in=claimed,
    )
    if removed.exists():
        logger.info(
            'teams: removing %s from teams no longer claimed: %s',
            user.username,
            sorted(removed.values_list('team__slug', flat=True)),
        )
        removed.delete()

    _sync_trainer_group(user, is_coach)
    _ensure_default_gym(user, is_coach)


def _sync_trainer_group(user: User, is_coach: bool) -> None:
    """
    Put coaches into (and drop ex-coaches from) wger's gym_trainer group, which
    carries the gym.gym_trainer permission used by the member list and the
    trainer login.
    """
    group = Group.objects.filter(name=TRAINER_GROUP_NAME).first()
    if group is None:
        group = Group.objects.create(name=TRAINER_GROUP_NAME)
        for app_label, codename in _TRAINER_GROUP_PERMS:
            permission = Permission.objects.filter(
                content_type__app_label=app_label,
                codename=codename,
            ).first()
            if permission:
                group.permissions.add(permission)
        logger.info('teams: created missing %s group', TRAINER_GROUP_NAME)

    if is_coach:
        user.groups.add(group)
    else:
        user.groups.remove(group)


def _ensure_default_gym(user: User, is_coach: bool) -> None:
    """
    Assign SSO users to the facility gym (TEAMS_DEFAULT_GYM).

    wger's trainer login requires trainer and member to share a gym, and the
    stock default-gym logic only runs for form-based signups, which SSO
    auto-provisioning bypasses. Users already in a different gym are left alone.
    """
    gym_name = getattr(settings, 'TEAMS_DEFAULT_GYM', '')
    if not gym_name:
        return

    gym = Gym.objects.filter(name=gym_name).first()
    if gym is None:
        gym = Gym.objects.create(name=gym_name)
        logger.info('teams: created facility gym "%s"', gym_name)

    profile = user.userprofile
    if profile.gym_id is None:
        profile.gym = gym
        profile.save(update_fields=['gym'])

    if profile.gym_id == gym.pk:
        config_class = GymAdminConfig if is_coach else GymUserConfig
        config_class.objects.get_or_create(user=user, defaults={'gym': gym})


@transaction.atomic
def copy_routine_for_user(template: Routine, user: User, start_date: datetime.date) -> Routine:
    """
    Deep-copy a routine into ``user``'s account, starting at ``start_date``.

    Mirrors manager.views.routine.copy_routine (days -> slots -> entries -> all
    config rows) and additionally copies labels, which the upstream view skips.
    """
    routine_copy: Routine = copy.copy(template)
    routine_copy.pk = None
    routine_copy.created = None
    routine_copy.user = user
    routine_copy.is_template = False
    routine_copy.is_public = False
    routine_copy.start = start_date
    routine_copy.end = start_date + template.duration
    routine_copy.save()

    for label in template.labels.all():
        label_copy = copy.copy(label)
        label_copy.pk = None
        label_copy.routine = routine_copy
        label_copy.save()

    for day in template.days.all():
        day_copy = copy.copy(day)
        day_copy.pk = None
        day_copy.routine = routine_copy
        day_copy.save()

        for slot in day.slots.all():
            slot_copy = copy.copy(slot)
            slot_copy.pk = None
            slot_copy.day = day_copy
            slot_copy.save()

            for entry in slot.entries.all():
                entry_copy = copy.copy(entry)
                entry_copy.pk = None
                entry_copy.slot = slot_copy
                entry_copy.save()

                for relation in _CONFIG_RELATIONS:
                    for config in getattr(entry, relation).all():
                        config_copy = copy.copy(config)
                        config_copy.pk = None
                        config_copy.slot_entry = entry_copy
                        config_copy.save()

    return routine_copy


def _materialize_one(assignment: RoutineAssignment, user: User) -> bool:
    """
    Give ``user`` their copy of the assignment's template, exactly once.

    An existing AssignedRoutine row - including a tombstone left by the player
    deleting their copy - means there is nothing to do.
    """
    if AssignedRoutine.objects.filter(assignment=assignment, user=user).exists():
        return False

    with transaction.atomic():
        routine = copy_routine_for_user(assignment.template, user, assignment.start)
        AssignedRoutine.objects.create(assignment=assignment, user=user, routine=routine)
    logger.info('teams: materialized %s for %s', assignment, user.username)
    return True


def materialize_assignment(assignment: RoutineAssignment) -> int:
    """
    Fan an assignment out to its current targets. Returns the number of new
    routine copies created. Idempotent; coaches never receive player programs.
    """
    if not assignment.active:
        return 0

    if assignment.user_id:
        targets = [assignment.user]
    else:
        if not assignment.team.is_active:
            return 0
        targets = User.objects.filter(
            team_memberships__team=assignment.team,
            team_memberships__is_coach=False,
        )

    return sum(1 for target in targets if _materialize_one(assignment, target))


def materialize_for_user(user: User) -> int:
    """
    Give ``user`` every routine they should have (login-time self-heal).

    Covers first logins (the account did not exist when the coach assigned) and
    late team joins. Returns the number of new routine copies created.
    """
    assignments = (
        RoutineAssignment.objects.filter(active=True)
        .filter(
            Q(user=user)
            | Q(
                team__is_active=True,
                team__memberships__user=user,
                team__memberships__is_coach=False,
            )
        )
        .distinct()
    )
    return sum(1 for assignment in assignments if _materialize_one(assignment, user))


def sync_from_social_login(user: User, group_names: Iterable[str]) -> None:
    """
    Single entry point for the social-account adapter: mirror the claim, then
    heal this user's assignments.
    """
    sync_user_teams(user, group_names)
    materialize_for_user(user)
