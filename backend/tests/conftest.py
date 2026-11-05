"""Make the src-layout package importable without adding runtime dependencies."""

import os
import sys
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))


@pytest.fixture(autouse=True)
def reset_process_local_api_rate_limit(monkeypatch):
    """Keep tests isolated from the API's intentionally process-local limiter and remote paid services."""

    monkeypatch.setenv("CHAT_MODEL_MODE", "template")
    try:
        from video_intelligence import api

        api.rate_limiter.reset()
    except Exception:
        # Tests that do not import the API should not need its optional runtime
        # configuration just to initialize the fixture environment.
        pass
    yield
