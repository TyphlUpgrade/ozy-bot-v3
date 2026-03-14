"""Shared pytest configuration."""
import pytest


# Make asyncio mode work without decorating every test file
def pytest_configure(config):
    config.addinivalue_line(
        "markers", "asyncio: mark test as async"
    )
