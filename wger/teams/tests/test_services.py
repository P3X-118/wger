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

# Django
from django.contrib.auth.models import User
from django.test import override_settings

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.manager.models import (
    Label,
    Routine,
)
from wger.teams.models import (
    AssignedRoutine,
    RoutineAssignment,
    Team,
    TeamMembership,
)
from wger.teams.services import (
    _CONFIG_RELATIONS,
    copy_routine_for_user,
    materialize_assignment,
    materialize_for_user,
    materialize_many,
)


class TeamsServiceBase(WgerTestCase):
    def setUp(self):
        super().setUp()
        # Routine 1 is the only fixture routine with actual structure
        # (days -> slots -> entries -> config rows)
        self.template = Routine.objects.get(pk=1)
        self.template.is_template = True
        self.template.is_public = True
        self.template.save()
        Label.objects.create(
            routine=self.template,
            start_offset=0,
            end_offset=6,
            label='Ramp-up week',
        )

        self.team = Team.objects.create(slug='14u', name='14U', authentik_group='team-14u')
        self.player1 = User.objects.create_user('player1', 'p1@example.com', 'pw')
        self.player2 = User.objects.create_user('player2', 'p2@example.com', 'pw')
        self.coach = User.objects.create_user('coach1', 'c1@example.com', 'pw')
        TeamMembership.objects.create(team=self.team, user=self.player1)
        TeamMembership.objects.create(team=self.team, user=self.player2)
        TeamMembership.objects.create(team=self.team, user=self.coach, is_coach=True)


class CopyRoutineForUserTestCase(TeamsServiceBase):
    """
    The deep copy must be a faithful clone of the template, re-dated and owned
    by the target user.
    """

    def test_copy_parity(self):
        start = datetime.date.today() + datetime.timedelta(days=3)
        routine_copy = copy_routine_for_user(self.template, self.player1, start)

        self.assertEqual(routine_copy.user, self.player1)
        self.assertEqual(routine_copy.name, self.template.name)
        self.assertEqual(routine_copy.description, self.template.description)
        self.assertFalse(routine_copy.is_template)
        self.assertFalse(routine_copy.is_public)
        self.assertEqual(routine_copy.start, start)
        self.assertEqual(routine_copy.end, start + self.template.duration)

        # Labels are copied (the upstream copy view misses these)
        self.assertEqual(routine_copy.labels.count(), self.template.labels.count())
        label_copy = routine_copy.labels.get()
        self.assertEqual(label_copy.label, 'Ramp-up week')
        self.assertEqual(label_copy.start_offset, 0)
        self.assertEqual(label_copy.end_offset, 6)

        # Structure parity: days -> slots -> entries -> every config relation
        days_orig = list(self.template.days.all())
        days_copy = list(routine_copy.days.all())
        self.assertEqual(len(days_orig), len(days_copy))
        self.assertGreater(len(days_orig), 0)

        for day_orig, day_copy in zip(days_orig, days_copy):
            self.assertEqual(day_orig.name, day_copy.name)
            self.assertEqual(day_orig.type, day_copy.type)
            self.assertEqual(day_orig.order, day_copy.order)
            self.assertEqual(day_orig.is_rest, day_copy.is_rest)
            self.assertEqual(day_orig.need_logs_to_advance, day_copy.need_logs_to_advance)

            slots_orig = list(day_orig.slots.all())
            slots_copy = list(day_copy.slots.all())
            self.assertEqual(len(slots_orig), len(slots_copy))

            for slot_orig, slot_copy in zip(slots_orig, slots_copy):
                self.assertEqual(slot_orig.order, slot_copy.order)
                self.assertEqual(slot_orig.comment, slot_copy.comment)

                entries_orig = list(slot_orig.entries.all())
                entries_copy = list(slot_copy.entries.all())
                self.assertEqual(len(entries_orig), len(entries_copy))

                for entry_orig, entry_copy in zip(entries_orig, entries_copy):
                    self.assertEqual(entry_orig.exercise_id, entry_copy.exercise_id)
                    self.assertEqual(entry_orig.type, entry_copy.type)
                    self.assertEqual(entry_orig.order, entry_copy.order)
                    self.assertEqual(entry_orig.repetition_unit_id, entry_copy.repetition_unit_id)
                    self.assertEqual(entry_orig.weight_unit_id, entry_copy.weight_unit_id)

                    for relation in _CONFIG_RELATIONS:
                        configs_orig = list(getattr(entry_orig, relation).all())
                        configs_copy = list(getattr(entry_copy, relation).all())
                        self.assertEqual(
                            len(configs_orig),
                            len(configs_copy),
                            f'config count mismatch for {relation}',
                        )
                        for config_orig, config_copy in zip(configs_orig, configs_copy):
                            self.assertEqual(config_orig.iteration, config_copy.iteration)
                            self.assertEqual(config_orig.value, config_copy.value)
                            self.assertEqual(config_orig.operation, config_copy.operation)

    def test_copy_has_configs(self):
        """The fixture routine actually exercises the config-copy loop"""
        routine_copy = copy_routine_for_user(self.template, self.player1, datetime.date.today())
        total_configs = 0
        for day in routine_copy.days.all():
            for slot in day.slots.all():
                for entry in slot.entries.all():
                    for relation in _CONFIG_RELATIONS:
                        total_configs += getattr(entry, relation).count()
        self.assertGreater(total_configs, 0)


