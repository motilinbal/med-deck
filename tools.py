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
from typing import List, Optional

from dateutil import parser as date_parser

import database as db
from models import MessageRole
from app.services.email_sender import send_email_broadcast
from app.context import get_card_id, require_card_id
from app.utils.text_sanitizer import MedicalLetterSanitizer

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
async def get_quantitative_overview() -> str:
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
        logger.error(f"Error in get_quantitative_overview: {e}")
        return f"Error retrieving blood test catalog: {str(e)}"


@require_card_id
async def get_specific_lab_values(
    test_names: List[str],
    start_date: str = None,
    end_date: str = None
) -> str:
    """
    Get the specific historical results for a list of blood tests.
    
    Use this after get_quantitative_overview to get detailed results
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
        logger.error(f"Error in get_specific_lab_values: {e}")
        err = {"error": str(e)}
        if warnings:
            err["syntax_warnings"] = warnings
        return json.dumps(err)


@require_card_id
async def get_abnormal_labs(
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
        logger.error(f"Error in get_abnormal_labs: {e}")
        err = {"error": str(e)}
        if warnings:
            err["syntax_warnings"] = warnings
        return json.dumps(err)


# =============================================================================
# GROUP B: MICROBIOLOGY
# =============================================================================

@require_card_id
async def get_microbiology_overview() -> str:
    """
    Get a list of all available microbiology culture reports for this patient.
    
    Returns a summary of all microbiology reports (cultures and sensitivities)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    get_microbiology_details to get the full report.
    
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
        logger.error(f"Error in get_microbiology_overview: {e}")
        return f"Error retrieving microbiology overview: {str(e)}"


@require_card_id
async def get_microbiology_details(
    indices: List[int]
) -> str:
    """
    Get the full details of specific microbiology reports by their index numbers.
    
    Use this after get_microbiology_overview to get complete culture
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
        logger.error(f"Error in get_microbiology_details: {e}")
        return f"Error retrieving microbiology details: {str(e)}"


# =============================================================================
# GROUP C: IMAGING
# =============================================================================

@require_card_id
async def get_imaging_overview() -> str:
    """
    Get a list of all available imaging reports for this patient.
    
    Returns a summary of all imaging studies (CT, MRI, Ultrasound, X-ray, etc.)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    get_imaging_details to get the full report.
    
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
        logger.error(f"Error in get_imaging_overview: {e}")
        return f"Error retrieving imaging overview: {str(e)}"


@require_card_id
async def get_imaging_details(
    indices: List[int]
) -> str:
    """
    Get the full details of specific imaging reports by their index numbers.
    
    Use this after get_imaging_overview to get complete radiology
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
        logger.error(f"Error in get_imaging_details: {e}")
        return f"Error retrieving imaging details: {str(e)}"


# =============================================================================
# GROUP D: PATHOLOGY
# =============================================================================

@require_card_id
async def get_pathology_overview() -> str:
    """
    Get a list of all available pathology (histopathology) reports for this patient.
    
    Returns a summary of all pathology reports (biopsies, surgical specimens, etc.)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    get_pathology_details to get the full report.
    
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
        logger.error(f"Error in get_pathology_overview: {e}")
        return f"Error retrieving pathology overview: {str(e)}"


@require_card_id
async def get_pathology_details(
    indices: List[int]
) -> str:
    """
    Get the full details of specific pathology reports by their index numbers.
    
    Use this after get_pathology_overview to get complete histopathology
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
        logger.error(f"Error in get_pathology_details: {e}")
        return f"Error retrieving pathology details: {str(e)}"


# =============================================================================
# GROUP E: CLINICAL HISTORY (SCRIBE OUTPUT)
# =============================================================================

@require_card_id
async def get_history_overview() -> str:
    """
    Get a chronological catalog of available patient history documents.
    
    Returns a summary of all clinical history documents (processed by the Scribe)
    available for this patient, ordered by date (most recent first).
    
    Each entry includes an index number that can be used with
    get_history_details to get the full document content.
    
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
        logger.error(f"Error in get_history_overview: {e}")
        return f"Error retrieving history overview: {str(e)}"


