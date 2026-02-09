"""
Configuration module for MedDeck Server.

This module centralizes environment variable access and provides
a clean interface for configuration settings across the application.
"""

import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


class Config:
    """Centralized configuration class for all environment variables."""
    
    # Soniox API Key for speech-to-text
    SONIOX_API_KEY = os.getenv("SONIOX_API_KEY")
    
    # Gmail credentials for email listener
    GMAIL_USER = os.getenv("GMAIL_USER")
    GMAIL_PASS = os.getenv("GMAIL_PASS")
    
    # MongoDB connection URL
    MONGO_URL = os.getenv("MONGO_URL", "mongodb://localhost:27017")
    
    # Trusted email senders - only process emails from these addresses
    # Environment variable should be comma-separated list of email addresses
    _trusted_senders_raw = os.getenv("TRUSTED_SENDERS", "")
    TRUSTED_SENDERS = [sender.strip().lower() for sender in _trusted_senders_raw.split(",") if sender.strip()]
    
    @classmethod
    def validate_email_config(cls) -> bool:
        """
        Validate that email configuration is properly set.
        
        Returns:
            True if both GMAIL_USER and GMAIL_PASS are set, False otherwise.
        """
        return bool(cls.GMAIL_USER and cls.GMAIL_PASS)
    
    @classmethod
    def validate_soniox_config(cls) -> bool:
        """
        Validate that Soniox configuration is properly set.
        
        Returns:
            True if SONIOX_API_KEY is set, False otherwise.
        """
        return bool(cls.SONIOX_API_KEY)
