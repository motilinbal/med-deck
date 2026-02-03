"""
Clinical Data Ingestion Pipeline
Orchestrates the flow: File -> Gemini -> Pydantic Validation -> MongoDB Insert
"""

import json
import os
import logging
from typing import Dict, List, Tuple, Any
from pymongo import MongoClient
from pymongo.errors import WriteError, BulkWriteError
from pydantic import ValidationError

# Import local modules
from ocr_engine import extract_data_from_image
from models import DiagnosticReport, Observation

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# MongoDB configuration
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.getenv("DB_NAME", "clinical_data_repository")


def clean_json_string(raw_text: str) -> str:
    """
    Cleans the raw JSON string from Gemini by removing markdown code blocks.
    
    Args:
        raw_text: Raw text potentially wrapped in markdown backticks
        
    Returns:
        Clean JSON string
    """
    cleaned = raw_text.strip()
    
    # Remove markdown code block markers
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]  # Remove ```json
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]  # Remove ```
    
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]  # Remove trailing ```
    
    return cleaned.strip()


def parse_and_validate(bundle_data: Dict[str, Any]) -> Tuple[List[DiagnosticReport], List[Observation], List[Dict]]:
    """
    Parses and validates FHIR Bundle entries against Pydantic models.
    
    Args:
        bundle_data: Parsed JSON dictionary containing FHIR Bundle
        
    Returns:
        Tuple of (valid_reports, valid_observations, errors)
    """
    valid_reports: List[DiagnosticReport] = []
    valid_observations: List[Observation] = []
    errors: List[Dict] = []
    
    # Check if this is a Bundle
    if bundle_data.get("resourceType") != "Bundle":
        logger.error("Root object is not a FHIR Bundle")
        errors.append({
            "error": "Invalid root resource type",
            "expected": "Bundle",
            "received": bundle_data.get("resourceType")
        })
        return valid_reports, valid_observations, errors
    
    entries = bundle_data.get("entry", [])
    if not entries:
        logger.warning("Bundle contains no entries")
        return valid_reports, valid_observations, errors
    
    logger.info(f"Processing {len(entries)} entries from Bundle")
    
    for idx, entry in enumerate(entries):
        resource = entry.get("resource", {}) if isinstance(entry, dict) else entry
        
        if not resource:
            logger.warning(f"Entry {idx}: No resource found")
            errors.append({"entry_index": idx, "error": "No resource in entry"})
            continue
        
        resource_type = resource.get("resourceType")
        
        try:
            if resource_type == "DiagnosticReport":
                # Validate against DiagnosticReport model
                report = DiagnosticReport.model_validate(resource)
                valid_reports.append(report)
                logger.debug(f"Entry {idx}: Validated DiagnosticReport (id={report.id})")
                
            elif resource_type == "Observation":
                # Validate against Observation model
                observation = Observation.model_validate(resource)
                valid_observations.append(observation)
                logger.debug(f"Entry {idx}: Validated Observation (id={observation.id})")
                
            else:
                logger.warning(f"Entry {idx}: Unknown resource type '{resource_type}'")
                errors.append({
                    "entry_index": idx,
                    "error": "Unknown resource type",
                    "resource_type": resource_type
                })
                
        except ValidationError as e:
            # Log detailed validation error but don't crash
            error_details = {
                "entry_index": idx,
                "resource_type": resource_type,
                "error": "Validation failed",
                "details": e.errors()
            }
            errors.append(error_details)
            logger.error(f"Entry {idx}: Validation failed for {resource_type}")
            for error in e.errors():
                logger.error(f"  - {error['loc']}: {error['msg']}")
                
        except Exception as e:
            # Catch any other unexpected errors
            error_details = {
                "entry_index": idx,
                "resource_type": resource_type,
                "error": f"Unexpected error: {str(e)}"
            }
            errors.append(error_details)
            logger.error(f"Entry {idx}: Unexpected error processing {resource_type}: {e}")
    
    return valid_reports, valid_observations, errors


