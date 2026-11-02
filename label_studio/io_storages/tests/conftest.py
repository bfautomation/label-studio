"""
Expose the shared fixture implementations from ``label_studio.tests`` so IO Storage tests
can run in isolation (pytest only auto-loads fixtures that live in parent directories).
"""

import pytest

from label_studio.tests.conftest import business_client as _business_client  # noqa: F401
from label_studio.tests.utils import project_id as _project_id  # noqa: F401

# Re-export fixtures so pytest treats them as part of this subtree.
business_client = _business_client
project_id = _project_id


@pytest.fixture
def export_storage_sync_on_save(settings):
    """For tests of the automatic copy and delete on export connections, which only happen when the app allows them."""
    settings.EXPORT_STORAGE_SYNC_ON_SAVE = True
