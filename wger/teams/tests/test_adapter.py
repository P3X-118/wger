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
from unittest import mock

# Django
from django.conf import settings
from django.contrib.auth.models import User
from django.test import override_settings

# wger
from wger.core.adapters import WgerSocialAccountAdapter
from wger.core.tests.base_testcase import WgerTestCase
from wger.manager.models import Routine
from wger.teams.models import (
    RoutineAssignment,
    Team,
    TeamMembership,
)


def _sociallogin(user, groups):
    # allauth >= 65 nests the OIDC claims under 'id_token'/'userinfo'
    return mock.Mock(
        user=user,
        is_existing=True,
        account=mock.Mock(extra_data={'userinfo': {'groups': groups}}),
    )


def _sociallogin_flat(user, groups):
    # pre-65 allauth stored the claims flat; the adapter must accept both
    return mock.Mock(
        user=user,
        is_existing=True,
        account=mock.Mock(extra_data={'groups': groups}),
    )


def _wger_settings(**overrides):
    merged = dict(settings.WGER_SETTINGS)
    merged.update(overrides)
    return merged


class AdapterTeamsSyncTestCase(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.adapter = WgerSocialAccountAdapter()
        self.user = User.objects.create_user('ssoplayer', 'sso@example.com', 'pw')

    def test_disabled_flag_is_a_noop(self):
        self.adapter._sync_teams(self.user, _sociallogin(self.user, ['team-14u']))
        self.assertEqual(Team.objects.count(), 0)

    def test_enabled_flag_syncs_and_materializes(self):
        team = Team.objects.create(slug='14u', name='14U', authentik_group='team-14u')
        template = Routine.objects.get(pk=3)
        template.is_template = True
        template.is_public = True
        template.save()
        RoutineAssignment.objects.create(
            template=template,
            team=team,
            start=datetime.date.today(),
        )

        with override_settings(WGER_SETTINGS=_wger_settings(TEAMS_ENABLED=True)):
            self.adapter._sync_teams(self.user, _sociallogin(self.user, ['team-14u']))

        self.assertTrue(TeamMembership.objects.filter(team=team, user=self.user).exists())
        self.assertTrue(Routine.objects.filter(user=self.user, is_template=False).exists())

    def test_sync_failure_never_breaks_login(self):
        with override_settings(WGER_SETTINGS=_wger_settings(TEAMS_ENABLED=True)):
            with mock.patch(
                'wger.teams.services.sync_from_social_login',
                side_effect=RuntimeError('boom'),
            ):
                # Must not raise
                self.adapter._sync_teams(self.user, _sociallogin(self.user, ['team-14u']))

        self.assertEqual(TeamMembership.objects.count(), 0)

    def test_missing_groups_claim_is_tolerated(self):
        sociallogin = mock.Mock(
            user=self.user,
            is_existing=True,
            account=mock.Mock(extra_data={}),
        )
        with override_settings(WGER_SETTINGS=_wger_settings(TEAMS_ENABLED=True)):
            self.adapter._sync_teams(self.user, sociallogin)
        self.assertEqual(Team.objects.count(), 0)

    def test_flat_claims_still_supported(self):
        Team.objects.create(slug='14u', name='14U', authentik_group='team-14u')
        with override_settings(WGER_SETTINGS=_wger_settings(TEAMS_ENABLED=True)):
            self.adapter._sync_teams(self.user, _sociallogin_flat(self.user, ['team-14u']))
        self.assertTrue(
            TeamMembership.objects.filter(user=self.user, team__slug='14u').exists()
        )

    def test_pre_social_login_wires_team_sync(self):
        request = mock.Mock()
        sociallogin = _sociallogin(self.user, ['team-14u'])
        with mock.patch.object(self.adapter, '_sync_admin') as sync_admin:
            with mock.patch.object(self.adapter, '_sync_teams') as sync_teams:
                self.adapter.pre_social_login(request, sociallogin)
        sync_admin.assert_called_once_with(self.user, sociallogin)
        sync_teams.assert_called_once_with(self.user, sociallogin)
