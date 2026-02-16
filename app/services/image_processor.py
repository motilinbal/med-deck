"""
Image Processing Service for MedDeck Server.

This module handles processing shared lab images:
- OCR extraction using Gemini
- Formatting OCR results as markdown for user preview
- Managing pending_images collection
- Storing approved images to database

Usage:
    from app.services.image_processor import process_image

    result = await process_image(image_bytes)
"""

import json
import logging
import uuid
from typing import Any, Dict, Optional

from database import (
    create_pending_image,
    get_pending_image,
    delete_pending_image,
    get_all_pending_images,
    store_quantitative_labs,
    store_reference_ranges,
    store_microbiology_reports,
    store_pathology_reports,
    store_imaging_reports,
    append_chat_message,
)
from models import MessageRole
from ocr_engine import extract_data_from_file

logger = logging.getLogger(__name__)


def generate_image_id() -> str:
    """Generate a unique image ID."""
    return f"img_{uuid.uuid4().hex[:12]}"


def format_as_markdown(ocr_data: Dict[str, Any]) -> str:
    """
    Convert OCR JSON output to readable markdown for user review.

    Args:
        ocr_data: Dict containing quantitative, microbiology, pathology, imaging

    Returns:
        Markdown formatted string
    """
    lines = ["### Lab Results Captured\n"]

    # Quantitative labs
    quantitative = ocr_data.get("quantitative", [])
    if quantitative:
        # Separate reference ranges from quantitative
        quant_labs = [q for q in quantitative if isinstance(q, dict) and q.get("category") != "Reference"]
        reference = [q for q in quantitative if isinstance(q, dict) and q.get("category") == "Reference"]

        if quant_labs:
            lines.append("**Quantitative Labs:**\n")
            lines.append("| Date | Material | Test | Value | Note |")
            lines.append("|------|----------|------|-------|------|")

            for item in quant_labs:
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
            if not isinstance(item, dict):
                continue
            date = item.get("date", "")
            material = item.get("material", "")
            gram_stain = item.get("gram_stain", "")
            cultures = item.get("culture", [])

            lines.append(f"- **{date}** {material}")
            if gram_stain:
                lines.append(f"  - Gram stain: {gram_stain}")
            for culture in cultures:
                if not isinstance(culture, dict):
                    continue
                name = culture.get("name", "")
                sensitivities = culture.get("sensitivities", {})
                sens_str = ", ".join([f"{k}: {v}" for k, v in sensitivities.items()]) if sensitivities else "No sensitivities"
                lines.append(f"  - {name}: {sens_str}")
        lines.append("")

    # Pathology
    pathology = ocr_data.get("pathology", [])
    if pathology:
        lines.append("**Pathology:**\n")
        for item in pathology:
            if not isinstance(item, dict):
                continue
            date = item.get("date", "")
            specimen = item.get("specimen", "")
            clinical_data = item.get("clinical_data", "")
            macroscopic = item.get("macroscopic", "")
            microscopic = item.get("microscopic", "")
            diagnosis = item.get("diagnosis", "")

            lines.append(f"- **{date}** {specimen}")
            if clinical_data:
                lines.append(f"  - Clinical: {clinical_data}")
            if macroscopic:
                lines.append(f"  - Macroscopic: {macroscopic}")
            if microscopic:
                lines.append(f"  - Microscopic: {microscopic}")
            if diagnosis:
                lines.append(f"  - Diagnosis: {diagnosis}")
        lines.append("")

    # Imaging
    imaging = ocr_data.get("imaging", [])
    if imaging:
        lines.append("**Imaging:**\n")
        for item in imaging:
            if not isinstance(item, dict):
                continue
            date = item.get("date", "")
            exam_type = item.get("exam_type", "")
            indication = item.get("indication", "")
            comparison = item.get("comparison", "")
            findings = item.get("findings", {})
            summary = item.get("summary", "")

            lines.append(f"- **{date}** {exam_type}")
            if indication:
                lines.append(f"  - Indication: {indication}")
            if comparison:
                lines.append(f"  - Comparison: {comparison}")
            if findings:
                for organ, finding in findings.items():
                    lines.append(f"  - {organ}: {finding}")
            if summary:
                lines.append(f"  - Summary: {summary}")
        lines.append("")

    # If nothing found
    if len(lines) == 1:
        lines.append("_No structured data detected. Please review the image manually._")
        lines.append("")

    # Add approval prompt
    lines.append("**Please select a patient card and approve or decline.**")

    return "\n".join(lines)


