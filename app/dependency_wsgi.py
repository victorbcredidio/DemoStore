"""Gunicorn factory for an individual loopback mock service."""
import os
from demostore.dependencies import create_dependency

app = create_dependency(os.environ["DEMO_COMPONENT"])
