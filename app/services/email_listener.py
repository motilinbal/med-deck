"""
Email Listener Service for MedDeck Server.

This module provides a background service that polls Gmail for emails with
"Patient X" or "Patient [number]" subject lines. It handles:
- Creating new cards for "Patient X" emails
- Finding existing cards for "Patient [number]" emails
- Staging email content (text chunks and PDFs) for user approval
- Notifying the frontend via WebSocket

Usage:
    from app.services.email_listener import EmailListenerService
    
    listener = EmailListenerService()
    asyncio.create_task(listener.start())  # Start in background
"""

import asyncio
import logging
import re
import traceback
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from imap_tools import MailBox, AND

from app.utils.text import clean_email_body, extract_chunks
from database import (
    create_empty_card,
    get_card_by_serial,
    create_pending_ingestion,
)
from models import PendingIngestion
from config import Config
from app.services.notification_hub import notification_hub

logger = logging.getLogger(__name__)

# Regex to match "Patient X" or "Patient [number]" (case insensitive, flexible whitespace)
PATIENT_SUBJECT_REGEX = re.compile(r"^\s*patient\s+(x|\d+)\s*$", re.IGNORECASE)


@dataclass
class EmailData:
    """Simple data class to hold email information from IMAP fetch."""
    uid: str
    subject: str
    body: str
    sender: str
    attachments: List[Tuple[str, bytes, str]]  # (filename, data, content_type)


class EmailListenerService:
    """
    Background service that polls Gmail for medical record emails.
    
    Uses a split architecture:
    - start(): Async loop that orchestrates fetching and processing
    - _fetch_unseen_emails(): Sync method for blocking IMAP operations
    - _process_single_email(): Async method for database operations and notifications
    """
    
    def __init__(self):
        self.is_running = False
        self.gmail_user = Config.GMAIL_USER
        self.gmail_pass = Config.GMAIL_PASS
    
    async def start(self):
        """
        Main entry point for the email listener service.
        
        Runs an infinite loop that:
        1. Fetches unseen emails (in thread to avoid blocking)
        2. Processes each email asynchronously
        3. Waits before next poll
        """
        if not Config.validate_email_config():
            logger.error("Email configuration incomplete. Cannot start email listener.")
            logger.error("Please set GMAIL_USER and GMAIL_PASS in .env file.")
            return
        
        logger.info(f"Starting Email Listener for user: {self.gmail_user}")
        self.is_running = True
        
        while self.is_running:
            try:
                # Fetch unseen emails (blocking I/O in separate thread)
                emails = await asyncio.to_thread(self._fetch_unseen_emails)
                
                # Process each email asynchronously
                for email_data in emails:
                    try:
                        await self._process_single_email(email_data)
                    except Exception as e:
                        logger.error(f"Error processing email {email_data.uid}: {e}")
                        logger.error(traceback.format_exc())
                
            except Exception as e:
                logger.error(f"Error in email listener loop: {e}")
                logger.error(traceback.format_exc())
            
            # Wait before next poll
            await asyncio.sleep(10)
    
    def _fetch_unseen_emails(self) -> List[EmailData]:
        """
        Synchronous method to fetch unseen emails from Gmail.
        
        Returns:
            List of EmailData objects containing email metadata and content.
            Returns empty list on connection/login errors.
        """
        emails = []
        
        try:
            with MailBox('imap.gmail.com').login(self.gmail_user, self.gmail_pass) as mailbox:
                # Fetch unseen emails, oldest first, mark as seen to prevent re-processing
                for msg in mailbox.fetch(AND(seen=False), mark_seen=True, reverse=False):
                    try:
                        # Extract attachments (only PDFs)
                        attachments = []
                        for att in msg.attachments:
                            filename = att.filename.lower()
                            if filename.endswith('.pdf') or att.content_type == 'application/pdf':
                                attachments.append((att.filename, att.payload, att.content_type))
                        
                        # Get body text (prefer plain text, fallback to HTML)
                        body = msg.text or msg.html or ""
                        
                        email_data = EmailData(
                            uid=str(msg.uid),
                            subject=msg.subject or "",
                            body=body,
                            sender=msg.from_,
                            attachments=attachments
                        )
                        emails.append(email_data)
                        
                    except Exception as e:
                        logger.error(f"Error parsing email {msg.uid}: {e}")
                        continue
                        
        except Exception as e:
            logger.error(f"IMAP connection/fetch error: {e}")
        
        return emails
    
    async def _process_single_email(self, email_data: EmailData):
        """
        Process a single email: parse subject, find/create card, stage data, notify.
        
        Args:
            email_data: EmailData object containing email content
        """
        # Parse subject line
        match = PATIENT_SUBJECT_REGEX.match(email_data.subject)
        if not match:
            logger.debug(f"Skipping email {email_data.uid}: subject doesn't match pattern")
            return
        
        identifier = match.group(1).lower()
        logger.info(f"Processing email {email_data.uid} for Patient {identifier}")
        
        # Determine card and whether we created a new one
        card_id: Optional[str] = None
        created_new_card = False
        
        if identifier == 'x':
            # "Patient X" - Create new card
            try:
                new_card = await create_empty_card()
                card_id = new_card['id']
                created_new_card = True
                logger.info(f"Created new card {card_id} for Patient X")
            except Exception as e:
                logger.error(f"Failed to create new card: {e}")
                return
        else:
            # "Patient [number]" - Find existing card
            serial = int(identifier)
            card = await get_card_by_serial(serial)
            if not card:
                logger.error(f"No card found with serial {serial}")
                return
            card_id = card['id']
            logger.info(f"Found existing card {card_id} for Patient {serial}")
        
        # Clean and extract text chunks
        cleaned_body = clean_email_body(email_data.body)
        chunks = extract_chunks(cleaned_body)
        
        # Process PDF attachment (only first PDF if multiple)
        pdf_filename = None
        pdf_data = None
        has_pdf = False
        
        if email_data.attachments:
            # Take the first PDF attachment
            filename, data, _ = email_data.attachments[0]
            pdf_filename = filename
            pdf_data = data
            has_pdf = True
            logger.info(f"Extracted PDF attachment: {filename} ({len(data)} bytes)")
        
        # Create PendingIngestion record
        pending_data = PendingIngestion(
            card_id=card_id,
            email_uid=email_data.uid,
            sender=email_data.sender,
            received_at=datetime.utcnow(),
            created_new_card=created_new_card,
            clean_body_chunks=chunks,
            has_pdf=has_pdf,
            pdf_filename=pdf_filename,
            pdf_data=pdf_data,
            status="waiting_approval"
        )
        
        try:
            pending_id = await create_pending_ingestion(pending_data)
            logger.info(f"Created pending ingestion {pending_id} for card {card_id}")
        except Exception as e:
            logger.error(f"Failed to create pending ingestion: {e}")
            return
        
        # Notify frontend
        try:
            await notification_hub.notify_new_mail(
                card_id=card_id,
                summary_data={
                    "pending_id": pending_id,
                    "has_pdf": has_pdf,
                    "text_chunks": len(chunks),
                    "sender": email_data.sender,
                    "timestamp": datetime.utcnow().isoformat()
                }
            )
            
            # If we created a new card, trigger sync for all clients
            if created_new_card:
                await notification_hub.trigger_sync()
                logger.info(f"Triggered sync for new card {card_id}")
                
        except Exception as e:
            logger.error(f"Failed to send notifications: {e}")
    
    def stop(self):
        """Signal the service to stop on next loop iteration."""
        self.is_running = False
        logger.info("Email listener stop requested")


# Global singleton instance
email_listener = EmailListenerService()
