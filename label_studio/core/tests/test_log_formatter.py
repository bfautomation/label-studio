import json
import logging

from core.utils.formatter import CustomJsonFormatter
from django.test import RequestFactory


def test_request_is_logged_without_its_query_string():
    """A refused request's log line names the method and path, never secrets carried in the query string."""
    request = RequestFactory().get('/user/signup/', {'token': 'invite-secret'})
    record = logging.LogRecord(
        'django.request', logging.WARNING, __file__, 1, 'Forbidden: %s', ('/user/signup/',), None
    )
    record.request = request

    line = json.loads(CustomJsonFormatter().format(record))

    assert line['request'] == 'GET /user/signup/'
    assert 'invite-secret' not in json.dumps(line)
