"""
Agent Tools for MedDeck.

This module defines the tools available to the AI Agent for querying
patient data from the database. Each tool is designed to be:
1. Called by the Agent with specific parameters
2. Injected with card_id by the tool router (not exposed to LLM)
3. Emitting "Info" messages to the chat for user feedback

The tools follow a consistent pattern:
- Accept card_id as an optional keyword argument (injected by router)
- Log an "Info" message to chat before executing
- Return results as JSON-serializable strings
"""

import json
import logging
from typing import List, Optional

import database as db
from models import MessageRole
from app.services.email_sender import send_email_broadcast

logger = logging.getLogger("MedDeckTools")


# =============================================================================
# GROUP A: QUANTITATIVE (BLOOD WORK)
# =============================================================================

async def tool_get_quantitative_overview(card_id: str = None) -> str:
    """
    Get a list of all available blood tests (Quantitative) for this patient.
    
    Returns a catalog of all quantitative lab tests available for this patient,
    including the test names, materials (e.g., Blood, Urine), and the date range
    of available results.
    
    Use this tool first to discover what blood tests are available before
    requesting specific values.
    
    Returns:
        A JSON string containing a list of available tests with their metadata.
    """
    if not card_id:
        return "Error: No patient card specified."
    
    try:
        # Emit info message to chat
        await db.append_chat_message(
            card_id, 
            MessageRole.INFO, 
            "📊 Fetching blood test catalog..."
        )
        
        # Fetch from database
        results = await db.get_quantitative_overview(card_id)
        
        # Return as formatted JSON string
        return json.dumps(results, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_quantitative_overview: {e}")
        return f"Error retrieving blood test catalog: {str(e)}"


async def tool_get_specific_lab_values(
    test_names: List[str],
    card_id: str = None
) -> str:
    """
    Get the specific historical results for a list of blood tests.
    
    Use this after tool_get_quantitative_overview to get detailed results
    for specific tests. Returns historical values with reference ranges
    where available.
    
    Args:
        test_names: A list of test names to retrieve (e.g., ['Hemoglobin', 'Glucose']).
                   Use the exact test names from the overview.
    
    Returns:
        A JSON string containing the test results with reference ranges.
    """
    if not card_id:
        return "Error: No patient card specified."
    
    if not test_names:
        return "Error: No test names provided."
    
    try:
        # Emit info message to chat
        test_list = ", ".join(test_names[:3])
        if len(test_names) > 3:
            test_list += f" and {len(test_names) - 3} more"
        await db.append_chat_message(
            card_id,
            MessageRole.INFO,
            f"📈 Fetching results for: {test_list}..."
        )
        
        # Fetch from database
        results = await db.get_quantitative_labs(card_id, test_names)
        
        return json.dumps(results, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_specific_lab_values: {e}")
        return f"Error retrieving lab values: {str(e)}"


# =============================================================================
# GROUP B: MICROBIOLOGY
# =============================================================================

async def tool_get_microbiology_overview(card_id: str = None) -> str:
    """
    Get a list of all available microbiology culture reports for this patient.
    
    Returns a summary of all microbiology reports (cultures and sensitivities)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    tool_get_microbiology_details to get the full report.
    
    Returns:
        A JSON string containing a list of microbiology report summaries with indices.
    """
    if not card_id:
        return "Error: No patient card specified."
    
    try:
        # Emit info message to chat
        await db.append_chat_message(
            card_id,
            MessageRole.INFO,
            "🧫 Fetching microbiology reports..."
        )
        
        # Fetch from database
        results = await db.get_microbiology_overview(card_id)
        
        # Add index numbers for easy reference
        for i, item in enumerate(results):
            item["index"] = i
        
        return json.dumps(results, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_microbiology_overview: {e}")
        return f"Error retrieving microbiology overview: {str(e)}"


async def tool_get_microbiology_details(
    indices: List[int],
    card_id: str = None
) -> str:
    """
    Get the full details of specific microbiology reports by their index numbers.
    
    Use this after tool_get_microbiology_overview to get complete culture
    and sensitivity information for specific reports.
    
    Args:
        indices: A list of 0-based index numbers from the overview list
                (e.g., [0] for the most recent, [1, 2] for the second and third).
    
    Returns:
        A JSON string containing the full microbiology reports including
        gram stain, culture results, and antibiotic sensitivities.
    """
    if not card_id:
        return "Error: No patient card specified."
    
    if not indices:
        return "Error: No indices provided."
    
    try:
        # Emit info message to chat
        index_str = ", ".join(f"#{i}" for i in indices[:3])
        if len(indices) > 3:
            index_str += f" and {len(indices) - 3} more"
        await db.append_chat_message(
            card_id,
            MessageRole.INFO,
            f"🧫 Fetching microbiology details for report {index_str}..."
        )
        
        # Fetch from database
        results = await db.get_microbiology_reports_by_indices(card_id, indices)
        
        return json.dumps(results, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_microbiology_details: {e}")
        return f"Error retrieving microbiology details: {str(e)}"


# =============================================================================
# GROUP C: IMAGING
# =============================================================================

async def tool_get_imaging_overview(card_id: str = None) -> str:
    """
    Get a list of all available imaging reports for this patient.
    
    Returns a summary of all imaging studies (CT, MRI, Ultrasound, X-ray, etc.)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    tool_get_imaging_details to get the full report.
    
    Returns:
        A JSON string containing a list of imaging report summaries with indices.
    """
    if not card_id:
        return "Error: No patient card specified."
    
    try:
        # Emit info message to chat
        await db.append_chat_message(
            card_id,
            MessageRole.INFO,
            "🖼️ Fetching imaging reports..."
        )
        
        # Fetch from database
        results = await db.get_imaging_overview(card_id)
        
        # Add index numbers for easy reference
        for i, item in enumerate(results):
            item["index"] = i
        
        return json.dumps(results, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_imaging_overview: {e}")
        return f"Error retrieving imaging overview: {str(e)}"


async def tool_get_imaging_details(
    indices: List[int],
    card_id: str = None
) -> str:
    """
    Get the full details of specific imaging reports by their index numbers.
    
    Use this after tool_get_imaging_overview to get complete radiology
    reports including findings and conclusions.
    
    Args:
        indices: A list of 0-based index numbers from the overview list
                (e.g., [0] for the most recent, [1, 2] for the second and third).
    
    Returns:
        A JSON string containing the full imaging reports including
        exam type, indication, findings, and summary/conclusion.
    """
    if not card_id:
        return "Error: No patient card specified."
    
    if not indices:
        return "Error: No indices provided."
    
    try:
        # Emit info message to chat
        index_str = ", ".join(f"#{i}" for i in indices[:3])
        if len(indices) > 3:
            index_str += f" and {len(indices) - 3} more"
        await db.append_chat_message(
            card_id,
            MessageRole.INFO,
            f"🖼️ Fetching imaging details for report {index_str}..."
        )
        
        # Fetch from database
        results = await db.get_imaging_reports_by_indices(card_id, indices)
        
        return json.dumps(results, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_imaging_details: {e}")
        return f"Error retrieving imaging details: {str(e)}"


# =============================================================================
# GROUP D: PATHOLOGY
# =============================================================================

async def tool_get_pathology_overview(card_id: str = None) -> str:
    """
    Get a list of all available pathology (histopathology) reports for this patient.
    
    Returns a summary of all pathology reports (biopsies, surgical specimens, etc.)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    tool_get_pathology_details to get the full report.
    
    Returns:
        A JSON string containing a list of pathology report summaries with indices.
    """
    if not card_id:
        return "Error: No patient card specified."
    
    try:
        # Emit info message to chat
        await db.append_chat_message(
            card_id,
            MessageRole.INFO,
            "🔬 Fetching pathology reports..."
        )
        
        # Fetch from database
        results = await db.get_pathology_overview(card_id)
        
        # Add index numbers for easy reference
        for i, item in enumerate(results):
            item["index"] = i
        
        return json.dumps(results, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_pathology_overview: {e}")
        return f"Error retrieving pathology overview: {str(e)}"


async def tool_get_pathology_details(
    indices: List[int],
    card_id: str = None
) -> str:
    """
    Get the full details of specific pathology reports by their index numbers.
    
    Use this after tool_get_pathology_overview to get complete histopathology
    reports including macroscopic, microscopic, and diagnosis sections.
    
    Args:
        indices: A list of 0-based index numbers from the overview list
                (e.g., [0] for the most recent, [1, 2] for the second and third).
    
    Returns:
        A JSON string containing the full pathology reports including
        specimen, clinical data, macroscopic findings, microscopic findings,
        and diagnosis.
    """
    if not card_id:
        return "Error: No patient card specified."
    
    if not indices:
        return "Error: No indices provided."
    
    try:
        # Emit info message to chat
        index_str = ", ".join(f"#{i}" for i in indices[:3])
        if len(indices) > 3:
            index_str += f" and {len(indices) - 3} more"
        await db.append_chat_message(
            card_id,
            MessageRole.INFO,
            f"🔬 Fetching pathology details for report {index_str}..."
        )
        
        # Fetch from database
        results = await db.get_pathology_reports_by_indices(card_id, indices)
        
        return json.dumps(results, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_pathology_details: {e}")
        return f"Error retrieving pathology details: {str(e)}"


# =============================================================================
# GROUP E: CLINICAL HISTORY (SCRIBE OUTPUT)
# =============================================================================

async def tool_get_history_overview(card_id: str = None) -> str:
    """
    Get a chronological catalog of available patient history documents.
    
    Returns a summary of all clinical history documents (processed by the Scribe)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    tool_get_history_details to get the full document content.
    
    Returns:
        A JSON string containing a list of history document summaries with indices.
    """
    if not card_id:
        return "Error: No patient card specified."
    
    try:
        # Emit info message to chat
        await db.append_chat_message(
            card_id,
            MessageRole.INFO,
            "📜 Fetching clinical history overview..."
        )
        
        # Fetch from database
        results = await db.get_history_overview(card_id)
        
        # Add index numbers for easy reference
        for i, item in enumerate(results):
            item["index"] = i
        
        return json.dumps(results, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_history_overview: {e}")
        return f"Error retrieving history overview: {str(e)}"


async def tool_get_history_details(
    indices: List[int],
    card_id: str = None
) -> str:
    """
    Get the full content of specific history documents by their index numbers.
    
    Use this after tool_get_history_overview to get complete clinical narratives
    for specific events (admissions, consults, discharge summaries, etc.).
    
    Args:
        indices: A list of 0-based index numbers from the overview list
                (e.g., [0] for the most recent, [1, 2] for the second and third).
    
    Returns:
        A JSON string containing the full history documents including
        title, timestamp, and markdown-formatted clinical narrative.
    """
    if not card_id:
        return "Error: No patient card specified."
    
    if not indices:
        return "Error: No indices provided."
    
    try:
        # Emit info message to chat
        index_str = ", ".join(f"#{i}" for i in indices[:3])
        if len(indices) > 3:
            index_str += f" and {len(indices) - 3} more"
        await db.append_chat_message(
            card_id,
            MessageRole.INFO,
            f"📜 Reading history documents {index_str}..."
        )
        
        # Fetch from database
        results = await db.get_history_documents_by_indices(card_id, indices)
        
        return json.dumps(results, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_history_details: {e}")
        return f"Error retrieving history details: {str(e)}"


# =============================================================================
# GROUP F: EMAIL NOTIFICATIONS
# =============================================================================

async def tool_send_email_update(
    subject: str,
    content: str,
    card_id: str = None
) -> str:
    """
    Send an email update about the current patient to the clinical care team.
    
    Use this tool when you need to communicate important patient information
    to the clinical team, such as:
    - Summary of a consultation
    - Lab results 
    - Status updates or handoff notes
    - Any information that should be documented in the patient's record
    
    The email will be automatically addressed to the configured clinical team
    and will include the patient's serial number in the subject line.
    
    Args:
        subject: A concise subject line describing the email content
                (e.g., "Summary of Cardiology Consult", "Critical Lab Values").
                The system will automatically prepend "Patient {serial} - " to this.
        content: The full email body in plain text. Include all relevant
                clinical information, findings, and recommendations.
    
    Returns:
        A string indicating success or failure of the email send operation.
    """
    if not card_id:
        return "Error: No patient context provided. Cannot send email."
    
    try:
        # Emit info message to chat
        await db.append_chat_message(
            card_id,
            MessageRole.INFO,
            "📧 Sending email update to clinical team..."
        )
        
        # Fetch the patient's card to get the serial number
        from bson.objectid import ObjectId
        card = await db.cards_collection.find_one({"_id": ObjectId(card_id)})
        
        if not card:
            return "Error: Patient card not found."
        
        serial = card.get("serial", "Unknown")
        
        # Format the subject with patient serial number prefix
        formatted_subject = f"Patient {serial} - {subject}"
        
        # Send the email broadcast
        success = send_email_broadcast(formatted_subject, content)
        
        if success:
            logger.info(f"Email update sent for patient {serial}: {subject}")
            return f"Email successfully sent to clinical team with subject: \"{formatted_subject}\""
        else:
            logger.warning(f"Failed to send email update for patient {serial}")
            return "Error: Failed to send email. Please check server logs for details."
            
    except Exception as e:
        logger.error(f"Error in tool_send_email_update: {e}")
        return f"Error sending email: {str(e)}"


# =============================================================================
# TOOL EXPORT LIST
# =============================================================================

# This list is passed to the Gemini model so it knows what tools are available.
# The Google Gen AI SDK will automatically convert these Python functions
# into the appropriate JSON schema for the model.
#
# IMPORTANT: card_id is NOT included in the schema exposed to the LLM.
# It is injected by the tool router in agent.py.
my_tool_list = [
    # Group A: Quantitative (Blood Work)
    tool_get_quantitative_overview,
    tool_get_specific_lab_values,
    
    # Group B: Microbiology
    tool_get_microbiology_overview,
    tool_get_microbiology_details,
    
    # Group C: Imaging
    tool_get_imaging_overview,
    tool_get_imaging_details,
    
    # Group D: Pathology
    tool_get_pathology_overview,
    tool_get_pathology_details,
    
    # Group E: Clinical History (Scribe Output)
    tool_get_history_overview,
    tool_get_history_details,
    
    # Group F: Email Notifications
    tool_send_email_update,
]
