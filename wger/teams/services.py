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
from django.core.cache import cache
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

# wger
from wger.gym.models import (
    Gym,
    GymAdminConfig,
    GymUserConfig,
)
from wger.manager.models import (
    Routine,
    WorkoutSession,
)
from wger.measurements.models import Category as MeasurementCategory
from wger.teams.models import (
    AssignedRoutine,
    RoutineAssignment,
    Team,
    TeamMembership,
)
from wger.utils.cache import CacheKeyMapper


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


def _assignment_targets(assignment: RoutineAssignment):
    """Current player targets of an assignment (never coaches; inactive => none)"""
    if not assignment.active:
        return []
    if assignment.user_id:
        return [assignment.user]
    if not assignment.team.is_active:
        return []
    return User.objects.filter(
        team_memberships__team=assignment.team,
        team_memberships__is_coach=False,
    )


def materialize_many(assignments) -> int:
    """
    Fan a batch of assignments out to their current targets. Returns the number
    of NEW routine copies created. Idempotent per (assignment, user).

    Within the batch, a player targeted twice with the same template AND start
    (e.g. member of two teams selected in one coach action) gets a single
    routine copy; the extra assignments still get an AssignedRoutine row
    pointing at that shared copy, so the login-time self-heal never produces a
    duplicate later. Different starts never share a copy.
    """
    copies = 0
    shared: dict[tuple, Routine] = {}
    for assignment in assignments:
        for user in _assignment_targets(assignment):
            if AssignedRoutine.objects.filter(assignment=assignment, user=user).exists():
                continue
            key = (assignment.template_id, user.pk, assignment.start)
            with transaction.atomic():
                routine = shared.get(key)
                if routine is None:
                    routine = copy_routine_for_user(assignment.template, user, assignment.start)
                    shared[key] = routine
                    copies += 1
                AssignedRoutine.objects.create(assignment=assignment, user=user, routine=routine)
            logger.info('teams: materialized %s for %s', assignment, user.username)
    return copies


def materialize_assignment(assignment: RoutineAssignment) -> int:
    """
    Fan a single assignment out to its current targets. Returns the number of
    new routine copies created. Idempotent; coaches never receive player
    programs.
    """
    return materialize_many([assignment])


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
    copies = 0
    shared: dict[tuple, Routine] = {}
    for assignment in assignments:
        if AssignedRoutine.objects.filter(assignment=assignment, user=user).exists():
            continue
        key = (assignment.template_id, user.pk, assignment.start)
        with transaction.atomic():
            routine = shared.get(key)
            if routine is None:
                routine = copy_routine_for_user(assignment.template, user, assignment.start)
                shared[key] = routine
                copies += 1
            AssignedRoutine.objects.create(assignment=assignment, user=user, routine=routine)
        logger.info('teams: materialized %s for %s', assignment, user.username)
    return copies


def ensure_player_metrics(user: User) -> None:
    """
    Seed the facility's measurement categories ("Name|unit" specs from
    TEAMS_METRICS) for a user. Names are deterministic on purpose: external
    importers (HitTrax, TrackMan) target categories by exact name.
    """
    for spec in getattr(settings, 'TEAMS_METRICS', []) or []:
        name, _, unit = spec.partition('|')
        name = name.strip()[:100]
        if not name:
            continue
        MeasurementCategory.objects.get_or_create(
            user=user,
            name=name,
            defaults={'unit': unit.strip()[:30]},
        )


def sync_from_social_login(user: User, group_names: Iterable[str]) -> None:
    """
    Single entry point for the social-account adapter: mirror the claim, then
    heal this user's assignments.
    """
    sync_user_teams(user, group_names)
    if TeamMembership.objects.filter(user=user).exists():
        ensure_player_metrics(user)
    materialize_for_user(user)


def roster_adherence(players, windows=(7, 28)):
    """
    Planned-vs-logged adherence per player over trailing windows (days).

    Planned = distinct non-rest days of the player's active assigned routines
    inside the window (via the cached Routine.date_sequence); logged = distinct
    workout-session days. Returns {user_id: {window: (planned, logged, pct)}}
    with pct None when nothing was planned.
    """
    players = list(players)
    today = timezone.localdate()
    max_window = max(windows)
    window_start = today - datetime.timedelta(days=max_window - 1)

    sessions = WorkoutSession.objects.filter(
        user__in=players,
        date__gte=window_start,
        date__lte=today,
    ).values_list('user_id', 'date')
    logged: dict[int, set] = {}
    for user_id, session_date in sessions:
        logged.setdefault(user_id, set()).add(session_date)

    instances = AssignedRoutine.objects.filter(
        user__in=players,
        assignment__active=True,
        routine__isnull=False,
    ).select_related('routine')
    planned: dict[int, set] = {}
    for instance in instances:
        routine = instance.routine
        if routine.start > today or routine.end < window_start:
            continue
        for day_data in routine.date_sequence:
            if (
                window_start <= day_data.date <= today
                and day_data.day is not None
                and not day_data.day.is_rest
            ):
                planned.setdefault(instance.user_id, set()).add(day_data.date)

    result = {}
    for player in players:
        per_window = {}
        for window in windows:
            start = today - datetime.timedelta(days=window - 1)
            planned_days = {d for d in planned.get(player.pk, ()) if d >= start}
            logged_days = {d for d in logged.get(player.pk, ()) if d >= start}
            pct = None
            if planned_days:
                pct = min(100, round(100 * len(logged_days) / len(planned_days)))
            per_window[window] = (len(planned_days), len(logged_days), pct)
        result[player.pk] = per_window
    return result


