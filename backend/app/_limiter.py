"""Shared slowapi rate-limiter instance.

Import this module instead of instantiating Limiter in individual routers
so that all routers share the same instance that is registered on app.state.
"""

from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