def persist_bundle(
    valid_reports: List[DiagnosticReport],
    valid_observations: List[Observation],
    mongo_uri: str = MONGO_URI,
    db_name: str = DB_NAME
) -> Dict[str, Any]:
    """
    Persists validated Pydantic models to MongoDB collections.
    
    Args:
        valid_reports: List of validated DiagnosticReport models
        valid_observations: List of validated Observation models
        mongo_uri: MongoDB connection URI
        db_name: Database name
        
    Returns:
        Dictionary with insertion results and statistics
    """
    results = {
        "reports_inserted": 0,
        "observations_inserted": 0,
        "errors": []
    }
    
    client = MongoClient(mongo_uri)
    
    try:
        db = client[db_name]
        
        # Insert DiagnosticReports
        if valid_reports:
            try:
                # Convert Pydantic models to dicts for MongoDB
                report_dicts = [
                    report.model_dump(by_alias=True, exclude_none=True)
                    for report in valid_reports
                ]
                
                # Ensure _id is set (use id field or let MongoDB generate)
                for report_dict in report_dicts:
                    if "_id" not in report_dict or report_dict["_id"] is None:
                        # If no ID provided, MongoDB will auto-generate ObjectId
                        report_dict.pop("_id", None)
                
                insert_result = db.diagnostic_reports.insert_many(report_dicts, ordered=False)
                results["reports_inserted"] = len(insert_result.inserted_ids)
                logger.info(f"Inserted {results['reports_inserted']} DiagnosticReports")
                
            except BulkWriteError as bwe:
                # Handle partial failures in bulk insert
                write_errors = bwe.details.get("writeErrors", [])
                results["errors"].extend(write_errors)
                successful_inserts = len(valid_reports) - len(write_errors)
                results["reports_inserted"] = successful_inserts
                logger.error(f"Bulk write error for reports: {len(write_errors)} failures")
                
            except WriteError as we:
                results["errors"].append({"collection": "diagnostic_reports", "error": str(we)})
                logger.error(f"Write error for reports: {we}")
        
        # Insert Observations
        if valid_observations:
            try:
                # Convert Pydantic models to dicts for MongoDB
                observation_dicts = [
                    obs.model_dump(by_alias=True, exclude_none=True)
                    for obs in valid_observations
                ]
                
                # Ensure _id is set
                for obs_dict in observation_dicts:
                    if "_id" not in obs_dict or obs_dict["_id"] is None:
                        obs_dict.pop("_id", None)
                
                insert_result = db.observations.insert_many(observation_dicts, ordered=False)
                results["observations_inserted"] = len(insert_result.inserted_ids)
                logger.info(f"Inserted {results['observations_inserted']} Observations")
                
            except BulkWriteError as bwe:
                write_errors = bwe.details.get("writeErrors", [])
                results["errors"].extend(write_errors)
                successful_inserts = len(valid_observations) - len(write_errors)
                results["observations_inserted"] = successful_inserts
                logger.error(f"Bulk write error for observations: {len(write_errors)} failures")
                
            except WriteError as we:
                results["errors"].append({"collection": "observations", "error": str(we)})
                logger.error(f"Write error for observations: {we}")
    
    finally:
        client.close()
    
    return results