def latest_metrics(players, names):
    """
    Latest measurement per (player, category name).
    Returns {user_id: {name: measurement}}.
    """
    # wger
    from wger.measurements.models import Measurement

    result: dict[int, dict] = {}
    measurements = (
        Measurement.objects.filter(
            category__user__in=players,
            category__name__in=names,
        )
        .select_related('category')
        .order_by('category__user_id', 'category__name', '-date')
    )
    for measurement in measurements:
        per_user = result.setdefault(measurement.category.user_id, {})
        per_user.setdefault(measurement.category.name, measurement)
    return result


def replace_active_assignments(teams, players, exclude_ids, new_start) -> int:
    """
    "Replace current programs": deactivate previous active assignments at the
    same scope and trim their routine copies so the new program takes over.

    Scope = assignments targeting any of ``teams``, or targeting any of
    ``players`` individually. Copies are never deleted (logs survive); their
    end date is pulled back to the day before ``new_start`` (never before
    their own start).
    """
    previous = (
        RoutineAssignment.objects.filter(active=True)
        .filter(Q(team__in=list(teams)) | Q(user__in=list(players)))
        .exclude(pk__in=exclude_ids)
    )
    replaced = 0
    for assignment in previous:
        assignment.active = False
        assignment.save(update_fields=['active'])
        replaced += 1
        for instance in assignment.instances.select_related('routine'):
            routine = instance.routine
            if routine is None or routine.end < new_start:
                continue
            routine.end = max(routine.start, new_start - datetime.timedelta(days=1))
            routine.save(update_fields=['end'])
            cache.delete(CacheKeyMapper.routine_date_sequence_key(routine.id))
    if replaced:
        logger.info('teams: replaced %s active assignments (new start %s)', replaced, new_start)
    return replaced


def apply_roster_snapshot(snapshot) -> dict:
    """
    Apply an IdP roster snapshot (from manage.py teams-roster-sync) so coaches
    see players who have never signed in.

    snapshot = {
        'teams': {group_name: [member, ...]},   # member: username/email/name
        'coaches': {username, ...},
    }
    Authoritative for the teams it contains: memberships are added AND removed.
    Never deletes users; login-time claim sync remains the fast path.
    """
    prefix = getattr(settings, 'OIDC_TEAM_GROUP_PREFIX', 'team-')
    stats = {'users_created': 0, 'memberships': 0, 'removed': 0, 'copies': 0}
    coaches = set(snapshot.get('coaches', ()))
    synced_users = []

    for group_name, members in snapshot.get('teams', {}).items():
        display_name = (group_name[len(prefix):] or group_name)[:60]
        team = Team.objects.filter(authentik_group=group_name).first()
        if team is None:
            team = Team.objects.create(
                authentik_group=group_name,
                slug=_unique_team_slug(display_name),
                name=display_name,
            )

        member_users = []
        for member in members:
            username = member['username']
            user = User.objects.filter(username=username).first()
            if user is None:
                name = (member.get('name') or '').strip()
                first, _, last = name.partition(' ')
                user = User.objects.create_user(
                    username,
                    member.get('email') or '',
                )
                user.first_name = first.strip()[:150]
                user.last_name = last.strip()[:150]
                user.set_unusable_password()
                user.save()
                stats['users_created'] += 1
            member_users.append(user)

        for user in member_users:
            is_coach = user.username in coaches
            membership, created = TeamMembership.objects.get_or_create(
                team=team,
                user=user,
                defaults={'is_coach': is_coach},
            )
            if created:
                stats['memberships'] += 1
            if membership.is_coach != is_coach:
                membership.is_coach = is_coach
                membership.save(update_fields=['is_coach'])
            _sync_trainer_group(user, is_coach)
            _ensure_default_gym(user, is_coach)
            if not is_coach:
                ensure_player_metrics(user)
            synced_users.append(user)

        removed = TeamMembership.objects.filter(team=team).exclude(
            user__in=member_users,
        )
        stats['removed'] += removed.count()
        removed.delete()

    for user in synced_users:
        stats['copies'] += materialize_for_user(user)
    return stats
