"""
Camera Capture Service for MedDeck Server.

This module handles processing camera-captured lab images:
- OCR extraction using Gemini
- Formatting OCR results as markdown for user preview
- Creating pending ingestion for user approval

Usage:
    from app.services.capture import process_capture

    pending_id = await process_capture(card_id, image_bytes)
"""

import json
import logging
from typing import Any, Dict, Optional

from database import (
    create_pending_ingestion,
    append_chat_message,
)
from models import MessageRole, PendingIngestion
from ocr_engine import extract_data_from_file

logger = logging.getLogger(__name__)


def format_capture_as_markdown(ocr_data: Dict[str, Any]) -> str:
    """
    Convert OCR JSON output to readable markdown for user review.

    Args:
        ocr_data: Dict containing quantitative, microbiology, pathology, imaging

    Returns:
        Markdown formatted string for chat display
    """
    lines = ["### Lab Results Captured\n"]

    # Quantitative labs
    quantitative = ocr_data.get("quantitative", [])
    if quantitative:
        lines.append("**Quantitative Labs:**\n")
        lines.append("| Date | Material | Test | Value | Note |")
        lines.append("|------|----------|------|-------|------|")

        for item in quantitative:
            if isinstance(item, dict):
                if "results" in item:
                    # Grouped panel
                    date = item.get("date", "")
                    material = item.get("material", "")
                    results = item.get("results", {})
                    note = item.get("note", "")

                    for test_name, value in results.items():
                        lines.append(f"| {date} | {material} | {test_name} | {value} | {note} |")
                else:
                    # Single observation
                    date = item.get("date", "")
                    material = item.get("material", "")
                    test_name = item.get("test_name", "")
                    value = item.get("value", "")
                    note = item.get("note", "")
                    lines.append(f"| {date} | {material} | {test_name} | {value} | {note} |")

        lines.append("")

    # Reference ranges
    reference = [item for item in quantitative if isinstance(item, dict) and item.get("category") == "Reference"]
    if reference:
        lines.append("**Reference Ranges:**\n")
        for item in reference:
            test_name = item.get("test_name", "")
            material = item.get("material", "")
            low = item.get("low_value", "")
            high = item.get("high_value", "")
            units = item.get("units", "")
            lines.append(f"- {test_name} ({material}): {low}-{high} {units}")
        lines.append("")

    # Microbiology
    microbiology = ocr_data.get("microbiology", [])
    if microbiology:
        lines.append("**Microbiology:**\n")
        for item in microbiology:
            date = item.get("date", "")
            material = item.get("material", "")
            gram_stain = item.get("gram_stain", "")
            cultures = item.get("culture", [])

            lines.append(f"- **{date}** {material}")
            if gram_stain:
                lines.append(f"  - Gram stain: {gram_stain}")
            for culture in cultures:
                name = culture.get("name", "")
                sensitivities = culture.get("sensitives", {})
                sens_str = ", ".join([f"{k}: {v}" for k, v in sensitivities.items()]) if sensitivities else "N/A"
                lines.append(f"  - {name}: {sens_str}")
        lines.append("")

    # Pathology
    pathology = ocr_data.get("pathology", [])
    if pathology:
        lines.append("**Pathology:**\n")
        for item in pathology:
            date = item.get("date", "")
            specimen = item.get("specimen", "")
            diagnosis = item.get("diagnosis", "")
            lines.append(f"- **{date}** {specimen}")
            if diagnosis:
                lines.append(f"  - Diagnosis: {diagnosis}")
        lines.append("")

    # Imaging
    imaging = ocr_data.get("imaging", [])
    if imaging:
        lines.append("**Imaging:**\n")
        for item in imaging:
            date = item.get("date", "")
            exam_type = item.get("exam_type", "")
            summary = item.get("summary", "")
            lines.append(f"- **{date}** {exam_type}")
            if summary:
                lines.append(f"  - {summary}")
        lines.append("")

    # If nothing found
    if len(lines) == 1:
        lines.append("_No structured data detected. Please review the image manually._")

    # Add approval prompt
    lines.append("\n**Please review the data above and approve or discard.**")

    return "\n".join(lines)


async def create_capture_pending(
    card_id: str,
    ocr_result: Dict[str, Any],
    formatted_preview: str
) -> str:
    """
    Create a pending ingestion record for camera capture.

    Args:
        card_id: The patient card ID
        ocr_result: The OCR JSON result
        formatted_preview: Markdown formatted preview for user

    Returns:
        The pending_id of the created record
    """
    pending = PendingIngestion(
        card_id=card_id,
        source_type="capture",
        email_uid=None,
        sender=None,
        created_new_card=False,
        clean_body_chunks=[],
        has_pdf=False,
        captured_data=ocr_result,
        formatted_preview=formatted_preview,
        status="waiting_approval"
    )

    pending_id = await create_pending_ingestion(pending)
    logger.info(f"Created capture pending ingestion {pending_id} for card {card_id}")

    return pending_id


async def process_capture(card_id: str, image_bytes: bytes, filename: str = "capture.jpg") -> str:
    """
    Process a captured image through OCR and create pending ingestion.

    Args:
        card_id: The patient card ID
        image_bytes: Raw image data
        filename: Original filename for type detection

    Returns:
        The pending_id of the created pending ingestion
    """
    import tempfile
    import os

    # Write to temp file for OCR processing
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name

    try:
        # Run OCR with "all" type to get comprehensive extraction
        logger.info(f"Running OCR on captured image for card {card_id}")
        ocr_json_str = extract_data_from_file(tmp_path, type="all")

        # Parse JSON response
        try:
            ocr_data = json.loads(ocr_json_str)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse OCR JSON: {e}")
            raise ValueError(f"OCR extraction failed: {ocr_json_str[:200]}")

        # Handle case where OCR returns a list
        if isinstance(ocr_data, list):
            # Merge all items into categories
            merged: Dict[str, Any] = {
                "quantitative": [],
                "microbiology": [],
                "pathology": [],
                "imaging": []
            }
            for item in ocr_data:
                if isinstance(item, dict):
                    category = item.get("category", "")
                    if category == "Quantitative":
                        merged["quantitative"].append(item)
                    elif category == "Reference":
                        merged["quantitative"].append(item)
                    elif category == "Microbiology":
                        merged["microbiology"].append(item)
                    elif category == "Pathology":
                        merged["pathology"].append(item)
                    elif category == "Imaging":
                        merged["imaging"].append(item)
            ocr_data = merged

        # Ensure all keys exist
        if not isinstance(ocr_data, dict):
            ocr_data = {"quantitative": [], "microbiology": [], "pathology": [], "imaging": []}
        else:
            ocr_data.setdefault("quantitative", [])
            ocr_data.setdefault("microbiology", [])
            ocr_data.setdefault("pathology", [])
            ocr_data.setdefault("imaging", [])

        # Format as markdown for user preview
        formatted_preview = format_capture_as_markdown(ocr_data)

        # Create pending ingestion
        pending_id = await create_capture_pending(card_id, ocr_data, formatted_preview)

        # Add info message to chat with preview
        await append_chat_message(
            card_id,
            MessageRole.INFO,
            formatted_preview
        )

        logger.info(f"Capture processing complete for card {card_id}, pending {pending_id}")
        return pending_id

    finally:
        # Cleanup temp file
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception as e:
                logger.warning(f"Failed to cleanup temp file: {e}")