class MaterializeAssignmentTestCase(TeamsServiceBase):
    def _assignment(self, **kwargs):
        defaults = {
            'template': self.template,
            'team': self.team,
            'start': datetime.date.today(),
        }
        defaults.update(kwargs)
        return RoutineAssignment.objects.create(**defaults)

    def test_team_fanout_excludes_coaches(self):
        assignment = self._assignment()
        created = materialize_assignment(assignment)

        self.assertEqual(created, 2)
        self.assertEqual(assignment.instances.count(), 2)
        self.assertTrue(Routine.objects.filter(user=self.player1, is_template=False).exists())
        self.assertTrue(Routine.objects.filter(user=self.player2, is_template=False).exists())
        self.assertFalse(Routine.objects.filter(user=self.coach).exists())

    def test_fanout_is_idempotent(self):
        assignment = self._assignment()
        self.assertEqual(materialize_assignment(assignment), 2)
        self.assertEqual(materialize_assignment(assignment), 0)
        self.assertEqual(assignment.instances.count(), 2)

    def test_tombstone_blocks_rematerialization(self):
        assignment = self._assignment()
        materialize_assignment(assignment)

        # Player 1 deletes their copy
        instance = assignment.instances.get(user=self.player1)
        instance.routine.delete()
        instance.refresh_from_db()
        self.assertIsNone(instance.routine)

        self.assertEqual(materialize_assignment(assignment), 0)
        self.assertFalse(Routine.objects.filter(user=self.player1).exists())

    def test_inactive_assignment_and_team_skipped(self):
        assignment = self._assignment(active=False)
        self.assertEqual(materialize_assignment(assignment), 0)

        self.team.is_active = False
        self.team.save()
        active_assignment = self._assignment()
        self.assertEqual(materialize_assignment(active_assignment), 0)

    def test_single_player_target(self):
        assignment = self._assignment(team=None, user=self.player1)
        self.assertEqual(materialize_assignment(assignment), 1)
        self.assertTrue(Routine.objects.filter(user=self.player1).exists())
        self.assertFalse(Routine.objects.filter(user=self.player2).exists())


