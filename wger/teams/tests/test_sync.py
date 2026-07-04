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
from django.contrib.auth.models import (
    Group,
    User,
)
from django.test import override_settings

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.gym.models import (
    Gym,
    GymAdminConfig,
    GymUserConfig,
)
from wger.teams.models import (
    Team,
    TeamMembership,
)
from wger.teams.services import (
    TRAINER_GROUP_NAME,
    sync_user_teams,
)


@override_settings(OIDC_COACH_GROUPS=['prime-role-coach'])
class SyncUserTeamsTestCase(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('ssoplayer', 'sso@example.com', 'pw')

    def test_creates_teams_from_prefixed_claims_only(self):
        sync_user_teams(self.user, ['team-14u', 'team-hs-pitching', 'prime-users', 'random'])

        self.assertEqual(Team.objects.count(), 2)
        team = Team.objects.get(authentik_group='team-14u')
        self.assertEqual(team.slug, '14u')
        self.assertEqual(team.name, '14u')
        self.assertTrue(TeamMembership.objects.filter(team=team, user=self.user).exists())

    def test_membership_is_claim_authoritative(self):
        sync_user_teams(self.user, ['team-14u'])
        sync_user_teams(self.user, ['team-16u'])

        memberships = TeamMembership.objects.filter(user=self.user)
        self.assertEqual(memberships.count(), 1)
        self.assertEqual(memberships.get().team.authentik_group, 'team-16u')
        # The abandoned team row itself survives
        self.assertTrue(Team.objects.filter(authentik_group='team-14u').exists())

    def test_coach_flag_and_trainer_group_follow_claim(self):
        sync_user_teams(self.user, ['team-14u', 'prime-role-coach'])

        membership = TeamMembership.objects.get(user=self.user)
        self.assertTrue(membership.is_coach)
        self.assertTrue(self.user.groups.filter(name=TRAINER_GROUP_NAME).exists())

        # Coach role revoked in the IdP
        sync_user_teams(self.user, ['team-14u'])
        membership.refresh_from_db()
        self.assertFalse(membership.is_coach)
        self.assertFalse(self.user.groups.filter(name=TRAINER_GROUP_NAME).exists())

    def test_trainer_group_recreated_with_permission_when_missing(self):
        Group.objects.filter(name=TRAINER_GROUP_NAME).delete()

        sync_user_teams(self.user, ['prime-role-coach'])

        group = Group.objects.get(name=TRAINER_GROUP_NAME)
        self.assertTrue(group.permissions.filter(codename='gym_trainer').exists())
        # Permission checks must pass for a freshly loaded user object
        fresh = User.objects.get(pk=self.user.pk)
        self.assertTrue(fresh.has_perm('gym.gym_trainer'))

    @override_settings(TEAMS_DEFAULT_GYM='Prime Baseball')
    def test_default_gym_assignment(self):
        sync_user_teams(self.user, ['team-14u'])

        gym = Gym.objects.get(name='Prime Baseball')
        self.user.userprofile.refresh_from_db()
        self.assertEqual(self.user.userprofile.gym_id, gym.pk)
        self.assertTrue(GymUserConfig.objects.filter(user=self.user, gym=gym).exists())

    @override_settings(TEAMS_DEFAULT_GYM='Prime Baseball')
    def test_default_gym_coach_gets_admin_config(self):
        sync_user_teams(self.user, ['team-14u', 'prime-role-coach'])

        gym = Gym.objects.get(name='Prime Baseball')
        self.assertTrue(GymAdminConfig.objects.filter(user=self.user, gym=gym).exists())

    @override_settings(TEAMS_DEFAULT_GYM='Prime Baseball')
    def test_existing_other_gym_left_alone(self):
        other_gym = Gym.objects.create(name='Elsewhere Fitness')
        profile = self.user.userprofile
        profile.gym = other_gym
        profile.save()

        sync_user_teams(self.user, ['team-14u'])

        profile.refresh_from_db()
        self.assertEqual(profile.gym_id, other_gym.pk)
        self.assertFalse(GymUserConfig.objects.filter(user=self.user).exists())

    def test_no_gym_when_setting_empty(self):
        sync_user_teams(self.user, ['team-14u'])
        self.user.userprofile.refresh_from_db()
        self.assertIsNone(self.user.userprofile.gym_id)

    @override_settings(OIDC_TEAM_GROUP_PREFIX='squad-')
    def test_custom_prefix(self):
        sync_user_teams(self.user, ['squad-elite', 'team-14u'])

        self.assertEqual(Team.objects.count(), 1)
        self.assertEqual(Team.objects.get().authentik_group, 'squad-elite')

    def test_slug_collision_gets_suffix(self):
        Team.objects.create(slug='14u', name='Other 14U', authentik_group='other-14u')
        sync_user_teams(self.user, ['team-14u'])

        team = Team.objects.get(authentik_group='team-14u')
        self.assertEqual(team.slug, '14u-2')
