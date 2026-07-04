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
from django.urls import path

# wger
from wger.teams import views


urlpatterns = [
    path('', views.TeamsOverviewView.as_view(), name='overview'),
    path('team/<int:pk>/', views.TeamDetailView.as_view(), name='detail'),
    path('team/<int:team_pk>/assign', views.assignment_create, name='assign'),
    path('assignment/<int:pk>/toggle', views.assignment_toggle, name='assignment-toggle'),
    path('assignment/<int:pk>/delete', views.assignment_delete, name='assignment-delete'),
]