class MaterializeManyTestCase(TeamsServiceBase):
    """Batch fan-out: one coach action across several targets"""

    def setUp(self):
        super().setUp()
        self.team_b = Team.objects.create(slug='16u', name='16U', authentik_group='team-16u')
        self.player3 = User.objects.create_user('player3', 'p3@example.com', 'pw')
        # player1 is on BOTH teams
        TeamMembership.objects.create(team=self.team_b, user=self.player1)
        TeamMembership.objects.create(team=self.team_b, user=self.player3)

    def _assignment(self, **kwargs):
        defaults = {'template': self.template, 'start': datetime.date.today()}
        defaults.update(kwargs)
        return RoutineAssignment.objects.create(**defaults)

    def test_shared_player_gets_one_copy_across_teams(self):
        assignment_a = self._assignment(team=self.team)
        assignment_b = self._assignment(team=self.team_b)

        copies = materialize_many([assignment_a, assignment_b])

        # player1 (both teams) once, player2 once, player3 once
        self.assertEqual(copies, 3)
        self.assertEqual(Routine.objects.filter(user=self.player1, is_template=False).count(), 1)
        # ...but BOTH assignments carry an instance row for player1, sharing the copy
        instance_a = assignment_a.instances.get(user=self.player1)
        instance_b = assignment_b.instances.get(user=self.player1)
        self.assertEqual(instance_a.routine_id, instance_b.routine_id)
        # and the login self-heal has nothing left to do
        self.assertEqual(materialize_for_user(self.player1), 0)

    def test_different_starts_never_share(self):
        assignment_a = self._assignment(user=self.player1)
        assignment_b = self._assignment(
            user=self.player1,
            start=datetime.date.today() + datetime.timedelta(days=30),
        )
        copies = materialize_many([assignment_a, assignment_b])
        self.assertEqual(copies, 2)
        self.assertEqual(Routine.objects.filter(user=self.player1).count(), 2)

    def test_login_heal_also_dedupes_same_template_and_start(self):
        late = User.objects.create_user('player9', 'p9@example.com', 'pw')
        TeamMembership.objects.create(team=self.team, user=late)
        TeamMembership.objects.create(team=self.team_b, user=late)
        self._assignment(team=self.team)
        self._assignment(team=self.team_b)

        copies = materialize_for_user(late)
        self.assertEqual(copies, 1)
        self.assertEqual(Routine.objects.filter(user=late).count(), 1)
        self.assertEqual(AssignedRoutine.objects.filter(user=late).count(), 2)


class MaterializeForUserTestCase(TeamsServiceBase):
    def test_login_self_heal_covers_team_and_user_targets(self):
        team_assignment = RoutineAssignment.objects.create(
            template=self.template,
            team=self.team,
            start=datetime.date.today(),
        )
        user_assignment = RoutineAssignment.objects.create(
            template=self.template,
            user=self.player1,
            start=datetime.date.today(),
        )

        # Same template AND same start via two assignments -> ONE shared copy,
        # but both assignments get their instance row
        created = materialize_for_user(self.player1)
        self.assertEqual(created, 1)
        self.assertEqual(Routine.objects.filter(user=self.player1).count(), 1)
        instance_a = AssignedRoutine.objects.get(assignment=team_assignment, user=self.player1)
        instance_b = AssignedRoutine.objects.get(assignment=user_assignment, user=self.player1)
        self.assertEqual(instance_a.routine_id, instance_b.routine_id)

        # Second run: nothing new
        self.assertEqual(materialize_for_user(self.player1), 0)

    def test_late_joiner_gets_existing_assignment(self):
        late = User.objects.create_user('player3', 'p3@example.com', 'pw')
        assignment = RoutineAssignment.objects.create(
            template=self.template,
            team=self.team,
            start=datetime.date.today(),
        )
        materialize_assignment(assignment)
        self.assertFalse(Routine.objects.filter(user=late).exists())

        # ...the player joins the team later (next login sync would do this)
        TeamMembership.objects.create(team=self.team, user=late)
        self.assertEqual(materialize_for_user(late), 1)
        self.assertTrue(Routine.objects.filter(user=late).exists())

    def test_coach_gets_nothing(self):
        RoutineAssignment.objects.create(
            template=self.template,
            team=self.team,
            start=datetime.date.today(),
        )
        self.assertEqual(materialize_for_user(self.coach), 0)
        self.assertFalse(Routine.objects.filter(user=self.coach).exists())


class PlayerMetricsTestCase(TeamsServiceBase):
    def test_metrics_seeded_idempotently(self):
        # wger
        from wger.measurements.models import Category
        from wger.teams.services import ensure_player_metrics

        with override_settings(TEAMS_METRICS=['Exit Velo|mph', 'Throw Velo|mph']):
            ensure_player_metrics(self.player1)
            ensure_player_metrics(self.player1)

        categories = Category.objects.filter(user=self.player1)
        self.assertEqual(categories.count(), 2)
        self.assertEqual(categories.get(name='Exit Velo').unit, 'mph')