def ingest_document(
    file_path: str,
    mongo_uri: str = MONGO_URI,
    db_name: str = DB_NAME
) -> Dict[str, Any]:
    """
    Main entry point for ingesting a clinical document.
    
    Orchestrates the full pipeline:
    File -> Gemini OCR -> JSON Cleaning -> Pydantic Validation -> MongoDB Insert
    
    Args:
        file_path: Path to the image/PDF file to process
        mongo_uri: MongoDB connection URI
        db_name: Database name
        
    Returns:
        Dictionary with ingestion summary
    """
    summary = {
        "file_path": file_path,
        "success": False,
        "extraction_success": False,
        "validation_success": False,
        "persistence_success": False,
        "reports_count": 0,
        "observations_count": 0,
        "validation_errors": [],
        "persistence_errors": [],
        "message": ""
    }
    
    logger.info(f"Starting ingestion for: {file_path}")
    
    # Step 1: Extract data using OCR/LLM
    try:
        raw_json = extract_data_from_image(file_path)
        summary["extraction_success"] = True
        logger.info("Extraction completed successfully")
    except Exception as e:
        summary["message"] = f"Extraction failed: {str(e)}"
        logger.error(summary["message"])
        return summary
    
    # Step 2: Clean and parse JSON
    try:
        cleaned_json = clean_json_string(raw_json)
        bundle_data = json.loads(cleaned_json)
        logger.info("JSON parsing completed successfully")
    except json.JSONDecodeError as e:
        summary["message"] = f"JSON parsing failed: {str(e)}"
        logger.error(summary["message"])
        # Log the raw response for debugging
        logger.debug(f"Raw response:\n{raw_json[:500]}...")
        return summary
    except Exception as e:
        summary["message"] = f"Unexpected error during JSON cleaning: {str(e)}"
        logger.error(summary["message"])
        return summary
    
    # Step 3: Validate against Pydantic models
    try:
        valid_reports, valid_observations, validation_errors = parse_and_validate(bundle_data)
        summary["validation_errors"] = validation_errors
        summary["reports_count"] = len(valid_reports)
        summary["observations_count"] = len(valid_observations)
        
        if valid_reports or valid_observations:
            summary["validation_success"] = True
            logger.info(f"Validation complete: {len(valid_reports)} reports, {len(valid_observations)} observations")
        else:
            summary["message"] = "No valid resources found after validation"
            logger.warning(summary["message"])
            if validation_errors:
                summary["message"] += f" ({len(validation_errors)} validation errors)"
            return summary
            
    except Exception as e:
        summary["message"] = f"Validation processing failed: {str(e)}"
        logger.error(summary["message"])
        return summary
    
    # Step 4: Persist to MongoDB
    try:
        persistence_results = persist_bundle(valid_reports, valid_observations, mongo_uri, db_name)
        summary["persistence_errors"] = persistence_results.get("errors", [])
        summary["reports_inserted"] = persistence_results.get("reports_inserted", 0)
        summary["observations_inserted"] = persistence_results.get("observations_inserted", 0)
        
        if not summary["persistence_errors"]:
            summary["persistence_success"] = True
            summary["success"] = True
            summary["message"] = (
                f"Successfully ingested {summary['observations_inserted']} observations "
                f"and {summary['reports_inserted']} reports"
            )
        else:
            # Partial success - some inserts failed
            summary["success"] = summary["reports_inserted"] > 0 or summary["observations_inserted"] > 0
            summary["message"] = (
                f"Partial success: {summary['observations_inserted']} observations "
                f"and {summary['reports_inserted']} reports inserted. "
                f"{len(summary['persistence_errors'])} errors occurred."
            )
        
        logger.info(summary["message"])
        
    except Exception as e:
        summary["message"] = f"Persistence failed: {str(e)}"
        logger.error(summary["message"])
        return summary
    
    return summary


if __name__ == "__main__":
    # CLI entry point
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python ingest.py <path_to_image_or_pdf>")
        print("Environment variables:")
        print("  MONGO_URI - MongoDB connection string (default: mongodb://localhost:27017)")
        print("  DB_NAME - Database name (default: clinical_data_repository)")
        sys.exit(1)
    
    file_path = sys.argv[1]
    
    # Run ingestion
    result = ingest_document(file_path)
    
    # Print summary
    print("\n" + "="*60)
    print("INGESTION SUMMARY")
    print("="*60)
    print(f"File: {result['file_path']}")
    print(f"Success: {result['success']}")
    print(f"Extraction: {'✓' if result['extraction_success'] else '✗'}")
    print(f"Validation: {'✓' if result['validation_success'] else '✗'}")
    print(f"Persistence: {'✓' if result['persistence_success'] else '✗'}")
    print(f"\nResources Found: {result['reports_count']} reports, {result['observations_count']} observations")
    print(f"Resources Inserted: {result.get('reports_inserted', 0)} reports, {result.get('observations_inserted', 0)} observations")
    
    if result['validation_errors']:
        print(f"\nValidation Errors: {len(result['validation_errors'])}")
        for err in result['validation_errors'][:5]:  # Show first 5
            print(f"  - Entry {err.get('entry_index', 'N/A')}: {err.get('error', 'Unknown')}")
        if len(result['validation_errors']) > 5:
            print(f"  ... and {len(result['validation_errors']) - 5} more")
    
    if result['persistence_errors']:
        print(f"\nPersistence Errors: {len(result['persistence_errors'])}")
    
    print(f"\nMessage: {result['message']}")
    print("="*60)
    
    # Exit with appropriate code
    sys.exit(0 if result['success'] else 1)