def parse_ocr_response(ocr_json_str: str) -> Dict[str, Any]:
    """
    Parse OCR JSON response into structured format.

    Args:
        ocr_json_str: Raw JSON string from OCR

    Returns:
        Dict with quantitative, microbiology, pathology, imaging keys
    """
    try:
        ocr_data = json.loads(ocr_json_str)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse OCR JSON: {e}")
        raise ValueError(f"OCR extraction failed: {ocr_json_str[:200]}")

    # Handle case where OCR returns a list
    if isinstance(ocr_data, list):
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

    return ocr_data


async def process_image(image_bytes: bytes, filename: str = "capture.jpg") -> Dict[str, Any]:
    """
    Process a captured image through OCR and create pending record.

    Args:
        image_bytes: Raw image data
        filename: Original filename for type detection

    Returns:
        Dict with image_id, preview, extracted_data
    """
    import tempfile
    import os

    # Generate unique ID
    image_id = generate_image_id()

    # Write to temp file for OCR processing
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
        tmp.write(image_bytes)
        tmp_path = tmp.name

    try:
        # Run OCR with "all" type
        logger.info(f"Running OCR on image {image_id}")
        ocr_json_str = extract_data_from_file(tmp_path, type="all")

        # Parse JSON response
        ocr_data = parse_ocr_response(ocr_json_str)

        # Format as markdown
        preview = format_as_markdown(ocr_data)

        # Store in pending_images collection
        await create_pending_image(image_id, preview, ocr_data)

        logger.info(f"Image processing complete: {image_id}")

        return {
            "image_id": image_id,
            "preview": preview,
            "extracted_data": ocr_data
        }

    finally:
        # Cleanup temp file
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception as e:
                logger.warning(f"Failed to cleanup temp file: {e}")


async def process_decision(image_id: str, decision: str, card_id: Optional[str] = None) -> Dict[str, Any]:
    """
    Process a user's decision on a pending image.

    Args:
        image_id: The image ID
        decision: "approve" or "decline"
        card_id: Required if decision is "approve"

    Returns:
        Result dict with status and message

    Raises:
        ValueError: If decision is invalid or card_id missing for approve
    """
    # Get pending image
    pending = await get_pending_image(image_id)
    if not pending:
        raise ValueError(f"Pending image {image_id} not found")

    if pending.get("decision") != "pending":
        raise ValueError(f"Image {image_id} already decided: {pending.get('decision')}")

    if decision == "decline":
        # Just delete the pending record
        await delete_pending_image(image_id)
        logger.info(f"Image {image_id} declined and removed")
        return {
            "status": "success",
            "message": "Image declined",
            "image_id": image_id
        }

    if decision == "approve":
        if not card_id:
            raise ValueError("card_id is required for approve decision")

        # Get the extracted data
        extracted_data = pending.get("extracted_data", {})

        # Store data in database using the same functions as ingestion
        total_stats = await store_extracted_data(card_id, extracted_data)

        # Delete pending record
        await delete_pending_image(image_id)

        # Add info message to chat (optional - showing data was stored)
        summary = (
            f"**Image Lab Data Stored**\n"
            f"• Quantitative: {total_stats.get('quant_labs_inserted', 0)} labs\n"
            f"• Microbiology: {total_stats.get('microbiology_inserted', 0)} reports\n"
            f"• Pathology: {total_stats.get('pathology_inserted', 0)} reports\n"
            f"• Imaging: {total_stats.get('imaging_inserted', 0)} reports"
        )
        await append_chat_message(card_id, MessageRole.INFO, summary)

        logger.info(f"Image {image_id} approved and stored to card {card_id}")

        return {
            "status": "success",
            "message": "Image approved and stored",
            "image_id": image_id,
            "card_id": card_id
        }

    raise ValueError(f"Invalid decision: {decision}")


