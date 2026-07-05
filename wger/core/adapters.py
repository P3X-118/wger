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
import logging

# Django
from django.conf import settings

# Third Party
from allauth.socialaccount.adapter import DefaultSocialAccountAdapter


logger = logging.getLogger(__name__)


class WgerSocialAccountAdapter(DefaultSocialAccountAdapter):
    """
    Social-account adapter for SSO (e.g. the SGC unified Authentik OIDC login).

    * Auto-provisions a wger account for SSO logins regardless of
      ``ALLOW_REGISTRATION`` (which gates only local username/password signups).
    * Maps the OIDC ``groups`` claim onto wger admin: membership of any group in
      ``settings.OIDC_ADMIN_GROUPS`` grants ``is_staff`` + ``is_superuser``;
      absence revokes them, so admin rights track the identity provider's group
      membership on every login.
    * When ``TEAMS_ENABLED`` is set, also mirrors the claim onto team rosters /
      the coach role and heals routine assignments (wger.teams).
    """

    def is_open_for_signup(self, request, sociallogin):
        return True

    @staticmethod
    def _claims(sociallogin):
        """
        Flatten the OIDC claims regardless of allauth version.

        allauth >= 65 stores extra_data nested as {'id_token': {...},
        'userinfo': {...}}; older versions stored the claims flat. Reading only
        the flat shape silently loses the 'groups' claim (and _sync_admin would
        then REVOKE staff on every login), so merge all sources.
        """
        extra = sociallogin.account.extra_data or {}
        claims = {}
        for key in ('id_token', 'userinfo'):
            value = extra.get(key)
            if isinstance(value, dict):
                claims.update(value)
        claims.update({k: v for k, v in extra.items() if k not in ('id_token', 'userinfo')})
        return claims

    def _sync_admin(self, user, sociallogin):
        admin_groups = set(getattr(settings, 'OIDC_ADMIN_GROUPS', []) or [])
        if not admin_groups or user is None or not user.pk:
            return
        claimed = set(self._claims(sociallogin).get('groups', []) or [])
        should_be_admin = bool(admin_groups & claimed)
        if user.is_staff != should_be_admin or user.is_superuser != should_be_admin:
            user.is_staff = should_be_admin
            user.is_superuser = should_be_admin
            user.save(update_fields=['is_staff', 'is_superuser'])
            logger.info(
                'wger SSO: set admin=%s for %s (matched groups %s)',
                should_be_admin,
                user,
                sorted(admin_groups & claimed),
            )

    def _apply_default_unit(self, user):
        # New SSO users default to imperial (lb) when SSO_DEFAULT_WEIGHT_UNIT is
        # set. save_user only runs at first provisioning, so returning users are
        # never overridden. weight_unit alone flips both weight and height units
        # (UserProfile.use_metric derives from it).
        default_unit = getattr(settings, 'SSO_DEFAULT_WEIGHT_UNIT', '')
        if not default_unit or user is None or not user.pk:
            return
        profile = getattr(user, 'userprofile', None)
        if profile is None:
            return
        valid = {choice[0] for choice in profile._meta.get_field('weight_unit').choices}
        if default_unit in valid and profile.weight_unit != default_unit:
            profile.weight_unit = default_unit
            profile.save(update_fields=['weight_unit'])

    def _sync_teams(self, user, sociallogin):
        # Mirror the groups claim onto team rosters / coach role and heal
        # routine assignments (wger.teams). Must never break the login itself.
        if not settings.WGER_SETTINGS.get('TEAMS_ENABLED') or user is None or not user.pk:
            return
        try:
            # wger
            from wger.teams.services import sync_from_social_login

            groups = self._claims(sociallogin).get('groups', []) or []
            sync_from_social_login(user, groups)
        except Exception:
            logger.exception('wger SSO: team sync failed for user %s', user)

    def save_user(self, request, sociallogin, form=None):
        user = super().save_user(request, sociallogin, form)
        self._sync_admin(user, sociallogin)
        self._apply_default_unit(user)
        self._sync_teams(user, sociallogin)
        return user

    def pre_social_login(self, request, sociallogin):
        super().pre_social_login(request, sociallogin)
        # Returning users: re-sync admin and teams from current group membership.
        if getattr(sociallogin, 'is_existing', False):
            self._sync_admin(sociallogin.user, sociallogin)
            self._sync_teams(sociallogin.user, sociallogin)
