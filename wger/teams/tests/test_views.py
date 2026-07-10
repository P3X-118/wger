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


@override_settings(WGER_SETTINGS=TEAMS_ON)
class TodayBannerTestCase(TeamsViewsBase):
    def _materialize_for_player(self):
        # wger
        from wger.teams.services import materialize_assignment

        assignment = RoutineAssignment.objects.create(
            template=self.template,
            team=self.team_a,
            start=datetime.date.today(),
        )
        materialize_assignment(assignment)

    def test_player_with_program_sees_banner(self):
        self._materialize_for_player()
        self.login(self.player)
        response = self.client.get(reverse('core:dashboard'))
        self.assertContains(response, 'Start today')
        self.assertContains(response, self.template.name)
        self.assertContains(response, 'Alpha')

    def test_no_banner_without_program(self):
        self.login(self.player2)
        # player2 has no assignment yet
        RoutineAssignment.objects.all().delete()
        response = self.client.get(reverse('core:dashboard'))
        self.assertNotContains(response, 'Start today')

    def test_programs_nav_link_present(self):
        self.login(self.player)
        response = self.client.get(reverse('core:dashboard'))
        self.assertContains(response, reverse('manager:template:public'))


class TodayBannerDisabledTestCase(TeamsViewsBase):
    def test_dashboard_stock_when_disabled(self):
        self.login(self.player)
        response = self.client.get(reverse('core:dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'Start today')


@override_settings(
    WGER_SETTINGS=_wger_settings(
        TEAMS_ENABLED=True, NAV_HIDE=['nutrition', 'weight', 'software']
    )
)
class NavHideTestCase(TeamsViewsBase):
    def test_hidden_sections_absent(self):
        self.login(self.player)
        response = self.client.get(reverse('core:dashboard'))
        self.assertNotContains(response, 'Nutrition plans')
        self.assertNotContains(response, 'Body weight')
        self.assertNotContains(response, 'About this software')

    def test_partial_hide_keeps_other_sections(self):
        with override_settings(
            WGER_SETTINGS=_wger_settings(TEAMS_ENABLED=True, NAV_HIDE=['nutrition'])
        ):
            self.login(self.player)
            response = self.client.get(reverse('core:dashboard'))
            self.assertNotContains(response, 'Nutrition plans')
            self.assertContains(response, 'About this software')


@override_settings(WGER_SETTINGS=TEAMS_ON)
class NavDefaultSectionsTestCase(TeamsViewsBase):
    def test_stock_sections_present_by_default(self):
        self.login(self.player)
        response = self.client.get(reverse('core:dashboard'))
        self.assertContains(response, 'Nutrition plans')
        self.assertContains(response, 'About this software')


@override_settings(WGER_SETTINGS=TEAMS_ON)
class PlayerDetailViewTestCase(TeamsViewsBase):
    def _url(self, user=None):
        return reverse('teams:player', kwargs={'user_pk': (user or self.player).pk})

    def test_coach_sees_player_page(self):
        # wger
        from wger.teams.services import materialize_assignment

        assignment = RoutineAssignment.objects.create(
            template=self.template,
            team=self.team_a,
            start=datetime.date.today(),
        )
        materialize_assignment(assignment)

        self.login(self.coach)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.template.name)
        self.assertContains(response, 'Adherence')

    def test_foreign_coach_blocked(self):
        coach2 = User.objects.create_user('coach2', 'c2@example.com', 'pw')
        coach2.groups.add(Group.objects.get(name='gym_trainer'))
        TeamMembership.objects.create(team=self.team_b, user=coach2, is_coach=True)

        self.login(coach2)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_player_blocked(self):
        self.login(self.player)
        self.assertEqual(self.client.get(self._url(self.player2)).status_code, 403)

    def test_staff_allowed(self):
        self.user_login('admin')
        self.assertEqual(self.client.get(self._url()).status_code, 200)


@override_settings(WGER_SETTINGS=TEAMS_ON)
class WorkoutModeTestCase(TeamsViewsBase):
    def _player_routine(self):
        # wger
        from wger.teams.services import materialize_assignment

        assignment = RoutineAssignment.objects.create(
            template=self.template,
            team=self.team_a,
            start=datetime.date.today(),
        )
        materialize_assignment(assignment)
        return Routine.objects.get(user=self.player, is_template=False)

    def _url(self, routine):
        return reverse('train:mode', kwargs={'routine_pk': routine.pk})

    def test_owner_gets_guided_day(self):
        routine = self._player_routine()
        self.login(self.player)
        response = self.client.get(self._url(routine))
        self.assertEqual(response.status_code, 200)
        # engine shell + entries + session bar + logging endpoints
        self.assertContains(response, 'wm-entry')
        self.assertContains(response, 'wm-finish-btn')
        self.assertContains(response, '/api/v2/workoutlog/')
        self.assertContains(response, 'data-slot-entry')

    def test_out_of_range_date_shows_empty_state(self):
        routine = self._player_routine()
        self.login(self.player)
        response = self.client.get(self._url(routine) + '?date=2030-01-01')
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No workout scheduled')
        self.assertNotContains(response, 'wm-entry')

    def test_foreign_routine_404(self):
        routine = self._player_routine()
        self.login(self.player2)
        self.assertEqual(self.client.get(self._url(routine)).status_code, 404)

    def test_anonymous_redirected(self):
        routine = self._player_routine()
        response = self.client.get(self._url(routine))
        self.assertEqual(response.status_code, 302)
        self.assertIn('login', response['Location'])

    def test_disabled_flag_403(self):
        routine = self._player_routine()
        self.login(self.player)
        with override_settings(WGER_SETTINGS=_wger_settings(TEAMS_ENABLED=False)):
            self.assertEqual(self.client.get(self._url(routine)).status_code, 403)

    def test_banner_cta_links_to_workout_mode(self):
        routine = self._player_routine()
        self.login(self.player)
        response = self.client.get(reverse('core:dashboard'))
        self.assertContains(response, self._url(routine))


INVITES_ON = {
    'AUTHENTIK_SYNC_URL': 'https://idp.example.com',
    'AUTHENTIK_SYNC_TOKEN': 'token',
    'AUTHENTIK_ENROLLMENT_FLOW': 'prime-player-enrollment',
}


@override_settings(WGER_SETTINGS=TEAMS_ON, **INVITES_ON)
class TeamInviteTestCase(TeamsViewsBase):
    def _url(self):
        return reverse('teams:invite', kwargs={'team_pk': self.team_a.pk})

    def _invitation(self):
        return {
            'pk': 'abc-123',
            'name': 'alpha-invite-2026-07-09',
            'expires': '2026-07-23T00:00:00Z',
            'single_use': False,
            'url': 'https://idp.example.com/if/flow/prime-player-enrollment/?itoken=abc-123',
        }

    def test_coach_sees_invite_console(self):
        # Standard Library
        from unittest import mock

        self.login(self.coach)
        with mock.patch(
            'wger.teams.views.authentik_api.list_team_invitations',
            return_value=[self._invitation()],
        ):
            response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'invite-qr')
        self.assertContains(response, 'itoken=abc-123')
        self.assertContains(response, 'sms:')

    def test_create_invitation(self):
        # Standard Library
        from unittest import mock

        self.login(self.coach)
        with mock.patch(
            'wger.teams.views.authentik_api.create_team_invitation',
            return_value=self._invitation(),
        ) as create:
            response = self.client.post(
                self._url(), {'action': 'create', 'days': '14', 'single_use': ''}
            )
        self.assertEqual(response.status_code, 302)
        self.assertIn('show=abc-123', response['Location'])
        create.assert_called_once_with(
            group_name='team-alpha',
            label=mock.ANY,
            days=14,
            single_use=False,
        )

    def test_revoke_invitation(self):
        # Standard Library
        from unittest import mock

        self.login(self.coach)
        with mock.patch('wger.teams.views.authentik_api.revoke_invitation') as revoke:
            response = self.client.post(self._url(), {'action': 'revoke', 'pk': 'abc-123'})
        self.assertEqual(response.status_code, 302)
        revoke.assert_called_once_with('abc-123')

    def test_email_invite_sent(self):
        # Standard Library
        from unittest import mock

        # Django
        from django.core import mail

        self.login(self.coach)
        with mock.patch(
            'wger.teams.views.authentik_api.list_team_invitations', return_value=[]
        ):
            response = self.client.post(
                self._url(),
                {
                    'action': 'email',
                    'email': 'kid@example.com',
                    'link': 'https://idp.example.com/if/flow/x/?itoken=abc',
                },
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn('kid@example.com', mail.outbox[0].to)
        self.assertIn('itoken=abc', mail.outbox[0].body)

    def test_player_blocked(self):
        self.login(self.player)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_hidden_when_not_configured(self):
        self.login(self.coach)
        with override_settings(AUTHENTIK_ENROLLMENT_FLOW=''):
            self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_team_page_shows_invite_button(self):
        # Standard Library

        self.login(self.coach)
        response = self.client.get(reverse('teams:detail', kwargs={'pk': self.team_a.pk}))
        self.assertContains(response, self._url())


@override_settings(WGER_SETTINGS=TEAMS_ON, TEAMS_METRICS=['Exit Velo|mph'])
class DashboardWidgetsTestCase(TeamsViewsBase):
    def test_player_sees_widgets(self):
        self.login(self.player)
        response = self.client.get(reverse('core:dashboard'))
        self.assertContains(response, 'This week')
        self.assertContains(response, 'My numbers')
        self.assertContains(response, 'Exit Velo')
        self.assertContains(response, 'Alpha')

    def test_no_widgets_without_membership(self):
        User.objects.create_user('loner', 'l@example.com', 'pw')
        self.client.login(username='loner', password='pw')
        response = self.client.get(reverse('core:dashboard'))
        self.assertNotContains(response, 'My numbers')

    def test_coach_gets_console_card(self):
        self.login(self.coach)
        response = self.client.get(reverse('core:dashboard'))
        self.assertContains(response, 'Coach console')


@override_settings(WGER_SETTINGS=TEAMS_ON)
class WorkoutModeV2TestCase(TeamsViewsBase):
    def test_v2_engine_markup(self):
        # wger
        from wger.teams.services import materialize_assignment

        assignment = RoutineAssignment.objects.create(
            template=self.template,
            team=self.team_a,
            start=datetime.date.today(),
        )
        materialize_assignment(assignment)
        routine = Routine.objects.get(user=self.player, is_template=False)

        self.login(self.player)
        response = self.client.get(reverse('train:mode', kwargs={'routine_pk': routine.pk}))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'wm-pending')
        self.assertContains(response, 'data-slot-index')
        self.assertContains(response, 'wm-queue-')


