import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# Import manga-sync.py via importlib (hyphen in filename prevents normal import)
_spec = importlib.util.spec_from_file_location(
    "manga_sync", os.path.join(REPO_ROOT, "manga-sync.py")
)
manga_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(manga_sync)
sys.modules["manga_sync"] = manga_sync


def pytest_addoption(parser):
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="Run integration tests that hit real APIs and invoke mdx",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "integration: hits real MangaDex API or invokes mdx — skip by default"
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-integration"):
        skip = pytest.mark.skip(reason="pass --run-integration to run")
        for item in items:
            if "integration" in item.keywords:
                item.add_marker(skip)
