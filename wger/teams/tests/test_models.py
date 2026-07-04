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
from django.db import (
    IntegrityError,
    transaction,
)
from django.db.models import ProtectedError

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.manager.models import Routine
from wger.teams.models import (
    AssignedRoutine,
    RoutineAssignment,
    Team,
    TeamMembership,
)
from wger.teams.services import copy_routine_for_user


class TeamsModelsTestCase(WgerTestCase):
    """
    Constraint-level behavior of the teams models
    """

    def setUp(self):
        super().setUp()
        self.template = Routine.objects.get(pk=3)
        self.template.is_template = True
        self.template.is_public = True
        self.template.save()

        self.team = Team.objects.create(
            slug='14u',
            name='14U',
            authentik_group='team-14u',
        )
        self.player = User.objects.get(username='test')

    def test_assignment_requires_exactly_one_target(self):
        """Neither target set -> the check constraint rejects the row"""
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoutineAssignment.objects.create(
                    template=self.template,
                    start=datetime.date.today(),
                )

    def test_assignment_rejects_both_targets(self):
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                RoutineAssignment.objects.create(
                    template=self.template,
                    team=self.team,
                    user=self.player,
                    start=datetime.date.today(),
                )

    def test_membership_unique_per_team_and_user(self):
        TeamMembership.objects.create(team=self.team, user=self.player)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TeamMembership.objects.create(team=self.team, user=self.player)

    def test_instance_unique_per_assignment_and_user(self):
        assignment = RoutineAssignment.objects.create(
            template=self.template,
            team=self.team,
            start=datetime.date.today(),
        )
        AssignedRoutine.objects.create(assignment=assignment, user=self.player)
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                AssignedRoutine.objects.create(assignment=assignment, user=self.player)

    def test_template_protected_while_assignments_exist(self):
        """PROTECT: even an inactive assignment blocks template deletion"""
        RoutineAssignment.objects.create(
            template=self.template,
            team=self.team,
            start=datetime.date.today(),
            active=False,
        )
        with self.assertRaises(ProtectedError):
            self.template.delete()

    def test_player_deleting_copy_leaves_tombstone(self):
        """SET_NULL: the instance row survives as a tombstone"""
        assignment = RoutineAssignment.objects.create(
            template=self.template,
            user=self.player,
            start=datetime.date.today(),
        )
        routine_copy = copy_routine_for_user(self.template, self.player, assignment.start)
        instance = AssignedRoutine.objects.create(
            assignment=assignment,
            user=self.player,
            routine=routine_copy,
        )

        routine_copy.delete()
        instance.refresh_from_db()
        self.assertIsNone(instance.routine)

    def test_assignment_delete_cascades_instances_but_keeps_copies(self):
        assignment = RoutineAssignment.objects.create(
            template=self.template,
            user=self.player,
            start=datetime.date.today(),
        )
        routine_copy = copy_routine_for_user(self.template, self.player, assignment.start)
        AssignedRoutine.objects.create(
            assignment=assignment,
            user=self.player,
            routine=routine_copy,
        )

        assignment.delete()
        self.assertEqual(AssignedRoutine.objects.count(), 0)
        self.assertTrue(Routine.objects.filter(pk=routine_copy.pk).exists())