@require_card_id
async def get_history_details(
    indices: List[int]
) -> str:
    """
    Get the full content of specific history documents by their index numbers.
    
    Use this after get_history_overview to get complete clinical narratives
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
        logger.error(f"Error in get_history_details: {e}")
        return f"Error retrieving history details: {str(e)}"


# =============================================================================
# GROUP F: EMAIL NOTIFICATIONS
# =============================================================================

@require_card_id
async def send_email_update(
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

        # Translate content to Hebrew and sanitize (for testing: always translate)
        try:
            from agent import translate_to_hebrew
            # Translate to Hebrew
            hebrew_content = await translate_to_hebrew(content)
            # Sanitize
            sanitizer = MedicalLetterSanitizer()
            final_content = sanitizer.process(hebrew_content)
        except Exception as e:
            logger.warning(f"Translation failed ({e}), sending in original language")
            sanitizer = MedicalLetterSanitizer()
            final_content = sanitizer.process(content)

        # Send the email broadcast
        success = send_email_broadcast(formatted_subject, final_content)
        
        if success:
            logger.info(f"Email update sent for patient {serial}: {subject}")
            return f"Email successfully sent to clinical team with subject: \"{formatted_subject}\""
        else:
            logger.warning(f"Failed to send email update for patient {serial}")
            return "Error: Failed to send email. Please check server logs for details."
            
    except Exception as e:
        logger.error(f"Error in send_email_update: {e}")
        return f"Error sending email: {str(e)}"


# =============================================================================
# GROUP H: ACID-BASE CALCULATOR
# =============================================================================

async def calculate_acid_base(
    ph: Optional[float] = None,
    pco2: Optional[float] = None,
    hco3: Optional[float] = None,
    na: Optional[float] = None,
    cl: Optional[float] = None,
    albumin: Optional[float] = None,
    bun: Optional[float] = None,
    glucose: Optional[float] = None,
    ethanol: Optional[float] = None
) -> str:
    """
    Calculate acid-base status from arterial blood gas and electrolyte values.

    This tool performs a comprehensive acid-base disturbance analysis using
    standard clinical formulas. It can identify primary disorders, assess
    compensation, calculate anion gap (with albumin correction), and detect
    mixed disorders.

    Prerequisite:
        Use get_quantitative_overview first to discover available lab tests,
        then get_specific_lab_values to retrieve pH, pCO₂, HCO₃⁻, and
        optional electrolytes (Na, Cl, Albumin, BUN, Glucose, Ethanol).

    Mandatory Args:
        ph: Arterial pH (e.g., 7.35)
        pco2: Partial pressure of CO₂ in mmHg (e.g., 40)
        hco3: Bicarbonate in mEq/L (e.g., 24)

    Optional Args (improve analysis when provided):
        na: Sodium in mEq/L (e.g., 140) - enables anion gap calculation
        cl: Chloride in mEq/L (e.g., 100) - enables anion gap calculation
        albumin: Albumin in g/dL (e.g., 4.0) - enables albumin-corrected AG
        bun: Blood urea nitrogen in mg/dL (e.g., 15) - enables osmolar gap
        glucose: Glucose in mg/dL (e.g., 100) - enables osmolar gap
        ethanol: Ethanol in mg/dL (e.g., 0) - enables osmolar gap

    Returns:
        A formatted plain text string containing the complete acid-base
        analysis including primary disorder, compensation assessment,
        anion gap analysis, delta gap analysis, and osmolar gap (if applicable).

    Note:
        Do NOT guess or estimate values for optional parameters. Only pass
        values that have been explicitly retrieved from the patient's lab data.
        Guessing values (e.g., assuming normal albumin of 4.0) can mask
        underlying disorders like HAGMA.
    """
    # --- 1. MANDATORY FIELD VALIDATION ---
    if ph is None:
        return "Error: Missing mandatory pH value. Please fetch the blood gas results first using get_quantitative_overview, then get_specific_lab_values."
    if pco2 is None:
        return "Error: Missing mandatory pCO₂ value. Please fetch the blood gas results first using get_quantitative_overview, then get_specific_lab_values."
    if hco3 is None:
        return "Error: Missing mandatory HCO₃⁻ value. Please fetch the blood gas results first using get_quantitative_overview, then get_specific_lab_values."

    # --- 2. ZERO DIVISION GUARD ---
    if hco3 == 0:
        return "Error: HCO₃⁻ cannot be exactly zero. Please verify the lab values."

    # --- 3. HANDLE OPTIONAL FIELD DEFAULTS ---
    if ethanol is None:
        ethanol = 0.0

    # --- 4. PHYSIOLOGICAL VALIDITY CHECK (Henderson-Hasselbalch) ---
    expected_h = 24 * (pco2 / hco3)
    actual_h = 10 ** (9 - ph)

    if abs(expected_h - actual_h) > (actual_h * 0.1):
        return (f"Error: The entered pH ({ph}), pCO₂ ({pco2}), and HCO₃⁻ ({hco3}) values do not appear "
                "physiologically compatible. These values do not satisfy the Henderson-Hasselbalch equation. "
                "Please verify the lab values were drawn from the same sample and time point. "
                "Common causes: mixing arterial pH with venous bicarbonate, or transcription errors.")

    # --- 5. DEFINE BASELINES ---
    ph_baseline = 7.4
    pco2_baseline = 40
    hco3_baseline = 24
    normal_ag_upper = 12

    # --- 6. PRELIMINARY ANALYSIS ---
    preliminary_disorder_type = 'Normal'
    preliminary_disorder_origin = 'N/A'

    if ph < ph_baseline:
        preliminary_disorder_type = 'Acidosis'
        preliminary_disorder_origin = 'Respiratory' if pco2 > pco2_baseline else 'Metabolic'
    elif ph > ph_baseline:
        preliminary_disorder_type = 'Alkalosis'
        preliminary_disorder_origin = 'Respiratory' if pco2 < pco2_baseline else 'Metabolic'
    elif ph == ph_baseline and (pco2 != pco2_baseline or hco3 != hco3_baseline):
        preliminary_disorder_type = 'Mixed Disorder (Balanced)'
        preliminary_disorder_origin = 'Mixed'

    # --- 7. ANION GAP & DELTA GAP CALCULATIONS ---
    anion_gap_display = 'N/A'
    corrected_anion_gap_display = 'N/A'
    anion_gap_analysis = 'N/A'
    goldmark_mnemonic = ''
    final_ag = float('nan')
    is_hagma_present = False

    hco3_before_display = 'N/A'
    delta_gap_analysis = ''

    if na is not None and cl is not None:
        anion_gap = na - (hco3 + cl)
        anion_gap_display = f"{anion_gap:.1f} mEq/L"
        final_ag = anion_gap

        if albumin is not None:
            corrected_ag = anion_gap + 2.5 * (4 - albumin)
            corrected_anion_gap_display = f"{corrected_ag:.1f} mEq/L (Corrected for Albumin)"
            final_ag = corrected_ag

        if final_ag > normal_ag_upper:
            is_hagma_present = True
            anion_gap_analysis = 'High Anion Gap Metabolic Acidosis (HAGMA)'
            goldmark_mnemonic = """G: Glycols
