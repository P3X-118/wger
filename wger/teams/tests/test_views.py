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
from wger.gym.models import Gym
from wger.manager.models import Routine
from wger.teams.models import (
    AssignedRoutine,
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

        self.player2 = User.objects.create_user('player2', 'p2@example.com', 'pw')
        TeamMembership.objects.create(team=self.team_a, user=self.player2)

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
        self.assertEqual(self.client.get(reverse('teams:assign-hub')).status_code, 403)

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

    def test_team_page_shows_roster_and_picker(self):
        self.login(self.coach)
        response = self.client.get(reverse('teams:detail', kwargs={'pk': self.team_a.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'player1')
        self.assertContains(response, 'Log in as user')
        self.assertContains(response, 'template-filter')
        self.assertContains(response, 'check-all-players')

    def test_assign_hub_renders_rosters(self):
        self.login(self.coach)
        response = self.client.get(reverse('teams:assign-hub'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Alpha')
        self.assertContains(response, 'player1')
        self.assertNotContains(response, 'Bravo')


class CoachConsoleDisabledTestCase(TeamsViewsBase):
    """Without TEAMS_ENABLED the console does not exist, even for coaches"""

    def test_coach_gets_403_when_disabled(self):
        self.login(self.coach)
        self.assertEqual(self.client.get(reverse('teams:overview')).status_code, 403)
        self.assertEqual(self.client.get(reverse('teams:assign-hub')).status_code, 403)

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

    def _post_assignment(self, team=None, templates=None, players=None, **extra):
        data = {
            'templates': templates or [self.template.pk],
            'start': datetime.date.today().isoformat(),
            'note': 'preseason',
        }
        if players:
            data['players'] = players
        data.update(extra)
        return self.client.post(self._assign_url(team), data)

    def test_no_players_checked_assigns_whole_team(self):
        self.login(self.coach)
        response = self._post_assignment()
        self.assertEqual(response.status_code, 302)

        assignment = RoutineAssignment.objects.get()
        self.assertEqual(assignment.team, self.team_a)
        self.assertIsNone(assignment.user)
        self.assertEqual(assignment.assigned_by, self.coach)
        # Fan-out reached both roster players
        self.assertTrue(Routine.objects.filter(user=self.player, is_template=False).exists())
        self.assertTrue(Routine.objects.filter(user=self.player2, is_template=False).exists())

    def test_subset_of_players_in_one_post(self):
        self.login(self.coach)
        response = self._post_assignment(players=[self.player.pk])
        self.assertEqual(response.status_code, 302)

        assignment = RoutineAssignment.objects.get()
        self.assertIsNone(assignment.team)
        self.assertEqual(assignment.user, self.player)
        self.assertTrue(Routine.objects.filter(user=self.player).exists())
        self.assertFalse(Routine.objects.filter(user=self.player2).exists())

    def test_multiple_templates_in_one_post(self):
        second = Routine.objects.get(pk=2)
        second.is_template = True
        second.is_public = True
        second.save()

        self.login(self.coach)
        response = self._post_assignment(templates=[self.template.pk, second.pk])
        self.assertEqual(response.status_code, 302)

        self.assertEqual(RoutineAssignment.objects.count(), 2)
        self.assertEqual(Routine.objects.filter(user=self.player, is_template=False).count(), 2)

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
        self.assertGreater(assignment.instances.count(), 0)

        response = self.client.post(
            reverse('teams:assignment-toggle', kwargs={'pk': assignment.pk})
        )
        self.assertEqual(response.status_code, 302)
        assignment.refresh_from_db()
        self.assertFalse(assignment.active)

        self.client.post(reverse('teams:assignment-toggle', kwargs={'pk': assignment.pk}))
        assignment.refresh_from_db()
        self.assertTrue(assignment.active)

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


@override_settings(WGER_SETTINGS=TEAMS_ON)
class AssignHubTestCase(TeamsViewsBase):
    def setUp(self):
        super().setUp()
        # coach also coaches Bravo; player1 is on BOTH teams, player3 only Bravo
        TeamMembership.objects.create(team=self.team_b, user=self.coach, is_coach=True)
        TeamMembership.objects.create(team=self.team_b, user=self.player)
        self.player3 = User.objects.create_user('player3', 'p3@example.com', 'pw')
        TeamMembership.objects.create(team=self.team_b, user=self.player3)

    def _post_hub(self, **data):
        payload = {
            'templates': [self.template.pk],
            'start': datetime.date.today().isoformat(),
            'note': '',
        }
        payload.update(data)
        return self.client.post(reverse('teams:assign-hub'), payload)

    def test_multi_team_shared_player_gets_one_copy(self):
        self.login(self.coach)
        response = self._post_hub(teams=[self.team_a.pk, self.team_b.pk])
        self.assertEqual(response.status_code, 302)

        self.assertEqual(RoutineAssignment.objects.count(), 2)
        # player1 (both teams) one copy; player2 + player3 one each
        self.assertEqual(Routine.objects.filter(user=self.player, is_template=False).count(), 1)
        self.assertEqual(AssignedRoutine.objects.filter(user=self.player).count(), 2)
        self.assertEqual(Routine.objects.filter(user=self.player2, is_template=False).count(), 1)
        self.assertEqual(Routine.objects.filter(user=self.player3, is_template=False).count(), 1)

    def test_player_picks_covered_by_team_are_skipped(self):
        self.login(self.coach)
        response = self._post_hub(teams=[self.team_a.pk], players=[self.player.pk])
        self.assertEqual(response.status_code, 302)
        # only the team assignment; no redundant individual one
        assignment = RoutineAssignment.objects.get()
        self.assertEqual(assignment.team, self.team_a)

    def test_teams_and_direct_players_combined(self):
        self.login(self.coach)
        response = self._post_hub(teams=[self.team_a.pk], players=[self.player3.pk])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(RoutineAssignment.objects.count(), 2)
        self.assertTrue(
            RoutineAssignment.objects.filter(user=self.player3, team__isnull=True).exists()
        )

    def test_foreign_team_rejected_by_form(self):
        # a second coach who only coaches Bravo cannot target Alpha
        coach2 = User.objects.create_user('coach2', 'c2@example.com', 'pw')
        coach2.groups.add(Group.objects.get(name='gym_trainer'))
        TeamMembership.objects.create(team=self.team_b, user=coach2, is_coach=True)

        self.login(coach2)
        response = self._post_hub(teams=[self.team_a.pk])
        self.assertEqual(response.status_code, 200)  # re-rendered with errors
        self.assertEqual(RoutineAssignment.objects.count(), 0)

    def test_no_targets_is_an_error(self):
        self.login(self.coach)
        response = self._post_hub()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(RoutineAssignment.objects.count(), 0)


@override_settings(WGER_SETTINGS=TEAMS_ON)
class PlayerSwitchingTestCase(TeamsViewsBase):
    def setUp(self):
        super().setUp()
        gym = Gym.objects.create(name='Prime Baseball')
        for user in (self.coach, self.player, self.player2):
            profile = user.userprofile
            profile.gym = gym
            profile.save()

    def _impersonate(self, target):
        return self.client.post(
            reverse('core:user:trainer-login', kwargs={'user_pk': target.pk})
        )

    def test_switch_player_directly(self):
        self.login(self.coach)
        self.assertEqual(self._impersonate(self.player).status_code, 302)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.player.pk)
        self.assertEqual(self.client.session['trainer.identity'], self.coach.pk)

        response = self.client.post(
            reverse('teams:switch-player', kwargs={'user_pk': self.player2.pk})
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.player2.pk)
        self.assertEqual(self.client.session['trainer.identity'], self.coach.pk)

    def test_return_lands_on_team_page(self):
        self.login(self.coach)
        self._impersonate(self.player)

        team_url = reverse('teams:detail', kwargs={'pk': self.team_a.pk})
        response = self.client.post(reverse('teams:coach-return'), {'next': team_url})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response['Location'], team_url)
        self.assertEqual(int(self.client.session['_auth_user_id']), self.coach.pk)
        self.assertNotIn('trainer.identity', self.client.session)

    def test_switch_requires_active_impersonation(self):
        self.login(self.coach)
        response = self.client.post(
            reverse('teams:switch-player', kwargs={'user_pk': self.player.pk})
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            self.client.post(reverse('teams:coach-return')).status_code,
            403,
        )

    def test_cannot_switch_to_privileged_user(self):
        coach2 = User.objects.create_user('coach2', 'c2@example.com', 'pw')
        coach2.groups.add(Group.objects.get(name='gym_trainer'))
        profile = coach2.userprofile
        profile.gym = self.coach.userprofile.gym
        profile.save()

        self.login(self.coach)
        self._impersonate(self.player)
        response = self.client.post(
            reverse('teams:switch-player', kwargs={'user_pk': coach2.pk})
        )
        self.assertEqual(response.status_code, 403)

    def test_cannot_switch_outside_coached_teams(self):
        outsider = User.objects.create_user('outsider', 'o@example.com', 'pw')
        TeamMembership.objects.create(team=self.team_b, user=outsider)
        profile = outsider.userprofile
        profile.gym = self.coach.userprofile.gym
        profile.save()

        self.login(self.coach)
        self._impersonate(self.player)
        response = self.client.post(
            reverse('teams:switch-player', kwargs={'user_pk': outsider.pk})
        )
        self.assertEqual(response.status_code, 403)

    def test_coach_bar_rendered_while_impersonating(self):
        self.login(self.coach)
        self._impersonate(self.player)
        response = self.client.get(reverse('core:dashboard'))
        self.assertContains(response, reverse('teams:coach-return'))
        self.assertContains(
            response, reverse('teams:switch-player', kwargs={'user_pk': self.player2.pk})
        )
