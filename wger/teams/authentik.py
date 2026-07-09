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

"""
Thin Authentik API client for the coach invite console.

Reuses the roster-sync credentials (AUTHENTIK_SYNC_URL / AUTHENTIK_SYNC_TOKEN;
the service account needs add/view/delete_invitation + view_flow) plus
AUTHENTIK_ENROLLMENT_FLOW (the invitation-gated enrollment flow slug). All
empty-by-default: with any piece missing the invite feature stays hidden.
"""

# Standard Library
import datetime
import logging

# Django
from django.conf import settings
from django.utils import timezone


logger = logging.getLogger(__name__)

_flow_pk_cache = {}


class InviteError(Exception):
    """Any failure talking to the IdP; message is safe to show a coach"""


def configured() -> bool:
    return bool(
        getattr(settings, 'AUTHENTIK_SYNC_URL', '')
        and getattr(settings, 'AUTHENTIK_SYNC_TOKEN', '')
        and getattr(settings, 'AUTHENTIK_ENROLLMENT_FLOW', '')
    )


def _base() -> str:
    return getattr(settings, 'AUTHENTIK_SYNC_URL', '').rstrip('/')


def _request(method: str, path: str, **kwargs):
    # Third Party
    import requests

    try:
        response = requests.request(
            method,
            f'{_base()}{path}',
            headers={'Authorization': f'Bearer {getattr(settings, "AUTHENTIK_SYNC_TOKEN", "")}'},
            timeout=15,
            **kwargs,
        )
    except requests.RequestException as error:
        raise InviteError('The identity provider is unreachable right now.') from error
    if response.status_code >= 400:
        logger.warning('authentik %s %s -> %s %s', method, path, response.status_code,
                       response.text[:200])
        raise InviteError(f'The identity provider rejected the request ({response.status_code}).')
    return response


def enrollment_flow_pk() -> str:
    slug = getattr(settings, 'AUTHENTIK_ENROLLMENT_FLOW', '')
    if slug in _flow_pk_cache:
        return _flow_pk_cache[slug]
    data = _request('GET', f'/api/v3/flows/instances/?slug={slug}').json()
    results = data.get('results') or []
    if not results:
        raise InviteError(f'Enrollment flow "{slug}" not found on the identity provider.')
    _flow_pk_cache[slug] = results[0]['pk']
    return _flow_pk_cache[slug]


def invite_url(invitation_pk: str) -> str:
    slug = getattr(settings, 'AUTHENTIK_ENROLLMENT_FLOW', '')
    return f'{_base()}/if/flow/{slug}/?itoken={invitation_pk}'


def _normalize(row: dict) -> dict:
    return {
        'pk': row['pk'],
        'name': row.get('name') or '',
        'expires': row.get('expires') or '',
        'single_use': bool(row.get('single_use')),
        'url': invite_url(row['pk']),
    }


def list_team_invitations(group_name: str) -> list:
    data = _request('GET', '/api/v3/stages/invitation/invitations/?page_size=100').json()
    invitations = []
    for row in data.get('results') or []:
        if (row.get('fixed_data') or {}).get('team_group') == group_name:
            invitations.append(_normalize(row))
    invitations.sort(key=lambda item: item['expires'], reverse=True)
    return invitations


def create_team_invitation(group_name: str, label: str, days: int, single_use: bool) -> dict:
    expires = timezone.now() + datetime.timedelta(days=days)
    row = _request(
        'POST',
        '/api/v3/stages/invitation/invitations/',
        json={
            'name': label,
            'expires': expires.isoformat(),
            'fixed_data': {'team_group': group_name},
            'single_use': single_use,
            'flow': enrollment_flow_pk(),
        },
    ).json()
    return _normalize(row)


def revoke_invitation(invitation_pk: str) -> None:
    _request('DELETE', f'/api/v3/stages/invitation/invitations/{invitation_pk}/')
