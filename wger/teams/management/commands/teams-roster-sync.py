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
from django.conf import settings
from django.core.management.base import (
    BaseCommand,
    CommandError,
)

# wger
from wger.teams.services import apply_roster_snapshot


class Command(BaseCommand):
    """
    Pre-sync team rosters from the Authentik API.

    Login-time claim sync only sees users who have signed in at least once;
    this pulls the full group membership so coaches see every rostered player
    immediately (accounts are pre-created with an unusable password — they
    still sign in via SSO). Requires AUTHENTIK_SYNC_URL + AUTHENTIK_SYNC_TOKEN
    (a read-only API token).
    """

    help = 'Pre-sync team rosters (and pending players) from the Authentik API'

    def handle(self, **options):
        # Third Party
        import requests

        base_url = getattr(settings, 'AUTHENTIK_SYNC_URL', '')
        token = getattr(settings, 'AUTHENTIK_SYNC_TOKEN', '')
        if not base_url or not token:
            raise CommandError('AUTHENTIK_SYNC_URL / AUTHENTIK_SYNC_TOKEN are not configured')

        prefix = getattr(settings, 'OIDC_TEAM_GROUP_PREFIX', 'team-')
        coach_groups = set(getattr(settings, 'OIDC_COACH_GROUPS', []) or [])
        session = requests.Session()
        session.headers['Authorization'] = f'Bearer {token}'

        groups, url = [], f'{base_url.rstrip("/")}/api/v3/core/groups/'
        params = {'page_size': 100, 'include_users': 'true'}
        while url:
            response = session.get(url, params=params, timeout=30)
            response.raise_for_status()
            payload = response.json()
            groups.extend(payload.get('results', []))
            url = payload.get('pagination', {}).get('next') or None
            if url:
                # authentik paginates with ?page=N on the same endpoint
                url = f'{base_url.rstrip("/")}/api/v3/core/groups/'
                params = {
                    'page_size': 100,
                    'include_users': 'true',
                    'page': payload['pagination']['next'],
                }

        def members(group):
            return [
                {
                    'username': user['username'],
                    'email': user.get('email') or '',
                    'name': user.get('name') or '',
                }
                for user in group.get('users_obj') or []
            ]

        snapshot = {
            'teams': {
                group['name']: members(group)
                for group in groups
                if group['name'].startswith(prefix)
            },
            'coaches': {
                user['username']
                for group in groups
                if group['name'] in coach_groups
                for user in group.get('users_obj') or []
            },
        }

        if not snapshot['teams']:
            self.stdout.write(self.style.WARNING(f'no groups matching "{prefix}*" found'))
            return

        stats = apply_roster_snapshot(snapshot)
        self.stdout.write(
            self.style.SUCCESS(
                '{teams} teams synced: {users_created} users created, '
                '{memberships} memberships added, {removed} removed, '
                '{copies} routines delivered'.format(
                    teams=len(snapshot['teams']), **stats
                )
            )
        )
