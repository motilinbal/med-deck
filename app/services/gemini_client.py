"""
Shared Gemini Client Module.

Provides a singleton genai.Client instance and shared safety settings
for the entire MedDeck application. All modules that need to call the
Gemini API should import from here instead of creating their own clients.

Usage:
    from app.services.gemini_client import get_client, get_safety_settings

    client = get_client()
    response = client.models.generate_content(...)
"""

import os
import logging
from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

# Module-level singleton
_client: genai.Client | None = None


def get_client() -> genai.Client:
    """
    Return the singleton Gemini client instance.

    Creates the client on first call and caches it for all subsequent calls.
    This eliminates per-call client creation overhead and prevents memory
    accumulation from orphaned HTTP connection pools.
    """
    global _client
    if _client is None:
        api_key = os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise ValueError("GEMINI_API_KEY not found in environment variables")
        _client = genai.Client(api_key=api_key)
        logger.info("Gemini client initialized (singleton)")
    return _client


def get_safety_settings() -> list[types.SafetySetting]:
    """
    Return standard medical safety configuration (BLOCK_NONE).

    Medical content requires all safety filters disabled to avoid
    false positives on clinical terminology.
    """
    return [
        types.SafetySetting(
            category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"
        ),
    ]
