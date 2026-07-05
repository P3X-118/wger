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
from unittest import mock

# Django
from django.conf import settings

# wger
from wger.core.tests import api_base_test
from wger.core.tests.base_testcase import WgerTestCase
from wger.exercises.models import ExerciseVideo
from wger.exercises.tests.api_mixins import ActstreamUpdateMixin


# TODO: add POST and DELETE tests
class ExerciseVideosApiTestCase(
    ActstreamUpdateMixin,
    api_base_test.BaseTestCase,
    api_base_test.ApiBaseTestCase,
    api_base_test.ApiGetTestCase,
):
    """
    Tests the exercise video resource
    """

    pk = 1
    private_resource = False
    resource = ExerciseVideo
    overview_cached = False
    data = {'is_main': True}

    def setUp(self):
        super().setUp()
        # Writes on the video API are gated on ALLOW_UPLOAD_VIDEOS, which is
        # off under settings.ci. These tests exercise the API mechanics, so
        # switch the gate on (auto-restored after each test).
        patcher = mock.patch.dict(settings.WGER_SETTINGS, {'ALLOW_UPLOAD_VIDEOS': True})
        patcher.start()
        self.addCleanup(patcher.stop)

    def get_resource_name(self):
        # The video endpoint is registered as ``video``, not ``exercisevideo``.
        return 'video'


class ExerciseVideoUploadGateTestCase(WgerTestCase):
    """
    ALLOW_UPLOAD_VIDEOS off -> the video API rejects every write but keeps
    reads open (CanContributeExerciseVideos)
    """

    def test_reads_stay_open_when_gate_off(self):
        with mock.patch.dict(settings.WGER_SETTINGS, {'ALLOW_UPLOAD_VIDEOS': False}):
            self.assertEqual(self.client.get('/api/v2/video/').status_code, 200)
            self.assertEqual(self.client.get('/api/v2/video/1/').status_code, 200)

    def test_writes_blocked_when_gate_off(self):
        self.user_login('admin')
        with mock.patch.dict(settings.WGER_SETTINGS, {'ALLOW_UPLOAD_VIDEOS': False}):
            self.assertEqual(
                self.client.post('/api/v2/video/', {'is_main': True}).status_code,
                403,
            )
            self.assertEqual(
                self.client.patch(
                    '/api/v2/video/1/',
                    data='{"is_main": true}',
                    content_type='application/json',
                ).status_code,
                403,
            )
            self.assertEqual(self.client.delete('/api/v2/video/1/').status_code, 403)

    def test_writes_allowed_again_when_gate_on(self):
        self.user_login('admin')
        with mock.patch.dict(settings.WGER_SETTINGS, {'ALLOW_UPLOAD_VIDEOS': True}):
            response = self.client.patch(
                '/api/v2/video/1/',
                data='{"is_main": true}',
                content_type='application/json',
            )
            self.assertEqual(response.status_code, 200)
