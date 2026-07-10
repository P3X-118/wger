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
CSV metric importer for facility data (HitTrax, TrackMan, generic sheets).

Design notes, learned from the real export shapes:

* HitTrax swing exports carry BOTH ``Velo`` (machine/pitch speed) and
  ``Exit Vel`` — the generic velo aliases are therefore only used for
  Throw Velo when no exit-velo column exists in the file (a dedicated
  pitching/velocity sheet).
* TrackMan pitch rows carry BOTH ``Batter`` and ``Pitcher`` names: exit-type
  metrics attribute to the batter column, throw-type to the pitcher column.
* Facilities track session MAXES: rows are aggregated to the max value per
  (player, metric, date); re-imports are idempotent (same-date readings only
  ever move up).
"""

# Standard Library
import csv
import datetime
import io
import re

# Django
from django.utils import timezone

# wger
from wger.measurements.models import (
    Category as MeasurementCategory,
    Measurement,
)


def _at_noon(day: datetime.date) -> datetime.datetime:
    """Measurement.date is a DateTimeField — store noon local (DST-safe)"""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(12, 0)))


BATTER_HEADERS = ('batter', 'batter name')
PITCHER_HEADERS = ('pitcher', 'pitcher name')
GENERIC_PLAYER_HEADERS = ('player name', 'player', 'athlete', 'name')
DATE_HEADERS = ('date', 'session date', 'day')

# metric -> (side, strong aliases, weak aliases). Weak throw aliases are only
# used when the file has no exit-velo column (see module docstring).
METRIC_SPECS = (
    (
        'Exit Velo',
        'batter',
        ('exitspeed', 'exit speed', 'exit vel', 'exit velo', 'exit velocity',
         'exit vel (mph)', 'max ev', 'maxev', 'ev (mph)'),
        (),
    ),
    (
        'Throw Velo',
        'pitcher',
        ('relspeed', 'rel speed', 'pitch vel', 'pitch velo', 'pitching velo',
         'throw velo', 'throwing velo', 'max vel'),
        ('velo', 'velocity'),
    ),
    (
        '60-Yard',
        'batter',
        ('60-yard', '60 yard', '60yd', '60 yd', 'sixty yard'),
        (),
    ),
)

_number_re = re.compile(r'-?\d+(?:\.\d+)?')


def _norm(value) -> str:
    return re.sub(r'\s+', ' ', str(value or '').strip().lower())


def _parse_value(raw):
    match = _number_re.search(str(raw or ''))
    return float(match.group()) if match else None


def _parse_date(raw, fallback):
    text = str(raw or '').strip().split(' ')[0].split('T')[0]
    for fmt in ('%Y-%m-%d', '%m/%d/%Y', '%m/%d/%y', '%d.%m.%Y'):
        try:
            return datetime.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return fallback


def _name_index(players):
    """username / 'first last' / 'last, first' / email -> user"""
    index = {}
    for player in players:
        keys = {player.username.lower()}
        full = _norm(player.get_full_name())
        if full:
            keys.add(full)
            parts = full.split(' ')
            if len(parts) >= 2:
                first, last = ' '.join(parts[:-1]), parts[-1]
                keys.add(f'{last}, {first}')
                keys.add(f'{last},{first}')
        if player.email:
            keys.add(player.email.lower())
        for key in keys:
            index.setdefault(key, player)
    return index


def import_metrics_csv(text, players, default_date=None, unit_map=None):
    """
    Parse a CSV export and write per-player session maxes as Measurements.
    ``players`` bounds who rows may attribute to (the coach's rosters).
    Raises ValueError with a coach-readable message on unusable files.
    """
    default_date = default_date or timezone.localdate()
    unit_map = unit_map or {}
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ValueError('The file has no header row.')
    headers = {_norm(name): name for name in reader.fieldnames if name}

    def find(aliases):
        return next((headers[alias] for alias in aliases if alias in headers), None)

    batter_col = find(BATTER_HEADERS)
    pitcher_col = find(PITCHER_HEADERS)
    generic_col = find(GENERIC_PLAYER_HEADERS)
    if not (batter_col or pitcher_col or generic_col):
        raise ValueError(
            'No player column found (looked for Batter, Pitcher, Player, Name, Athlete).'
        )
    date_col = find(DATE_HEADERS)

    has_exit_column = any(alias in headers for alias in METRIC_SPECS[0][2])
    metric_columns = []  # (metric, value column, player column)
    used_columns = set()
    for metric, side, strong, weak in METRIC_SPECS:
        aliases = strong if has_exit_column else (*strong, *weak)
        column = next(
            (headers[a] for a in aliases if a in headers and headers[a] not in used_columns),
            None,
        )
        if column is None:
            continue
        used_columns.add(column)
        if side == 'pitcher':
            player_column = pitcher_col or generic_col or batter_col
        else:
            player_column = batter_col or generic_col or pitcher_col
        metric_columns.append((metric, column, player_column))
    if not metric_columns:
        raise ValueError(
            'No metric columns found (ExitSpeed / Exit Vel, RelSpeed / Pitch Vel, 60-Yard).'
        )

    index = _name_index(players)
    best = {}  # (user_id, metric, date) -> (value, user)
    unmatched, skipped, total_rows = set(), 0, 0
    for row in reader:
        total_rows += 1
        row_date = _parse_date(row.get(date_col) if date_col else None, default_date)
        row_scored = False
        for metric, value_column, player_column in metric_columns:
            value = _parse_value(row.get(value_column))
            if value is None or value <= 0:
                continue
            raw_name = row.get(player_column)
            name = _norm(raw_name)
            if not name:
                continue
            user = index.get(name)
            if user is None:
                unmatched.add(str(raw_name).strip())
                continue
            row_scored = True
            key = (user.pk, metric, row_date)
            if value > best.get(key, (0, None))[0]:
                best[key] = (value, user)
        if not row_scored:
            skipped += 1

    created = updated = 0
    players_touched = set()
    for (user_id, metric, metric_date), (value, user) in best.items():
        category, _ = MeasurementCategory.objects.get_or_create(
            user=user,
            name=metric,
            defaults={'unit': unit_map.get(metric, '')},
        )
        existing = Measurement.objects.filter(
            category=category, date__date=metric_date
        ).first()
        if existing is not None:
            if value > float(existing.value):
                existing.value = value
                existing.save(update_fields=['value'])
                updated += 1
        else:
            Measurement.objects.create(
                category=category, date=_at_noon(metric_date), value=value
            )
            created += 1
        players_touched.add(user_id)

    return {
        'created': created,
        'updated': updated,
        'players': len(players_touched),
        'rows': total_rows,
        'skipped': skipped,
        'unmatched': sorted(unmatched),
        'metrics': [metric for metric, _, _ in metric_columns],
    }