async def store_extracted_data(card_id: str, extracted_data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Store extracted data to database.

    Args:
        card_id: Target card ID
        extracted_data: Dict with quantitative, microbiology, pathology, imaging

    Returns:
        Stats dict with counts
    """
    total_stats = {
        "quant_labs_inserted": 0,
        "quant_labs_duplicates": 0,
        "ref_ranges_inserted": 0,
        "ref_ranges_duplicates": 0,
        "microbiology_inserted": 0,
        "microbiology_duplicates": 0,
        "pathology_inserted": 0,
        "pathology_duplicates": 0,
        "imaging_inserted": 0,
        "imaging_duplicates": 0,
    }

    # Split quantitative data into labs and reference ranges
    quantitative_data = extracted_data.get("quantitative", [])
    quant_labs = []
    ref_ranges = []

    for item in quantitative_data:
        if isinstance(item, dict) and item.get("category") == "Reference":
            ref_ranges.append(item)
        else:
            quant_labs.append(item)

    # Store quantitative labs
    if quant_labs:
        lab_stats = await store_quantitative_labs(card_id, quant_labs)
        total_stats["quant_labs_inserted"] += lab_stats.get("inserted", 0)
        total_stats["quant_labs_duplicates"] += lab_stats.get("duplicates_skipped", 0)
        logger.info(f"Stored {lab_stats.get('inserted', 0)} quantitative labs for card {card_id}")

    # Store reference ranges
    if ref_ranges:
        ref_stats = await store_reference_ranges(card_id, ref_ranges)
        total_stats["ref_ranges_inserted"] += ref_stats.get("inserted", 0)
        total_stats["ref_ranges_duplicates"] += ref_stats.get("duplicates_skipped", 0)
        logger.info(f"Stored {ref_stats.get('inserted', 0)} reference ranges for card {card_id}")

    # Store microbiology reports
    microbiology_data = extracted_data.get("microbiology", [])
    if microbiology_data:
        micro_stats = await store_microbiology_reports(card_id, microbiology_data)
        total_stats["microbiology_inserted"] += micro_stats.get("inserted", 0)
        total_stats["microbiology_duplicates"] += micro_stats.get("duplicates_skipped", 0)
        logger.info(f"Stored {micro_stats.get('inserted', 0)} microbiology reports for card {card_id}")

    # Store pathology reports
    pathology_data = extracted_data.get("pathology", [])
    if pathology_data:
        path_stats = await store_pathology_reports(card_id, pathology_data)
        total_stats["pathology_inserted"] += path_stats.get("inserted", 0)
        total_stats["pathology_duplicates"] += path_stats.get("duplicates_skipped", 0)
        logger.info(f"Stored {path_stats.get('inserted', 0)} pathology reports for card {card_id}")

    # Store imaging reports
    imaging_data = extracted_data.get("imaging", [])
    if imaging_data:
        imaging_stats = await store_imaging_reports(card_id, imaging_data)
        total_stats["imaging_inserted"] += imaging_stats.get("inserted", 0)
        total_stats["imaging_duplicates"] += imaging_stats.get("duplicates_skipped", 0)
        logger.info(f"Stored {imaging_stats.get('inserted', 0)} imaging reports for card {card_id}")

    logger.info(f"Database persistence complete for card {card_id}: {total_stats}")

    return total_stats
