"""
Agent Tools for MedDeck.

This module defines the tools available to the AI Agent for querying
patient data from the database. Each tool is designed to be:
1. Called by the Agent with specific parameters (no card_id needed)
2. Automatically retrieves card_id from context via @require_card_id decorator
3. Emitting "Info" messages to the chat for user feedback

The tools follow a consistent pattern:
- Decorated with @require_card_id for automatic context validation
- Retrieve card_id via get_card_id() at the start
- Log an "Info" message to chat before executing
- Return results as JSON-serializable strings
"""

import json
import logging
import ast
from datetime import datetime
from typing import List

from dateutil import parser as date_parser

import database as db
from models import MessageRole
from app.services.email_sender import send_email_broadcast
from app.context import get_card_id, require_card_id

logger = logging.getLogger("MedDeckTools")


# =============================================================================
# VALIDATION HELPERS
# =============================================================================

def validate_date_input(date_str: str | None, is_end_date: bool = False):
    """
    Parse date string safely with soft-fail behavior.
    
    Args:
        date_str: The date string from the LLM (e.g., "2024-01-01")
        is_end_date: If True, sets time to 23:59:59.999999 for midnight inputs.
        
    Returns:
        (datetime_obj, None) if valid
        (None, warning_msg) if invalid
        (None, None) if empty
    """
    if not date_str:
        return None, None
    try:
        dt = date_parser.parse(date_str)
        # Fix "Midnight Problem": If end_date is 00:00:00, move to end of day
        if is_end_date and dt.hour == 0 and dt.minute == 0 and dt.second == 0:
            dt = dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        return dt, None
    except (ValueError, TypeError, OverflowError):
        warning = f"Warning: Date '{date_str}' could not be parsed. Expected ISO format (YYYY-MM-DD)."
        return None, warning


def validate_list_input(input_val: list | str):
    """
    Ensure input is a list of strings. Handles stringified lists from LLM.
    
    Returns:
        (cleaned_list, warning_msg)
    """
    if isinstance(input_val, list):
        return [str(i) for i in input_val if i is not None], None
    
    if isinstance(input_val, str):
        clean = input_val.strip()
        # Method A: Try safe AST literal eval (handles ['A', 'B'])
        if clean.startswith('[') and clean.endswith(']'):
            try:
                parsed = ast.literal_eval(clean)
                if isinstance(parsed, list):
                    return [str(x) for x in parsed], "Note: Parsed stringified list."
            except (ValueError, SyntaxError):
                pass
        
        # Method B: Fallback comma-separation
        if ',' in clean:
            items = [x.strip(" '\"") for x in clean.split(',')]
            return items, "Note: Parsed CSV string."
        
        # Method C: Single item
        return [clean.strip(" '\"")], "Note: Treated single string as list."
    
    return [], "Error: Could not parse input as list."


# =============================================================================
# GROUP A: QUANTITATIVE (BLOOD WORK)
# =============================================================================

@require_card_id
async def tool_get_quantitative_overview() -> str:
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
    card_id = get_card_id()
    
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


