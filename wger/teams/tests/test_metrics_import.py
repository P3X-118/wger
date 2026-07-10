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

# wger
from wger.core.tests.base_testcase import WgerTestCase
from wger.measurements.models import (
    Category,
    Measurement,
)
from wger.teams.metrics_import import import_metrics_csv


class MetricsImportTestCase(WgerTestCase):
    def setUp(self):
        super().setUp()
        self.hitter = User.objects.create_user('jsmith', 'js@example.com', 'pw')
        self.hitter.first_name, self.hitter.last_name = 'John', 'Smith'
        self.hitter.save()
        self.pitcher = User.objects.create_user('mlopez', 'ml@example.com', 'pw')
        self.pitcher.first_name, self.pitcher.last_name = 'Maria', 'Lopez'
        self.pitcher.save()
        self.players = [self.hitter, self.pitcher]
        self.units = {'Exit Velo': 'mph', 'Throw Velo': 'mph', '60-Yard': 's'}

    def _values(self, user, metric):
        # Django
        from django.utils import timezone

        return {
            (timezone.localtime(m.date).date(), float(m.value))
            for m in Measurement.objects.filter(category__user=user, category__name=metric)
        }

    def test_hittrax_style_ignores_machine_velo(self):
        """'Velo' (machine pitch speed) must NOT become Throw Velo when the
        file also has an exit-velo column"""
        csv_text = (
            'AB,Date,Player Name,Velo,Exit Vel,Dist\n'
            '1,6/12/2026,John Smith,58.2,84.1,210\n'
            '2,6/12/2026,John Smith,58.9,88.7,245\n'
            '3,6/12/2026,John Smith,58.1,86.3,230\n'
        )
        report = import_metrics_csv(csv_text, self.players, unit_map=self.units)

        self.assertEqual(report['metrics'], ['Exit Velo'])
        self.assertEqual(report['created'], 1)
        self.assertEqual(
            self._values(self.hitter, 'Exit Velo'),
            {(datetime.date(2026, 6, 12), 88.7)},
        )
        self.assertFalse(Measurement.objects.filter(category__name='Throw Velo').exists())
        category = Category.objects.get(user=self.hitter, name='Exit Velo')
        self.assertEqual(category.unit, 'mph')

    def test_trackman_style_dual_attribution(self):
        """ExitSpeed -> Batter, RelSpeed -> Pitcher, per row"""
        csv_text = (
            'Date,Batter,Pitcher,RelSpeed,ExitSpeed\n'
            '2026-06-15,"Smith, John","Lopez, Maria",71.4,90.2\n'
            '2026-06-15,"Smith, John","Lopez, Maria",72.8,87.0\n'
        )
        report = import_metrics_csv(csv_text, self.players, unit_map=self.units)

        self.assertEqual(sorted(report['metrics']), ['Exit Velo', 'Throw Velo'])
        self.assertEqual(
            self._values(self.hitter, 'Exit Velo'),
            {(datetime.date(2026, 6, 15), 90.2)},
        )
        self.assertEqual(
            self._values(self.pitcher, 'Throw Velo'),
            {(datetime.date(2026, 6, 15), 72.8)},
        )
        # nothing crossed over
        self.assertFalse(
            Measurement.objects.filter(
                category__user=self.hitter, category__name='Throw Velo'
            ).exists()
        )

    def test_velocity_only_sheet_uses_weak_alias(self):
        csv_text = 'Name,Velo\njsmith,74.5\n'
        report = import_metrics_csv(
            csv_text, self.players, default_date=datetime.date(2026, 7, 1),
            unit_map=self.units,
        )
        self.assertEqual(report['metrics'], ['Throw Velo'])
        self.assertEqual(
            self._values(self.hitter, 'Throw Velo'),
            {(datetime.date(2026, 7, 1), 74.5)},
        )

    def test_unmatched_names_reported_and_scoped(self):
        csv_text = (
            'Player,Exit Velocity\n'
            'John Smith,85\n'
            'Somebody Else,99\n'
        )
        report = import_metrics_csv(csv_text, self.players, unit_map=self.units)
        self.assertEqual(report['unmatched'], ['Somebody Else'])
        self.assertEqual(report['players'], 1)

    def test_reimport_is_idempotent_and_keeps_max(self):
        csv_text = 'Player,Date,Exit Vel\nJohn Smith,6/12/2026,88.7\n'
        first = import_metrics_csv(csv_text, self.players, unit_map=self.units)
        second = import_metrics_csv(csv_text, self.players, unit_map=self.units)
        self.assertEqual((first['created'], first['updated']), (1, 0))
        self.assertEqual((second['created'], second['updated']), (0, 0))

        better = 'Player,Date,Exit Vel\nJohn Smith,6/12/2026,91.0\n'
        third = import_metrics_csv(better, self.players, unit_map=self.units)
        self.assertEqual((third['created'], third['updated']), (0, 1))
        self.assertEqual(
            self._values(self.hitter, 'Exit Velo'),
            {(datetime.date(2026, 6, 12), 91.0)},
        )

    def test_unusable_files_raise_readable_errors(self):
        with self.assertRaises(ValueError):
            import_metrics_csv('Foo,Bar\n1,2\n', self.players)
        with self.assertRaises(ValueError):
            import_metrics_csv('Player,Notes\nJohn Smith,ok\n', self.players)