O: Oxoproline
L: L-Lactate
D: D-Lactate
M: Methanol
A: Aspirin
R: Renal Failure
K: Ketoacidosis"""

            # Delta Gap Calculation
            hco3_before = hco3 + (final_ag - 12)
            hco3_before_display = f"~{hco3_before:.1f} mEq/L"
            if hco3_before < 22:
                delta_gap_analysis = 'concomitant non-anion gap metabolic acidosis'
            elif hco3_before > 26:
                delta_gap_analysis = 'pre-existing metabolic alkalosis'
            else:
                delta_gap_analysis = 'No pre-existing acid-base disorder'
        else:
            anion_gap_analysis = 'Anion gap is normal'
    else:
        anion_gap_analysis = 'Na and/or Cl not provided'

    # --- 8. FINAL DIAGNOSIS SYNTHESIS ---
    if is_hagma_present and preliminary_disorder_origin == 'Metabolic' and preliminary_disorder_type == 'Acidosis':
        primary_diagnosis = 'High Anion Gap Metabolic Acidosis'
    elif preliminary_disorder_origin == 'Mixed':
        primary_diagnosis = 'Mixed Acid-Base Disorder (pH 7.40 - Balanced)'
    elif preliminary_disorder_type != 'Normal':
        primary_diagnosis = f"{preliminary_disorder_origin} {preliminary_disorder_type}"
    else:
        primary_diagnosis = 'Normal Acid-Base Status'

    # --- 9. COMPENSATION ANALYSIS ---
    expected_pco2_display = 'N/A'
    expected_hco3_display = 'N/A'
    compensation_analysis = 'N/A'

    if preliminary_disorder_type != 'Normal':
        if preliminary_disorder_origin == 'Metabolic':
            if preliminary_disorder_type == 'Acidosis':
                expected_pco2_lower = (1.5 * hco3) + 8 - 2
                expected_pco2_upper = (1.5 * hco3) + 8 + 2
                expected_pco2_display = f"Winter's: {expected_pco2_lower:.1f} - {expected_pco2_upper:.1f} mmHg"
                if pco2 > expected_pco2_upper:
                    compensation_analysis = 'Concomitant Respiratory Acidosis'
                elif pco2 < expected_pco2_lower:
                    compensation_analysis = 'Concomitant Respiratory Alkalosis'
                else:
                    compensation_analysis = 'Appropriate respiratory compensation'
            elif preliminary_disorder_type == 'Alkalosis':
                delta_hco3 = hco3 - hco3_baseline
                predicted_pco2 = pco2_baseline + (2/3 * delta_hco3)
                expected_pco2_display = f"~{predicted_pco2:.1f} mmHg"
                if pco2 > predicted_pco2 + 3:
                    compensation_analysis = 'Concomitant Respiratory Acidosis'
                elif pco2 < predicted_pco2 - 3:
                    compensation_analysis = 'Concomitant Respiratory Alkalosis'
                else:
                    compensation_analysis = 'Appropriate respiratory compensation'
        elif preliminary_disorder_origin == 'Respiratory':
            delta_pco2 = pco2 - pco2_baseline
            if preliminary_disorder_type == 'Acidosis':
                expected_hco3_acute = hco3_baseline + (delta_pco2 / 10) * 1
                expected_hco3_chronic = hco3_baseline + (delta_pco2 / 10) * 3
                expected_hco3_display = f"Acute: ~{expected_hco3_acute:.1f}, Chronic: ~{expected_hco3_chronic:.1f} mEq/L"
                if hco3 > expected_hco3_chronic:
                    compensation_analysis = 'Concomitant Metabolic Alkalosis'
                elif hco3 < expected_hco3_acute:
                    compensation_analysis = 'Concomitant Metabolic Acidosis'
                else:
                    compensation_analysis = 'Mixed acute-on-chronic process or transitional state'
            elif preliminary_disorder_type == 'Alkalosis':
                expected_hco3_acute = hco3_baseline + (delta_pco2 / 10) * 2
                expected_hco3_chronic = hco3_baseline + (delta_pco2 / 10) * 4
                expected_hco3_display = f"Acute: ~{expected_hco3_acute:.1f}, Chronic: ~{expected_hco3_chronic:.1f} mEq/L"
                if hco3 > expected_hco3_acute:
                    compensation_analysis = 'Concomitant Metabolic Alkalosis'
                elif hco3 < expected_hco3_chronic:
                    compensation_analysis = 'Concomitant Metabolic Acidosis'
                else:
                    compensation_analysis = 'Mixed acute-on-chronic process or transitional state'

    # --- 10. OSMOLAR GAP ANALYSIS (for HAGMA) ---
    calculated_osmolality_display = 'N/A'
    osmolar_gap_note = ''

    if is_hagma_present:
        if na is not None and bun is not None and glucose is not None:
            calculated_osmolality = (2 * na) + (bun / 2.8) + (glucose / 18) + (ethanol / 4.6)
            calculated_osmolality_display = f"{calculated_osmolality:.1f} mOsm/kg"
            osmolar_gap_note = "Compare to measured osmolality. A gap > 10 may suggest unmeasured osmoles (methanol, ethylene glycol, isopropanol)"
        else:
            calculated_osmolality_display = 'Na, BUN, or Glucose not provided'

    # --- 11. BUILD OUTPUT ---
    output_lines = []

    # Primary Disorder
    output_lines.append("=== PRIMARY DISORDER ===")
    output_lines.append(primary_diagnosis)
    output_lines.append("")

    # Compensation Analysis
    output_lines.append("=== COMPENSATION ANALYSIS ===")
    if expected_pco2_display != 'N/A':
        output_lines.append(f"Expected pCO₂: {expected_pco2_display}")
    if expected_hco3_display != 'N/A':
        output_lines.append(f"Expected HCO₃⁻: {expected_hco3_display}")
    if compensation_analysis != 'N/A':
        output_lines.append(f"Interpretation: {compensation_analysis}")
    output_lines.append("")

    # Anion Gap Analysis
    output_lines.append("=== ANION GAP ANALYSIS ===")
    if anion_gap_display != 'N/A':
        output_lines.append(f"Anion Gap: {anion_gap_display}")
    if corrected_anion_gap_display != 'N/A':
        output_lines.append(f"Corrected AG: {corrected_anion_gap_display}")
    if anion_gap_analysis != 'N/A' and 'not provided' not in anion_gap_analysis:
        output_lines.append(f"Interpretation: {anion_gap_analysis}")
    if goldmark_mnemonic:
        output_lines.append("GOLDMARK:")
        output_lines.append(goldmark_mnemonic)
    output_lines.append("")

    # Delta Gap Analysis
    output_lines.append("=== DELTA GAP ANALYSIS ===")
    if hco3_before_display != 'N/A':
        output_lines.append(f"Bicarbonate Before: {hco3_before_display}")
    if delta_gap_analysis:
        output_lines.append(f"Interpretation: {delta_gap_analysis}")
    output_lines.append("")

    # Osmolar Gap Analysis
    output_lines.append("=== OSMOLAR GAP ANALYSIS ===")
    if calculated_osmolality_display != 'N/A' and 'not provided' not in calculated_osmolality_display:
        output_lines.append(f"Calculated Osmolality: {calculated_osmolality_display}")
    if osmolar_gap_note:
        output_lines.append(f"Note: {osmolar_gap_note}")
    output_lines.append("")

    # Clinical Notes
    output_lines.append("=== CLINICAL NOTES ===")
    if na is None or cl is None:
        output_lines.append("- Anion gap calculation requires Na and Cl values. Provide these for HAGMA assessment.")
    if albumin is None and na is not None and cl is not None:
        output_lines.append("- Albumin-corrected anion gap not calculated. Provide albumin to unmask potential hidden HAGMA.")
    if (bun is None or glucose is None) and is_hagma_present:
        output_lines.append("- Osmolar gap not calculated. Provide BUN and Glucose for toxic alcohol screening.")
    if preliminary_disorder_type == 'Normal' and na is not None and cl is not None:
        output_lines.append("- All values within normal limits.")

    return "\n".join(output_lines)


# =============================================================================
# GROUP G: AGENT CONTROL (Tool-as-Answer Pattern)
# =============================================================================

async def submit_final_answer(
    response_text: str,
    confidence_score: float = 0.8
) -> str:
    """
    Use this tool to deliver your final response to the user.
    
    CRITICAL: You MUST use this tool when you have sufficient information to
    answer the user's query. Do NOT generate the answer as raw text - you
    must use this tool to submit it.
    
    This tool signals that you have completed your reasoning and are ready
    to deliver your final answer. Use it when:
    - You have gathered enough data to answer the question
    - You have completed your analysis
    - You are ready to present your findings to the user
    
    Args:
        response_text: The final answer content to be displayed to the user.
                      This should be a complete, polished response.
        confidence_score: Your self-evaluated confidence in the answer (0.0 to 1.0).
                        1.0 = highly confident, 0.0 = uncertain.
    
    Returns:
        A confirmation that the answer has been submitted.
    """
    # This is a meta-tool that doesn't need card_id
    # It simply confirms the answer is ready
    response_len = len(response_text) if response_text else 0
    return f"Final answer submitted with confidence {confidence_score}. Response length: {response_len} characters."


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
    get_quantitative_overview,
    get_specific_lab_values,
    get_abnormal_labs,
    
    # Group B: Microbiology
    get_microbiology_overview,
    get_microbiology_details,
    
    # Group C: Imaging
    get_imaging_overview,
    get_imaging_details,
    
    # Group D: Pathology
    get_pathology_overview,
    get_pathology_details,
    
    # Group E: Clinical History (Scribe Output)
    get_history_overview,
    get_history_details,
    
    # Group F: Email Notifications
    send_email_update,

    # Group H: Acid-Base Calculator
    calculate_acid_base,

    # Group G: Agent Control (Tool-as-Answer)
    submit_final_answer,
]
