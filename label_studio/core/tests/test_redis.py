import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from label_studio.core.redis import get_job_health, is_job_on_worker, start_job_async_or_sync


def test_is_job_on_worker_does_not_call_get_job_ids():
    """Ensure membership check avoids StartedJobRegistry.get_job_ids, preventing signal usage in threads."""
    fake_connection = MagicMock()
    fake_connection.zscore.return_value = None

    with patch('label_studio.core.redis.StartedJobRegistry') as registry_cls:
        registry = registry_cls.return_value
        registry.connection = fake_connection
        registry.key = 'rq:started:low'
        registry.get_job_ids.side_effect = ValueError('should not be called')

        assert is_job_on_worker('job123', 'low') is False

        fake_connection.zscore.assert_called_once_with('rq:started:low', b'job123')

        # Ensure None job IDs short-circuit without touching Redis.
        fake_connection.zscore.reset_mock()
        assert is_job_on_worker(None, 'low') is False
        fake_connection.zscore.assert_not_called()


def test_is_job_on_worker_safe_from_non_main_thread(monkeypatch):
    """Simulate the original failure: registry.get_job_ids would raise when used from non-main threads."""
    import signal

    original_signal = signal.signal

    def fake_signal(sig, handler):
        if threading.current_thread() is not threading.main_thread():
            raise ValueError('signal only works in main thread of the main interpreter')
        return original_signal(sig, handler)

    monkeypatch.setattr(signal, 'signal', fake_signal)

    fake_connection = MagicMock()
    fake_connection.zscore.return_value = None

    class DummyRegistry:
        def __init__(self, queue_name, connection):
            self.connection = fake_connection
            self.key = f'rq:started:{queue_name}'

        def get_job_ids(self):
            # The old implementation would call this, which uses signal and fails in threads
            signal.signal(signal.SIGALRM, lambda *args: None)
            return []

    with patch('label_studio.core.redis.StartedJobRegistry', DummyRegistry):
        result: dict[str, object] = {}

        def runner():
            try:
                result['value'] = is_job_on_worker('job123', 'low')
            except Exception as exc:  # pragma: no cover - used for regression verification
                result['error'] = exc

        t = threading.Thread(target=runner)
        t.start()
        t.join()

        if 'error' in result:
            raise result['error']  # type: ignore[misc]

        assert result['value'] is False


@pytest.mark.parametrize(
    'status, workers_available, expected',
    [
        ('started', False, 'running'),
        ('finished', False, 'finished'),
        ('failed', False, 'failed'),
        ('queued', True, 'queued'),
        ('queued', False, 'stalled'),
        ('deferred', False, 'stalled'),
        ('scheduled', True, 'queued'),
    ],
)
def test_document_job_health_uses_direct_job_lookup(status, workers_available, expected):
    # A queue with no registry/list APIs ensures polling never scans other jobs.
    job = SimpleNamespace(get_status=lambda refresh: status)
    queue = SimpleNamespace(fetch_job=MagicMock(return_value=job))
    with (
        patch('label_studio.core.redis.redis_connected', return_value=True),
        patch('label_studio.core.redis.django_rq.get_queue', return_value=queue),
        patch('label_studio.core.redis.queue_has_active_workers', return_value=workers_available),
    ):
        assert get_job_health('upload-attempt', 'high') == expected
    queue.fetch_job.assert_called_once_with('upload-attempt')


def test_required_background_job_never_runs_synchronously_without_redis():
    convert_document = MagicMock()
    with patch('label_studio.core.redis.redis_connected', return_value=False):
        with pytest.raises(RuntimeError, match='Background queue is unavailable'):
            start_job_async_or_sync(convert_document, require_async=True)
    convert_document.assert_not_called()
