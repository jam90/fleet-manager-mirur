"""Shim de compatibilidad: el cliente REST vive en `fm.adapters.mir.client`.
Se mantiene una versión porque `scripts/*.py` importan de aquí."""
from fm.adapters.mir.client import *  # noqa: F401,F403
from fm.adapters.mir.client import MirApiError, MirClient, MirStatus  # noqa: F401
