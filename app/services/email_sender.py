"""
Email sender service for outbound SMTP email dispatch.

This module handles sending email broadcasts to the clinical care team
using SMTP with Gmail credentials.
"""

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import Config

logger = logging.getLogger(__name__)


def send_email_broadcast(subject: str, body: str) -> bool:
    """
    Send an email broadcast to all configured recipients.
    
    Uses BCC to protect recipient privacy and prevent Reply-All storms.
    
    Args:
        subject: The email subject line (already formatted with patient serial).
        body: The plain text body content.
    
    Returns:
        True if the email was sent successfully, False otherwise.
    """
    # Validate recipients
    if not Config.RECIPIENTS:
        logger.warning("No recipients configured. Email not sent.")
        return False
    
    # Construct MIME message
    msg = MIMEMultipart()
    msg["From"] = Config.GMAIL_USER
    msg["Subject"] = subject
    
    # Use BCC to protect recipient privacy and prevent Reply-All storms
    # Do not set the "To" header
    msg["Bcc"] = ", ".join(Config.RECIPIENTS)
    
    # Attach body as plain text
    msg.attach(MIMEText(body, "plain"))
    
    try:
        # Connect to Gmail SMTP server using SSL
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(Config.GMAIL_USER, Config.GMAIL_PASS)
            server.send_message(msg)
        
        logger.info(f"Email broadcast sent successfully to {len(Config.RECIPIENTS)} recipient(s)")
        return True
    
    except Exception as e:
        logger.error(f"Failed to send email broadcast: {e}")
        return False