@override_settings(WGER_SETTINGS=TEAMS_ON, TEAMS_METRICS=['Exit Velo|mph', 'Throw Velo|mph'])
class ManualMetricEntryTestCase(TeamsViewsBase):
    def _url(self):
        return reverse('teams:player-metric', kwargs={'user_pk': self.player.pk})

    def test_coach_records_reading(self):
        # wger
        from wger.measurements.models import Measurement

        self.login(self.coach)
        response = self.client.post(
            self._url(),
            {'metric': 'Exit Velo', 'value': '87.5', 'date': '2026-07-01'},
        )
        self.assertEqual(response.status_code, 302)
        measurement = Measurement.objects.get(
            category__user=self.player, category__name='Exit Velo'
        )
        self.assertEqual(float(measurement.value), 87.5)
        self.assertEqual(measurement.category.unit, 'mph')

        # same-date manual entry is authoritative (overwrites, not max)
        self.client.post(
            self._url(),
            {'metric': 'Exit Velo', 'value': '85.0', 'date': '2026-07-01'},
        )
        measurement.refresh_from_db()
        self.assertEqual(float(measurement.value), 85.0)

    def test_player_cannot_record(self):
        self.login(self.player)
        response = self.client.post(
            self._url(), {'metric': 'Exit Velo', 'value': '90', 'date': '2026-07-01'}
        )
        self.assertEqual(response.status_code, 403)

    def test_unknown_metric_rejected(self):
        self.login(self.coach)
        response = self.client.post(
            self._url(), {'metric': 'Shoe Size', 'value': '11', 'date': '2026-07-01'}
        )
        self.assertEqual(response.status_code, 403)

    def test_foreign_coach_blocked(self):
        coach2 = User.objects.create_user('coach2', 'c2@example.com', 'pw')
        coach2.groups.add(Group.objects.get(name='gym_trainer'))
        TeamMembership.objects.create(team=self.team_b, user=coach2, is_coach=True)
        self.login(coach2)
        response = self.client.post(
            self._url(), {'metric': 'Exit Velo', 'value': '90', 'date': '2026-07-01'}
        )
        self.assertEqual(response.status_code, 403)


