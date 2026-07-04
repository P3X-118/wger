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


class AssignmentForm(forms.Form):
    """
    Coach assigns a routine template to the whole team or a single player.
    """

    template = forms.ModelChoiceField(
        queryset=Routine.objects.none(),
        label=_('Routine'),
        widget=forms.Select(attrs={'class': 'form-select'}),
    )

    player = forms.ModelChoiceField(
        queryset=User.objects.none(),
        required=False,
        label=_('Player'),
        empty_label=_('Whole team'),
        widget=forms.Select(attrs={'class': 'form-select'}),
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

    def __init__(self, *args, coach=None, team=None, **kwargs):
        super().__init__(*args, **kwargs)
        if coach is not None:
            self.fields['template'].queryset = Routine.templates.filter(
                Q(is_public=True) | Q(user=coach)
            ).order_by('name')
        if team is not None:
            self.fields['player'].queryset = User.objects.filter(
                team_memberships__team=team,
                team_memberships__is_coach=False,
            ).order_by('username')
