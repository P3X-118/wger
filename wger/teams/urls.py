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
    path('assign/', views.assign_hub, name='assign-hub'),
    path('team/<int:pk>/', views.TeamDetailView.as_view(), name='detail'),
    path('team/<int:team_pk>/invite/', views.team_invite, name='invite'),
    path('import/', views.metrics_import_view, name='metrics-import'),
    path('player/<int:user_pk>/', views.player_detail, name='player'),
    path('player/<int:user_pk>/metric', views.player_metric, name='player-metric'),
    path('team/<int:team_pk>/assign', views.assignment_create, name='assign'),
    path('assignment/<int:pk>/toggle', views.assignment_toggle, name='assignment-toggle'),
    path('assignment/<int:pk>/delete', views.assignment_delete, name='assignment-delete'),
    path('switch/<int:user_pk>', views.switch_player, name='switch-player'),
    path('return', views.coach_return, name='coach-return'),
]
