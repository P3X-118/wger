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
from django import forms
from django.contrib.auth.models import User
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

# wger
from wger.manager.models import Routine
from wger.teams.models import Team


class BatchAssignmentForm(forms.Form):
    """
    One coach action: N templates x (teams and/or individual players).

    The checkboxes are rendered by hand in the templates (roster rows, filter
    lists); this form only validates the submitted pk lists.
    """

    templates = forms.ModelMultipleChoiceField(
        queryset=Routine.objects.none(),
        label=_('Routines'),
    )

    teams = forms.ModelMultipleChoiceField(
        queryset=Team.objects.none(),
        required=False,
        label=_('Teams'),
    )

    players = forms.ModelMultipleChoiceField(
        queryset=User.objects.none(),
        required=False,
        label=_('Players'),
    )

    start = forms.DateField(
        label=_('Start date'),
        initial=datetime.date.today,
        widget=forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}, format='%Y-%m-%d'),
    )

    note = forms.CharField(
        label=_('Note'),
        max_length=200,
        required=False,
        widget=forms.TextInput(attrs={'class': 'form-control'}),
    )

    replace = forms.BooleanField(
        label=_('Replace current programs'),
        required=False,
        help_text=_(
            'Deactivate the targets\' previous programs and end their old '
            'routines the day before the new start.'
        ),
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )

    align_week = forms.BooleanField(
        label=_('Start on a Monday'),
        required=False,
        help_text=_(
            'Snap the start to the next Monday so week-shaped programs line '
            'up with real weeks (rest days on weekends).'
        ),
        widget=forms.CheckboxInput(attrs={'class': 'form-check-input'}),
    )

    def clean(self):
        cleaned = super().clean()
        if cleaned.get('align_week') and cleaned.get('start'):
            start = cleaned['start']
            days_ahead = (7 - start.weekday()) % 7
            if days_ahead:
                cleaned['start'] = start + datetime.timedelta(days=days_ahead)
        if not cleaned.get('teams') and not cleaned.get('players'):
            raise forms.ValidationError(_('Pick at least one team or player.'))
        return cleaned

    def __init__(self, *args, coach=None, teams=None, players=None, **kwargs):
        """
        coach:   whose template library to offer (public + own)
        teams:   selectable team targets (None => team targeting disabled)
        players: selectable player targets
        """
        super().__init__(*args, **kwargs)
        if coach is not None:
            self.fields['templates'].queryset = Routine.templates.filter(
                Q(is_public=True) | Q(user=coach)
            ).order_by('name')
        if teams is not None:
            self.fields['teams'].queryset = teams
        if players is not None:
            self.fields['players'].queryset = players
