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
from django.core.management.base import BaseCommand

# wger
from wger.teams.models import RoutineAssignment
from wger.teams.services import materialize_assignment


class Command(BaseCommand):
    """
    Re-materialize team/player routine assignments (wger.teams).

    Team membership itself only syncs at SSO login (the OIDC groups claim is
    only available then); this command heals the *fan-out* of assignments to
    current members, e.g. after a database restore or bulk membership edits.
    The primary mechanism remains the login-time self-heal.
    """

    help = 'Re-materialize team/player routine assignments'

    def add_arguments(self, parser):
        parser.add_argument(
            '--assignment',
            type=int,
            default=None,
            help='Only process the assignment with this ID',
        )

    def handle(self, **options):
        assignments = RoutineAssignment.objects.filter(active=True)
        if options['assignment']:
            assignments = assignments.filter(pk=options['assignment'])

        total = 0
        for assignment in assignments:
            created = materialize_assignment(assignment)
            total += created
            if created:
                self.stdout.write(f'{assignment}: {created} new')
        self.stdout.write(self.style.SUCCESS(f'{total} routine copies created'))