@require_card_id
async def tool_get_specific_lab_values(
    test_names: List[str],
    start_date: str = None,
    end_date: str = None
) -> str:
    """
    Get the specific historical results for a list of blood tests.
    
    Use this after tool_get_quantitative_overview to get detailed results
    for specific tests. Returns historical values with reference ranges
    where available.
    
    Args:
        test_names: A list of test names to retrieve (e.g., ['Hemoglobin', 'Glucose']).
                   Use the exact test names from the overview.
        start_date: Optional start date filter (ISO format: YYYY-MM-DD).
        end_date: Optional end date filter (ISO format: YYYY-MM-DD).
    
    Returns:
        A JSON string containing the test results with reference ranges.
    """
    card_id = get_card_id()
    
    # 1. Validate inputs
    clean_names, name_warn = validate_list_input(test_names)
    start_dt, start_warn = validate_date_input(start_date)
    end_dt, end_warn = validate_date_input(end_date, is_end_date=True)
    
    warnings = [w for w in [name_warn, start_warn, end_warn] if w]
    
    if not clean_names:
        return json.dumps({"error": "No valid test names provided.", "syntax_warnings": warnings})
    
    try:
        # Emit info message to chat
        test_list = ", ".join(clean_names[:3])
        if len(clean_names) > 3:
            test_list += f" and {len(clean_names) - 3} more"
            
        date_info = ""
        if start_dt or end_dt:
            date_info = f" ({start_date or 'beginning'} to {end_date or 'now'})"

        await db.append_chat_message(
            card_id,
            MessageRole.INFO,
            f"📈 Fetching results for: {test_list}{date_info}..."
        )
        
        # Fetch from database
        results = await db.get_quantitative_labs(
            card_id,
            clean_names,
            start_time=start_dt,
            end_time=end_dt
        )
        
        response = {"results": results}
        if warnings:
            response["syntax_warnings"] = warnings
            
        return json.dumps(response, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_specific_lab_values: {e}")
        err = {"error": str(e)}
        if warnings:
            err["syntax_warnings"] = warnings
        return json.dumps(err)


@require_card_id
async def tool_get_abnormal_labs(
    start_date: str = None,
    end_date: str = None
) -> str:
    """
    Get all abnormal lab results for this patient.
    
    Returns lab results that are outside normal reference ranges OR
    results where no reference range is defined (for safety).
    
    This tool uses a safety-first approach:
    - Results with values above/below reference ranges are marked ABNORMAL_HIGH/LOW
    - Results without reference ranges are included and marked UNKNOWN_REF
    - Non-numeric results (e.g., "Positive") are included and marked NON_NUMERIC
    
    Args:
        start_date: Optional start date filter (ISO format: YYYY-MM-DD).
        end_date: Optional end date filter (ISO format: YYYY-MM-DD).
    
    Returns:
        A JSON string containing abnormal results with status indicators.
    """
    card_id = get_card_id()
    
    start_dt, start_warn = validate_date_input(start_date)
    end_dt, end_warn = validate_date_input(end_date, is_end_date=True)
    warnings = [w for w in [start_warn, end_warn] if w]
    
    try:
        await db.append_chat_message(card_id, MessageRole.INFO, "🚨 Scanning for abnormal labs...")
        
        # Query DB (with implicit limit)
        data = await db.get_abnormal_labs(
            card_id,
            start_time=start_dt,
            end_time=end_dt
        )
        
        response = {
            "note": "Includes results with missing reference ranges for safety.",
            "count": len(data["results"]),
            "results": data["results"]
        }
        
        if data.get("truncated"):
            response["truncated"] = True
            response["truncation_note"] = f"Showing {len(data['results'])} of {data.get('total_available')} abnormal results. Consider narrowing date range."
        
        if warnings:
            response["syntax_warnings"] = warnings
            
        return json.dumps(response, indent=2, default=str)
        
    except Exception as e:
        logger.error(f"Error in tool_get_abnormal_labs: {e}")
        err = {"error": str(e)}
        if warnings:
            err["syntax_warnings"] = warnings
        return json.dumps(err)


# =============================================================================
# GROUP B: MICROBIOLOGY
# =============================================================================

@require_card_id
async def tool_get_microbiology_overview() -> str:
    """
    Get a list of all available microbiology culture reports for this patient.
    
    Returns a summary of all microbiology reports (cultures and sensitivities)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    tool_get_microbiology_details to get the full report.
    
    Returns:
        A JSON string containing a list of microbiology report summaries with indices.
    """
    card_id = get_card_id()
    
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


@require_card_id
async def tool_get_microbiology_details(
    indices: List[int]
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
    card_id = get_card_id()
    
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

@require_card_id
async def tool_get_imaging_overview() -> str:
    """
    Get a list of all available imaging reports for this patient.
    
    Returns a summary of all imaging studies (CT, MRI, Ultrasound, X-ray, etc.)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    tool_get_imaging_details to get the full report.
    
    Returns:
        A JSON string containing a list of imaging report summaries with indices.
    """
    card_id = get_card_id()
    
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


@require_card_id
async def tool_get_imaging_details(
    indices: List[int]
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
    card_id = get_card_id()
    
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

@require_card_id
async def tool_get_pathology_overview() -> str:
    """
    Get a list of all available pathology (histopathology) reports for this patient.
    
    Returns a summary of all pathology reports (biopsies, surgical specimens, etc.)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    tool_get_pathology_details to get the full report.
    
    Returns:
        A JSON string containing a list of pathology report summaries with indices.
    """
    card_id = get_card_id()
    
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


@require_card_id
async def tool_get_pathology_details(
    indices: List[int]
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
    card_id = get_card_id()
    
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

@require_card_id
async def tool_get_history_overview() -> str:
    """
    Get a chronological catalog of available patient history documents.
    
    Returns a summary of all clinical history documents (processed by the Scribe)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    tool_get_history_details to get the full document content.
    
    Returns:
        A JSON string containing a list of history document summaries with indices.
    """
    card_id = get_card_id()
    
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


@require_card_id
async def tool_get_history_details(
    indices: List[int]
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
    card_id = get_card_id()
    
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

@require_card_id
async def tool_send_email_update(
    subject: str,
    content: str
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
    card_id = get_card_id()
    
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
# NOTE: card_id is NO LONGER in any tool signature. It is retrieved from
# context via the @require_card_id decorator and get_card_id() function.
my_tool_list = [
    # Group A: Quantitative (Blood Work)
    tool_get_quantitative_overview,
    tool_get_specific_lab_values,
    tool_get_abnormal_labs,  # NEW
    
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
