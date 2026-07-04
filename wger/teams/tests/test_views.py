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
from django.conf import settings
from django.contrib.auth.models import (
    Group,
    User,
)
from django.test import override_settings
from django.urls import reverse

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.manager.models import Routine
from wger.teams.models import (
    RoutineAssignment,
    Team,
    TeamMembership,
)


def _wger_settings(**overrides):
    merged = dict(settings.WGER_SETTINGS)
    merged.update(overrides)
    return merged


TEAMS_ON = _wger_settings(TEAMS_ENABLED=True)


class TeamsViewsBase(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.template = Routine.objects.get(pk=1)
        self.template.is_template = True
        self.template.is_public = True
        self.template.save()

        self.team_a = Team.objects.create(slug='alpha', name='Alpha', authentik_group='team-alpha')
        self.team_b = Team.objects.create(slug='bravo', name='Bravo', authentik_group='team-bravo')

        self.coach = User.objects.create_user('coach1', 'c1@example.com', 'pw')
        self.coach.groups.add(Group.objects.get(name='gym_trainer'))
        TeamMembership.objects.create(team=self.team_a, user=self.coach, is_coach=True)

        self.player = User.objects.create_user('player1', 'p1@example.com', 'pw')
        TeamMembership.objects.create(team=self.team_a, user=self.player)

    def login(self, user):
        self.client.login(username=user.username, password='pw')


@override_settings(WGER_SETTINGS=TEAMS_ON)
class CoachConsoleAccessTestCase(TeamsViewsBase):
    def test_anonymous_redirected_to_login(self):
        response = self.client.get(reverse('teams:overview'))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_player_gets_403(self):
        self.login(self.player)
        self.assertEqual(self.client.get(reverse('teams:overview')).status_code, 403)
        self.assertEqual(
            self.client.get(reverse('teams:detail', kwargs={'pk': self.team_a.pk})).status_code,
            403,
        )

    def test_coach_sees_only_own_teams(self):
        self.login(self.coach)
        response = self.client.get(reverse('teams:overview'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Alpha')
        self.assertNotContains(response, 'Bravo')

    def test_coach_cannot_open_foreign_team(self):
        self.login(self.coach)
        response = self.client.get(reverse('teams:detail', kwargs={'pk': self.team_b.pk}))
        self.assertEqual(response.status_code, 403)

    def test_staff_sees_all_teams(self):
        self.user_login('admin')
        response = self.client.get(reverse('teams:overview'))
        self.assertContains(response, 'Alpha')
        self.assertContains(response, 'Bravo')

    def test_team_page_shows_roster(self):
        self.login(self.coach)
        response = self.client.get(reverse('teams:detail', kwargs={'pk': self.team_a.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'player1')
        self.assertContains(response, 'Log in as user')


class CoachConsoleDisabledTestCase(TeamsViewsBase):
    """Without TEAMS_ENABLED the console does not exist, even for coaches"""

    def test_coach_gets_403_when_disabled(self):
        self.login(self.coach)
        self.assertEqual(self.client.get(reverse('teams:overview')).status_code, 403)

    def test_nav_has_no_coach_entry_when_disabled(self):
        self.login(self.coach)
        response = self.client.get(reverse('core:dashboard'))
        self.assertNotContains(response, reverse('teams:overview'))


@override_settings(WGER_SETTINGS=TEAMS_ON)
class NavEntryTestCase(TeamsViewsBase):
    def test_coach_sees_nav_entry(self):
        self.login(self.coach)
        response = self.client.get(reverse('core:dashboard'))
        self.assertContains(response, reverse('teams:overview'))

    def test_player_has_no_nav_entry(self):
        self.login(self.player)
        response = self.client.get(reverse('core:dashboard'))
        self.assertNotContains(response, reverse('teams:overview'))


@override_settings(WGER_SETTINGS=TEAMS_ON)
class AssignmentViewsTestCase(TeamsViewsBase):
    def _assign_url(self, team=None):
        return reverse('teams:assign', kwargs={'team_pk': (team or self.team_a).pk})

    def _post_assignment(self, team=None, **extra):
        data = {
            'template': self.template.pk,
            'start': datetime.date.today().isoformat(),
            'note': 'preseason',
        }
        data.update(extra)
        return self.client.post(self._assign_url(team), data)

    def test_coach_assigns_to_whole_team(self):
        self.login(self.coach)
        response = self._post_assignment()
        self.assertEqual(response.status_code, 302)

        assignment = RoutineAssignment.objects.get()
        self.assertEqual(assignment.team, self.team_a)
        self.assertIsNone(assignment.user)
        self.assertEqual(assignment.assigned_by, self.coach)
        # Fan-out happened for the roster player
        self.assertTrue(Routine.objects.filter(user=self.player, is_template=False).exists())

    def test_coach_assigns_to_single_player(self):
        self.login(self.coach)
        response = self._post_assignment(player=self.player.pk)
        self.assertEqual(response.status_code, 302)

        assignment = RoutineAssignment.objects.get()
        self.assertIsNone(assignment.team)
        self.assertEqual(assignment.user, self.player)
        self.assertTrue(Routine.objects.filter(user=self.player).exists())

    def test_player_cannot_assign(self):
        self.login(self.player)
        self.assertEqual(self._post_assignment().status_code, 403)

    def test_coach_cannot_assign_on_foreign_team(self):
        self.login(self.coach)
        self.assertEqual(self._post_assignment(team=self.team_b).status_code, 403)

    def test_toggle_and_delete(self):
        self.login(self.coach)
        self._post_assignment()
        assignment = RoutineAssignment.objects.get()
        instance_count = assignment.instances.count()
        self.assertGreater(instance_count, 0)

        # Deactivate
        response = self.client.post(
            reverse('teams:assignment-toggle', kwargs={'pk': assignment.pk})
        )
        self.assertEqual(response.status_code, 302)
        assignment.refresh_from_db()
        self.assertFalse(assignment.active)

        # Reactivate
        self.client.post(reverse('teams:assignment-toggle', kwargs={'pk': assignment.pk}))
        assignment.refresh_from_db()
        self.assertTrue(assignment.active)

        # Delete: instances cascade, the player's routine copy survives
        copy_pk = assignment.instances.get(user=self.player).routine_id
        response = self.client.post(
            reverse('teams:assignment-delete', kwargs={'pk': assignment.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(RoutineAssignment.objects.exists())
        self.assertTrue(Routine.objects.filter(pk=copy_pk).exists())

    def test_player_cannot_toggle_or_delete(self):
        self.login(self.coach)
        self._post_assignment()
        assignment = RoutineAssignment.objects.get()

        self.login(self.player)
        self.assertEqual(
            self.client.post(
                reverse('teams:assignment-toggle', kwargs={'pk': assignment.pk})
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                reverse('teams:assignment-delete', kwargs={'pk': assignment.pk})
            ).status_code,
            403,
        )

    def test_get_not_allowed_on_mutations(self):
        self.login(self.coach)
        self.assertEqual(self.client.get(self._assign_url()).status_code, 405)