class RosterAdherenceTestCase(TeamsServiceBase):
    def test_planned_vs_logged(self):
        # Django
        from django.utils import timezone

        # wger
        from wger.manager.models import WorkoutSession
        from wger.teams.services import (
            copy_routine_for_user,
            roster_adherence,
        )

        today = timezone.localdate()
        routine = copy_routine_for_user(
            self.template, self.player1, today - datetime.timedelta(days=6)
        )
        assignment = RoutineAssignment.objects.create(
            template=self.template,
            user=self.player1,
            start=routine.start,
        )
        AssignedRoutine.objects.create(
            assignment=assignment, user=self.player1, routine=routine
        )

        result = roster_adherence([self.player1], windows=(7,))
        planned, logged, pct = result[self.player1.pk][7]
        self.assertGreater(planned, 0)
        self.assertEqual(logged, 0)
        self.assertEqual(pct, 0)

        for offset in (0, 1):
            WorkoutSession.objects.create(
                user=self.player1,
                date=today - datetime.timedelta(days=offset),
            )
        planned2, logged2, pct2 = roster_adherence([self.player1], windows=(7,))[
            self.player1.pk
        ][7]
        self.assertEqual(planned2, planned)
        self.assertEqual(logged2, 2)
        self.assertEqual(pct2, min(100, round(100 * 2 / planned)))


class ReplaceAssignmentsTestCase(TeamsServiceBase):
    def test_replace_deactivates_and_trims(self):
        # wger
        from wger.teams.services import replace_active_assignments

        old = RoutineAssignment.objects.create(
            template=self.template,
            team=self.team,
            start=datetime.date.today() - datetime.timedelta(days=10),
        )
        materialize_assignment(old)
        old_copy = old.instances.get(user=self.player1).routine
        self.assertGreater(old_copy.end, datetime.date.today())

        new_start = datetime.date.today() + datetime.timedelta(days=1)
        replaced = replace_active_assignments(
            teams=[self.team], players=[], exclude_ids=[], new_start=new_start
        )
        self.assertEqual(replaced, 1)
        old.refresh_from_db()
        self.assertFalse(old.active)
        old_copy.refresh_from_db()
        self.assertEqual(old_copy.end, new_start - datetime.timedelta(days=1))


class RosterSnapshotTestCase(TeamsServiceBase):
    @override_settings(TEAMS_METRICS=['Exit Velo|mph'])
    def test_apply_snapshot_precreates_and_materializes(self):
        # wger
        from wger.measurements.models import Category
        from wger.teams.services import apply_roster_snapshot

        assignment = RoutineAssignment.objects.create(
            template=self.template,
            team=self.team,
            start=datetime.date.today(),
        )
        materialize_assignment(assignment)

        snapshot = {
            'teams': {
                'team-14u': [
                    {'username': 'player1', 'email': 'p1@example.com', 'name': ''},
                    {'username': 'newkid', 'email': 'nk@example.com', 'name': 'New Kid'},
                    {'username': 'coach1', 'email': 'c1@example.com', 'name': ''},
                ],
            },
            'coaches': {'coach1'},
        }
        stats = apply_roster_snapshot(snapshot)

        self.assertEqual(stats['users_created'], 1)
        newkid = User.objects.get(username='newkid')
        self.assertEqual(newkid.first_name, 'New')
        self.assertEqual(newkid.last_name, 'Kid')
        self.assertFalse(newkid.has_usable_password())
        self.assertTrue(
            TeamMembership.objects.filter(team=self.team, user=newkid, is_coach=False).exists()
        )
        # pre-created player got the active program + facility metrics
        self.assertTrue(Routine.objects.filter(user=newkid, is_template=False).exists())
        self.assertTrue(Category.objects.filter(user=newkid).exists())
        # player2 was not in the snapshot -> membership removed (authoritative)
        self.assertFalse(
            TeamMembership.objects.filter(team=self.team, user=self.player2).exists()
        )
        # coach flag honored
        membership = TeamMembership.objects.get(team=self.team, user=self.coach)
        self.assertTrue(membership.is_coach)
