"""The runtime image must consume the dependency versions exercised by CI."""

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def test_every_runtime_resolution_is_in_the_tested_dev_lock():
    def resolutions(name):
        return set(re.findall(r"^([\w.-]+)==([^ ;\\\n]+)", (ROOT / name).read_text(), re.M))

    assert resolutions("requirements.lock") <= resolutions("requirements-dev.lock")
