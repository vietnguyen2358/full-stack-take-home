import logging
import os
from functools import lru_cache

logger = logging.getLogger(__name__)

_client = None


def get_supabase():
    """Return a Supabase client, or None if not configured."""
    global _client
    if _client is not None:
        return _client

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")

    if not url or not key:
        logger.warning("SUPABASE_URL or SUPABASE_KEY not set — database disabled")
        return None

    from supabase import create_client

    _client = create_client(url, key)
    logger.info("Supabase client initialized: %s", url)
    return _client
