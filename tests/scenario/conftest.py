from unittest.mock import patch

import pytest
from charm import TempoCharm
from scenario import Context


@pytest.fixture
def tempo_charm():
    with patch("charm.KubernetesServicePatch"):
        with patch("lightkube.core.client.GenericSyncClient"):
            yield TempoCharm


@pytest.fixture(scope="function")
def context(tempo_charm):
    return Context(charm_type=tempo_charm)