@override_settings(WGER_SETTINGS=TEAMS_ON, TEAMS_METRICS=['Exit Velo|mph', 'Throw Velo|mph'])
class MetricsImportViewTestCase(TeamsViewsBase):
    def _url(self):
        return reverse('teams:metrics-import')

    def test_coach_uploads_csv(self):
        # Django
        from django.core.files.uploadedfile import SimpleUploadedFile

        # wger
        from wger.measurements.models import Measurement

        self.player.first_name, self.player.last_name = 'Player', 'One'
        self.player.save()
        upload = SimpleUploadedFile(
            'session.csv',
            b'Player,Date,Exit Vel\nPlayer One,7/1/2026,88.7\n',
            content_type='text/csv',
        )
        self.login(self.coach)
        response = self.client.post(self._url(), {'file': upload, 'date': '2026-07-01'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Import finished')
        self.assertTrue(
            Measurement.objects.filter(
                category__user=self.player, category__name='Exit Velo'
            ).exists()
        )

    def test_player_blocked(self):
        self.login(self.player)
        self.assertEqual(self.client.get(self._url()).status_code, 403)

    def test_bad_file_reports_error(self):
        # Django
        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile('x.csv', b'Foo,Bar\n1,2\n', content_type='text/csv')
        self.login(self.coach)
        response = self.client.post(self._url(), {'file': upload})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No player column')


@override_settings(WGER_SETTINGS=TEAMS_ON, TEAMS_METRICS=['Exit Velo|mph', 'Throw Velo|mph'])
class MetricColumnCollapseTestCase(TeamsViewsBase):
    def _team_url(self):
        return reverse('teams:detail', kwargs={'pk': self.team_a.pk})

    def test_columns_hidden_without_data(self):
        self.login(self.coach)
        response = self.client.get(self._team_url())
        self.assertNotContains(response, 'Exit Velo')

    def test_column_appears_with_data(self):
        # wger
        from wger.measurements.models import (
            Category,
            Measurement,
        )

        category = Category.objects.create(user=self.player, name='Exit Velo', unit='mph')
        Measurement.objects.create(category=category, date=datetime.date.today(), value=88)

        self.login(self.coach)
        response = self.client.get(self._team_url())
        self.assertContains(response, 'Exit Velo')
        self.assertContains(response, '88')
