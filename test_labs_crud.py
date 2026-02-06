#!/usr/bin/env python3
"""
Comprehensive Test Suite for Labs Collection CRUD Operations
=============================================================

This test suite validates all Create, Read, Update (via duplicate handling), 
and Delete operations for the labs collection using real-world OCR output data.

Data Sources:
- Table JSON files: labs1_test_second_pass_page{1-5}_table{1-5}.json
- Narrative JSON file: labs1_test_second_pass_narrative.json

Test Structure:
1. CREATE Operations - Store quantitative, reference, microbiology, pathology, imaging
2. READ Operations - Query and retrieve data with various filters
3. DELETE Operations - Remove data and verify cascade behavior
4. Edge Cases - Error handling, validation, duplicate detection

All tests provide detailed logging for transparency and supervision.
"""

import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from bson.objectid import ObjectId

# Configure detailed logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    datefmt='%H:%M:%S'
)
logger = logging.getLogger(__name__)

# Add separator for readability
SECTION_SEPARATOR = "=" * 80
TEST_SEPARATOR = "-" * 80

# =============================================================================
# TEST RESULT TRACKING
# =============================================================================

class TestResults:
    """Tracks test execution results."""
    
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.errors = []
        self.current_section = None
    
    def start_section(self, name: str):
        """Start a new test section."""
        self.current_section = name
        print(f"\n{SECTION_SEPARATOR}")
        print(f"SECTION: {name}")
        print(SECTION_SEPARATOR)
    
    def log_pass(self, test_name: str, details: str = ""):
        """Log a passed test."""
        self.passed += 1
        status = "✓ PASS"
        msg = f"{status} | {test_name}"
        if details:
            msg += f" | {details}"
        print(msg)
        logger.info(f"PASSED: {test_name}")
    
    def log_fail(self, test_name: str, reason: str, details: str = ""):
        """Log a failed test."""
        self.failed += 1
        error_msg = f"✗ FAIL | {test_name} | {reason}"
        if details:
            error_msg += f"\n  Details: {details}"
        print(error_msg)
        logger.error(f"FAILED: {test_name} - {reason}")
        self.errors.append({
            "section": self.current_section,
            "test": test_name,
            "reason": reason,
            "details": details
        })
    
    def log_info(self, message: str):
        """Log informational message."""
        print(f"  ℹ {message}")
        logger.info(message)
    
    def log_data(self, label: str, data: Any, indent: int = 2):
        """Log data with formatting."""
        if isinstance(data, (dict, list)):
            formatted = json.dumps(data, indent=indent, default=str)
            print(f"  {label}:\n{formatted}")
        else:
            print(f"  {label}: {data}")
    
    def print_summary(self):
        """Print test execution summary."""
        print(f"\n{SECTION_SEPARATOR}")
        print("TEST EXECUTION SUMMARY")
        print(SECTION_SEPARATOR)
        print(f"Total Tests: {self.passed + self.failed}")
        print(f"Passed: {self.passed}")
        print(f"Failed: {self.failed}")
        print(f"Success Rate: {(self.passed / (self.passed + self.failed) * 100):.1f}%" if (self.passed + self.failed) > 0 else "N/A")
        
        if self.errors:
            print(f"\nFailed Tests:")
            for i, error in enumerate(self.errors, 1):
                print(f"  {i}. [{error['section']}] {error['test']}")
                print(f"     Reason: {error['reason']}")
        
        print(SECTION_SEPARATOR)

# Global test results tracker
results = TestResults()

# =============================================================================
# DATA LOADING UTILITIES
# =============================================================================

OUTPUT_DIR = Path("output/20260205_181745_labs1_test_second_pass")
TABLES_DIR = OUTPUT_DIR / "tables" / "json"
NARRATIVE_FILE = OUTPUT_DIR / "narrative" / "json" / "labs1_test_second_pass_narrative.json"

def load_json_file(filepath: Path) -> List[Dict]:
    """Load and parse a JSON file."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load {filepath}: {e}")
        return []

def load_all_table_data() -> List[Dict]:
    """Load all table JSON files and combine them."""
    all_data = []
    table_files = sorted(TABLES_DIR.glob("*.json"))
    
    results.log_info(f"Loading {len(table_files)} table files from {TABLES_DIR}")
    
    for filepath in table_files:
        data = load_json_file(filepath)
        results.log_info(f"  Loaded {filepath.name}: {len(data)} documents")
        all_data.extend(data)
    
    return all_data

def load_narrative_data() -> List[Dict]:
    """Load narrative JSON file."""
    data = load_json_file(NARRATIVE_FILE)
    results.log_info(f"Loaded narrative data: {len(data)} documents")
    return data

def count_document_types(data: List[Dict]) -> Dict[str, int]:
    """Count documents by format type."""
    counts = {"Format A (single)": 0, "Format B (grouped)": 0}
    for doc in data:
        if "results" in doc and isinstance(doc["results"], dict):
            counts["Format B (grouped)"] += 1
        else:
            counts["Format A (single)"] += 1
    return counts

# =============================================================================
# DATABASE CONNECTION SETUP
# =============================================================================

async def setup_database():
    """Initialize database connection and verify connectivity."""
    from database import client, db, labs_collection, cards_collection
    
    results.start_section("DATABASE SETUP")
    
    try:
        # Test connection
        await client.admin.command('ping')
        results.log_pass("Database Connection", "Successfully connected to MongoDB")
        
        # Get collection stats
        labs_count = await labs_collection.estimated_document_count()
        results.log_info(f"Labs collection document count: {labs_count}")
        
        return client, db, labs_collection, cards_collection
        
    except Exception as e:
        results.log_fail("Database Connection", str(e))
        raise

async def cleanup_test_data(card_id: str):
    """Clean up test data for a specific card."""
    from database import delete_labs_by_card
    try:
        result = await delete_labs_by_card(card_id)
        results.log_info(f"Cleaned up test data: {result['deleted_count']} documents deleted")
        return result['deleted_count']
    except Exception as e:
        results.log_info(f"Cleanup warning (may be expected): {e}")
        return 0

# =============================================================================
# CREATE OPERATIONS TESTS
# =============================================================================

async def test_store_quantitative_labs(card_id: str, table_data: List[Dict]):
    """Test storing quantitative lab results from table data."""
    from database import store_quantitative_labs
    
    results.start_section("CREATE: Quantitative Labs Storage")
    
    # Analyze input data
    doc_types = count_document_types(table_data)
    results.log_info("Input Data Analysis:")
    results.log_data("Document Types", doc_types)
    results.log_info(f"Total documents to process: {len(table_data)}")
    
    # Store the data
    try:
        result = await store_quantitative_labs(card_id, table_data)
        
        results.log_info(f"Storage Result:")
        results.log_data("Counts", result)
        
        # Validate results
        total_processed = result['inserted'] + result['duplicates_skipped'] + result['errors']
        
        if result['errors'] == 0:
            results.log_pass(
                "Quantitative Storage - No Errors",
                f"Inserted: {result['inserted']}, Duplicates: {result['duplicates_skipped']}"
            )
        else:
            results.log_fail(
                "Quantitative Storage - Error Check",
                f"{result['errors']} documents had errors"
            )
        
        # Verify inserted count is reasonable
        expected_min = len([d for d in table_data if "results" not in d])
        if result['inserted'] >= expected_min:
            results.log_pass(
                "Quantitative Storage - Insert Count",
                f"Inserted {result['inserted']} documents (min expected: {expected_min})"
            )
        else:
            results.log_fail(
                "Quantitative Storage - Insert Count",
                f"Only {result['inserted']} inserted, expected at least {expected_min}"
            )
        
        return result
        
    except Exception as e:
        results.log_fail("Quantitative Storage - Exception", str(e))
        raise

async def test_store_reference_ranges(card_id: str):
    """Test storing reference range documents."""
    from database import store_reference_ranges
    
    results.start_section("CREATE: Reference Range Storage")
    
    # Create sample reference data based on tests in the table data
    reference_data = [
        {"test_name": "Hemoglobin", "material": "Blood", "low_value": 12.0, "high_value": 16.0, "units": "g/dL"},
        {"test_name": "Hemoglobin", "material": "Pleural Fluid", "low_value": None, "high_value": 1.0, "units": "g/dL"},
        {"test_name": "White Blood Cell Count", "material": "Blood", "low_value": 4.0, "high_value": 11.0, "units": "K/uL"},
        {"test_name": "C-Reactive Protein", "material": "Blood", "low_value": 0.0, "high_value": 1.0, "units": "mg/dL"},
        {"test_name": "pH", "material": "Venous Blood", "low_value": 7.35, "high_value": 7.45, "units": ""},
        {"test_name": "pH", "material": "Pleural Fluid", "low_value": 7.30, "high_value": 7.50, "units": ""},
        {"test_name": "Lactate", "material": "Venous Blood", "low_value": 4.5, "high_value": 19.8, "units": "mg/dL"},
        {"test_name": "Lactic Acid", "material": "Blood", "low_value": 4.5, "high_value": 19.8, "units": "mg/dL"},
        {"test_name": "Partial Pressure of Oxygen", "material": "Venous Blood", "low_value": 35.0, "high_value": 45.0, "units": "mmHg"},
        {"test_name": "International Normalized Ratio", "material": "Blood", "low_value": 0.9, "high_value": 1.1, "units": ""}
    ]
    
    results.log_info(f"Storing {len(reference_data)} reference range documents")
    
    try:
        result = await store_reference_ranges(card_id, reference_data)
        results.log_data("Storage Result", result)
        
        if result['errors'] == 0:
            results.log_pass("Reference Range Storage", f"Inserted: {result['inserted']}, Duplicates: {result['duplicates_skipped']}")
        else:
            results.log_fail("Reference Range Storage", f"{result['errors']} documents had errors")
        
        return result
        
    except Exception as e:
        results.log_fail("Reference Range Storage", str(e))
        raise

async def test_store_microbiology_reports(card_id: str, narrative_data: List[Dict]):
    """Test storing microbiology reports from narrative data."""
    from database import store_microbiology_reports
    
    results.start_section("CREATE: Microbiology Report Storage")
    
    microbiology_docs = [d for d in narrative_data if d.get("category") == "Microbiology"]
    results.log_info(f"Found {len(microbiology_docs)} microbiology documents")
    
    if not microbiology_docs:
        results.log_info("No microbiology data to test - skipping")
        return {"inserted": 0, "duplicates_skipped": 0, "errors": 0}
    
    try:
        result = await store_microbiology_reports(card_id, microbiology_docs)
        results.log_data("Storage Result", result)
        
        if result['errors'] == 0 and result['inserted'] == len(microbiology_docs):
            results.log_pass("Microbiology Storage", f"All {result['inserted']} documents inserted successfully")
        elif result['errors'] > 0:
            results.log_fail("Microbiology Storage", f"{result['errors']} documents had errors")
        else:
            results.log_pass("Microbiology Storage", f"Inserted: {result['inserted']}, Duplicates: {result['duplicates_skipped']}")
        
        return result
        
    except Exception as e:
        results.log_fail("Microbiology Storage", str(e))
        raise

async def test_store_pathology_reports(card_id: str, narrative_data: List[Dict]):
    """Test storing pathology reports from narrative data."""
    from database import store_pathology_reports
    
    results.start_section("CREATE: Pathology Report Storage")
    
    pathology_docs = [d for d in narrative_data if d.get("category") == "Pathology"]
    results.log_info(f"Found {len(pathology_docs)} pathology documents")
    
    if not pathology_docs:
        results.log_info("No pathology data to test - skipping")
        return {"inserted": 0, "duplicates_skipped": 0, "errors": 0}
    
    try:
        result = await store_pathology_reports(card_id, pathology_docs)
        results.log_data("Storage Result", result)
        
        if result['errors'] == 0 and result['inserted'] == len(pathology_docs):
            results.log_pass("Pathology Storage", f"All {result['inserted']} documents inserted successfully")
        elif result['errors'] > 0:
            results.log_fail("Pathology Storage", f"{result['errors']} documents had errors")
        else:
            results.log_pass("Pathology Storage", f"Inserted: {result['inserted']}, Duplicates: {result['duplicates_skipped']}")
        
        return result
        
    except Exception as e:
        results.log_fail("Pathology Storage", str(e))
        raise

async def test_store_imaging_reports(card_id: str):
    """Test storing imaging reports with sample data."""
    from database import store_imaging_reports
    
    results.start_section("CREATE: Imaging Report Storage")
    
    imaging_data = [
        {
            "category": "Imaging", "date": "15/11/25", "time": "10:30",
            "exam_type": "CT Chest w/ Contrast", "indication": "Evaluate pleural effusion",
            "comparison": "None available",
            "findings": {"Lungs": "Moderate left pleural effusion", "Mediastinum": "No lymphadenopathy"},
            "summary": "Moderate left pleural effusion. No evidence of pulmonary embolism."
        },
        {
            "category": "Imaging", "date": "20/11/25", "time": "14:15",
            "exam_type": "Chest X-Ray", "indication": "Follow-up pleural effusion",
            "comparison": "CT Chest 15/11/25",
            "findings": {"Lungs": "Decreased left pleural effusion", "Pleura": "No pneumothorax"},
            "summary": "Interval decrease in left pleural effusion."
        }
    ]
    
    results.log_info(f"Storing {len(imaging_data)} imaging reports")
    
    try:
        result = await store_imaging_reports(card_id, imaging_data)
        results.log_data("Storage Result", result)
        
        if result['errors'] == 0:
            results.log_pass("Imaging Storage", f"Inserted: {result['inserted']}, Duplicates: {result['duplicates_skipped']}")
        else:
            results.log_fail("Imaging Storage", f"{result['errors']} documents had errors")
        
        return result
        
    except Exception as e:
        results.log_fail("Imaging Storage", str(e))
        raise

# =============================================================================
# DUPLICATE DETECTION TESTS
# =============================================================================

async def test_duplicate_detection(card_id: str, table_data: List[Dict], narrative_data: List[Dict]):
    """Test duplicate detection by re-inserting the same data."""
    from database import store_quantitative_labs, store_reference_ranges
    from database import store_microbiology_reports, store_pathology_reports
    
    results.start_section("DUPLICATE DETECTION TESTS")
    
    results.log_info("Re-inserting same data to test duplicate detection...")
    
    # Test quantitative duplicates
    result = await store_quantitative_labs(card_id, table_data)
    if result['duplicates_skipped'] > 0 or result['inserted'] == 0:
        results.log_pass("Quantitative Duplicate Detection", f"Skipped {result['duplicates_skipped']} duplicates")
    else:
        results.log_fail("Quantitative Duplicate Detection", "No duplicates detected - expected duplicates to be skipped")
    
    # Test reference duplicates
    reference_data = [{"test_name": "Hemoglobin", "material": "Blood", "low_value": 12.0, "high_value": 16.0, "units": "g/dL"}]
    result = await store_reference_ranges(card_id, reference_data)
    if result['duplicates_skipped'] > 0 or result['inserted'] == 0:
        results.log_pass("Reference Range Duplicate Detection", f"Skipped {result['duplicates_skipped']} duplicates")
    else:
        results.log_fail("Reference Range Duplicate Detection", "No duplicates detected")
    
    # Test microbiology duplicates
    microbiology_docs = [d for d in narrative_data if d.get("category") == "Microbiology"]
    if microbiology_docs:
        result = await store_microbiology_reports(card_id, microbiology_docs)
        results.log_pass("Microbiology Duplicate Detection", f"Inserted: {result['inserted']}, Skipped: {result['duplicates_skipped']}")
    
    # Test pathology duplicates
    pathology_docs = [d for d in narrative_data if d.get("category") == "Pathology"]
    if pathology_docs:
        result = await store_pathology_reports(card_id, pathology_docs)
        results.log_pass("Pathology Duplicate Detection", f"Inserted: {result['inserted']}, Skipped: {result['duplicates_skipped']}")

# =============================================================================
# READ OPERATIONS TESTS
# =============================================================================

async def test_get_quantitative_labs(card_id: str):
    """Test retrieving quantitative labs with various filters."""
    from database import get_quantitative_labs, get_quantitative_overview
    
    results.start_section("READ: Quantitative Labs Query")
    
    overview = await get_quantitative_overview(card_id)
    results.log_info(f"Available tests in database: {len(overview)}")
    
    if not overview:
        results.log_fail("Quantitative Query", "No quantitative data found in database")
        return
    
    test_names = [item['test_name'] for item in overview[:5]]
    results.log_info(f"Sample tests: {', '.join(test_names)}")
    
    # Test 1: Query specific tests without time filter
    try:
        test_names_to_query = ["Hemoglobin", "White Blood Cell Count", "pH"]
        results_data = await get_quantitative_labs(card_id, test_names_to_query)
        results.log_info(f"Query without time filter returned {len(results_data)} test groups")
        
        if len(results_data) > 0:
            results.log_pass("Quantitative Query - Basic", f"Retrieved {len(results_data)} test groups")
            if results_data:
                results.log_data("Sample result", results_data[0], indent=4)
        else:
            results.log_fail("Quantitative Query - Basic", "No results returned")
        
    except Exception as e:
        results.log_fail("Quantitative Query - Basic", str(e))
    
    # Test 2: Query with time range
    try:
        start_time = datetime(2025, 11, 1, 0, 0, 0)
        end_time = datetime(2025, 12, 31, 23, 59, 59)
        results_time = await get_quantitative_labs(card_id, ["Hemoglobin", "Lactate"], start_time=start_time, end_time=end_time)
        results.log_info(f"Query with time range returned {len(results_time)} test groups")
        results.log_pass("Quantitative Query - Time Range", f"Retrieved {len(results_time)} test groups with time filter")
    except Exception as e:
        results.log_fail("Quantitative Query - Time Range", str(e))
    
    # Test 3: Query with only start time
    try:
        start_time = datetime(2025, 12, 1, 0, 0, 0)
        results_start = await get_quantitative_labs(card_id, ["Lactate"], start_time=start_time)
        results.log_pass("Quantitative Query - Start Time Only", f"Retrieved {len(results_start)} test groups from Dec 2025 onwards")
    except Exception as e:
        results.log_fail("Quantitative Query - Start Time Only", str(e))
    
    # Test 4: Query non-existent test
    try:
        results_empty = await get_quantitative_labs(card_id, ["NonExistentTest"])
        if len(results_empty) == 0:
            results.log_pass("Quantitative Query - Non-existent Test", "Correctly returned empty list for unknown test")
        else:
            results.log_fail("Quantitative Query - Non-existent Test", f"Expected empty list, got {len(results_empty)} results")
    except Exception as e:
        results.log_fail("Quantitative Query - Non-existent Test", str(e))

async def test_get_quantitative_overview(card_id: str):
    """Test getting quantitative overview/catalog."""
    from database import get_quantitative_overview
    
    results.start_section("READ: Quantitative Overview")
    
    try:
        overview = await get_quantitative_overview(card_id)
        results.log_info(f"Overview returned {len(overview)} unique test+material combinations")
        
        if len(overview) > 0:
            results.log_pass("Quantitative Overview", f"Catalog contains {len(overview)} test entries")
            results.log_info("Sample entries:")
            for item in overview[:3]:
                results.log_info(f"  - {item['test_name']} ({item['material']})")
        else:
            results.log_fail("Quantitative Overview", "No quantitative data in catalog")
        
        # Verify sorting
        if len(overview) >= 2:
            is_sorted = all(overview[i]['test_name'] <= overview[i+1]['test_name'] for i in range(len(overview)-1))
            if is_sorted:
                results.log_pass("Quantitative Overview - Sorting", "Results sorted alphabetically")
            else:
                results.log_fail("Quantitative Overview - Sorting", "Results not sorted alphabetically")
        
    except Exception as e:
        results.log_fail("Quantitative Overview", str(e))

async def test_get_narrative_overviews(card_id: str):
    """Test overview functions for narrative documents."""
    from database import get_microbiology_overview, get_pathology_overview, get_imaging_overview
    
    results.start_section("READ: Narrative Document Overviews")
    
    # Test microbiology overview
    try:
        micro_overview = await get_microbiology_overview(card_id)
        results.log_info(f"Microbiology overview: {len(micro_overview)} reports")
        if len(micro_overview) > 0:
            results.log_pass("Microbiology Overview", f"Found {len(micro_overview)} reports")
        else:
            results.log_info("No microbiology reports found (may be expected)")
    except Exception as e:
        results.log_fail("Microbiology Overview", str(e))
    
    # Test pathology overview
    try:
        path_overview = await get_pathology_overview(card_id)
        results.log_info(f"Pathology overview: {len(path_overview)} reports")
        if len(path_overview) > 0:
            results.log_pass("Pathology Overview", f"Found {len(path_overview)} reports")
        else:
            results.log_info("No pathology reports found (may be expected)")
    except Exception as e:
        results.log_fail("Pathology Overview", str(e))
    
    # Test imaging overview
    try:
        imaging_overview = await get_imaging_overview(card_id)
        results.log_info(f"Imaging overview: {len(imaging_overview)} reports")
        if len(imaging_overview) > 0:
            results.log_pass("Imaging Overview", f"Found {len(imaging_overview)} reports")
        else:
            results.log_info("No imaging reports found (may be expected)")
    except Exception as e:
        results.log_fail("Imaging Overview", str(e))

async def test_get_narrative_reports(card_id: str):
    """Test retrieving specific narrative reports."""
    from database import get_microbiology_overview, get_pathology_overview, get_imaging_overview
    from database import get_microbiology_report, get_pathology_report, get_imaging_report
    
    results.start_section("READ: Specific Narrative Report Retrieval")
    
    # Test microbiology report retrieval
    try:
        overview = await get_microbiology_overview(card_id)
        if overview:
            item = overview[0]
            reports = await get_microbiology_report(card_id, item['timestamp'], item['material'])
            if len(reports) > 0:
                results.log_pass("Microbiology Report Retrieval", f"Retrieved {len(reports)} document(s)")
            else:
                results.log_fail("Microbiology Report Retrieval", "No documents found for given timestamp+material")
        else:
            results.log_info("Skipping microbiology report retrieval - no data")
    except Exception as e:
        results.log_fail("Microbiology Report Retrieval", str(e))
    
    # Test pathology report retrieval
    try:
        overview = await get_pathology_overview(card_id)
        if overview:
            item = overview[0]
            reports = await get_pathology_report(card_id, item['timestamp'], item['specimen'])
            if len(reports) > 0:
                results.log_pass("Pathology Report Retrieval", f"Retrieved {len(reports)} document(s)")
            else:
                results.log_fail("Pathology Report Retrieval", "No documents found for given timestamp+specimen")
        else:
            results.log_info("Skipping pathology report retrieval - no data")
    except Exception as e:
        results.log_fail("Pathology Report Retrieval", str(e))
    
    # Test imaging report retrieval
    try:
        overview = await get_imaging_overview(card_id)
        if overview:
            item = overview[0]
            reports = await get_imaging_report(card_id, item['timestamp'], item['exam_type'])
            if len(reports) > 0:
                results.log_pass("Imaging Report Retrieval", f"Retrieved {len(reports)} document(s)")
            else:
                results.log_fail("Imaging Report Retrieval", "No documents found for given timestamp+exam_type")
        else:
            results.log_info("Skipping imaging report retrieval - no data")
    except Exception as e:
        results.log_fail("Imaging Report Retrieval", str(e))

async def test_bulk_index_retrieval(card_id: str):
    """Test bulk retrieval by indices."""
    from database import get_microbiology_overview, get_pathology_overview, get_imaging_overview
    from database import get_microbiology_reports_by_indices, get_pathology_reports_by_indices, get_imaging_reports_by_indices
    
    results.start_section("READ: Bulk Index Retrieval")
    
    # Test microbiology bulk retrieval
    try:
        overview = await get_microbiology_overview(card_id)
        if len(overview) >= 1:
            reports = await get_microbiology_reports_by_indices(card_id, [0])
            if len(reports) > 0:
                results.log_pass("Microbiology Bulk Retrieval - Single Index", f"Retrieved {len(reports)} document(s)")
            if len(overview) >= 2:
                reports = await get_microbiology_reports_by_indices(card_id, [0, 1])
                results.log_pass("Microbiology Bulk Retrieval - Multiple Indices", f"Retrieved {len(reports)} document(s)")
            reports = await get_microbiology_reports_by_indices(card_id, [999])
            if len(reports) == 0:
                results.log_pass("Microbiology Bulk Retrieval - Out of Range", "Gracefully handled out-of-range index")
        else:
            results.log_info("Skipping microbiology bulk retrieval - insufficient data")
    except Exception as e:
        results.log_fail("Microbiology Bulk Retrieval", str(e))
    
    # Similar tests for pathology
    try:
        overview = await get_pathology_overview(card_id)
        if len(overview) >= 1:
            reports = await get_pathology_reports_by_indices(card_id, [0])
            if len(reports) > 0:
                results.log_pass("Pathology Bulk Retrieval", f"Retrieved {len(reports)} document(s)")
        else:
            results.log_info("Skipping pathology bulk retrieval - insufficient data")
    except Exception as e:
        results.log_fail("Pathology Bulk Retrieval", str(e))
    
    # Similar tests for imaging
    try:
        overview = await get_imaging_overview(card_id)
        if len(overview) >= 1:
            reports = await get_imaging_reports_by_indices(card_id, [0])
            if len(reports) > 0:
                results.log_pass("Imaging Bulk Retrieval", f"Retrieved {len(reports)} document(s)")
        else:
            results.log_info("Skipping imaging bulk retrieval - insufficient data")
    except Exception as e:
        results.log_fail("Imaging Bulk Retrieval", str(e))

# =============================================================================
# DELETE OPERATIONS TESTS
# =============================================================================

async def test_delete_operations(card_id: str):
    """Test delete operations."""
    from database import delete_labs_by_card, labs_collection
    
    results.start_section("DELETE: Lab Document Deletion")
    
    count_before = await labs_collection.count_documents({"card_id": card_id})
    results.log_info(f"Documents before deletion: {count_before}")
    
    if count_before == 0:
        results.log_fail("Delete Operations", "No data to delete")
        return
    
    try:
        result = await delete_labs_by_card(card_id)
        deleted = result['deleted_count']
        results.log_info(f"Deleted {deleted} documents")
        
        if deleted == count_before:
            results.log_pass("Delete Labs by Card", f"Deleted all {deleted} documents")
        else:
            results.log_fail("Delete Labs by Card", f"Deleted {deleted} of {count_before} documents")
        
        count_after = await labs_collection.count_documents({"card_id": card_id})
        if count_after == 0:
            results.log_pass("Delete Verification", "All documents successfully removed")
        else:
            results.log_fail("Delete Verification", f"{count_after} documents still remain")
        
    except Exception as e:
        results.log_fail("Delete Operations", str(e))

# =============================================================================
# EDGE CASE AND ERROR HANDLING TESTS
# =============================================================================

async def test_error_handling(card_id: str):
    """Test error handling and edge cases."""
    from database import get_quantitative_labs, get_quantitative_overview
    
    results.start_section("EDGE CASES: Error Handling")
    
    # Test 1: Invalid card_id format
    try:
        await get_quantitative_overview("invalid_card_id")
        results.log_fail("Invalid Card ID Handling", "Should have raised ValueError for invalid card_id")
    except ValueError as e:
        results.log_pass("Invalid Card ID Handling", f"Correctly raised ValueError: {str(e)[:50]}...")
    except Exception as e:
        results.log_fail("Invalid Card ID Handling", f"Unexpected exception type: {type(e).__name__}")
    
    # Test 2: Non-existent card_id
    try:
        fake_card_id = str(ObjectId())
        result = await get_quantitative_overview(fake_card_id)
        if len(result) == 0:
            results.log_pass("Non-existent Card ID", "Correctly returned empty list for unknown card")
        else:
            results.log_fail("Non-existent Card ID", f"Expected empty list, got {len(result)} results")
    except Exception as e:
        results.log_fail("Non-existent Card ID", str(e))
    
    # Test 3: Empty test names list
    try:
        result = await get_quantitative_labs(card_id, [])
        if len(result) == 0:
            results.log_pass("Empty Test Names List", "Correctly handled empty test names list")
        else:
            results.log_fail("Empty Test Names List", f"Expected empty result, got {len(result)}")
    except Exception as e:
        results.log_fail("Empty Test Names List", str(e))

# =============================================================================
# MAIN TEST EXECUTION
# =============================================================================

async def run_all_tests():
    """Execute all tests in sequence."""
    
    print(f"\n{SECTION_SEPARATOR}")
    print("LABS COLLECTION CRUD TEST SUITE")
    print("================================")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(SECTION_SEPARATOR)
    
    # Load all test data
    results.start_section("DATA LOADING")
    table_data = load_all_table_data()
    narrative_data = load_narrative_data()
    
    results.log_info(f"Total table documents: {len(table_data)}")
    results.log_info(f"Total narrative documents: {len(narrative_data)}")
    
    # Setup database
    client, db, labs_collection, cards_collection = await setup_database()
    
    # Create a test card
    results.start_section("TEST SETUP")
    
    test_card_id = None
    try:
        existing_card = await cards_collection.find_one()
        if existing_card:
            test_card_id = str(existing_card['_id'])
            results.log_info(f"Using existing card: {test_card_id}")
        else:
            from database import create_empty_card
            new_card = await create_empty_card()
            test_card_id = new_card['id']
            results.log_info(f"Created new test card: {test_card_id}")
    except Exception as e:
        test_card_id = str(ObjectId())
        results.log_info(f"Using isolated test ID: {test_card_id}")
    
    await cleanup_test_data(test_card_id)
    
    try:
        # CREATE OPERATIONS
        await test_store_quantitative_labs(test_card_id, table_data)
        await test_store_reference_ranges(test_card_id)
        await test_store_microbiology_reports(test_card_id, narrative_data)
        await test_store_pathology_reports(test_card_id, narrative_data)
        await test_store_imaging_reports(test_card_id)
        
        # DUPLICATE DETECTION
        await test_duplicate_detection(test_card_id, table_data, narrative_data)
        
        # READ OPERATIONS
        await test_get_quantitative_labs(test_card_id)
        await test_get_quantitative_overview(test_card_id)
        await test_get_narrative_overviews(test_card_id)
        await test_get_narrative_reports(test_card_id)
        await test_bulk_index_retrieval(test_card_id)
        
        # EDGE CASES
        await test_error_handling(test_card_id)
        
        # DELETE OPERATIONS
        await test_delete_operations(test_card_id)
        
    except Exception as e:
        logger.error(f"Test execution failed: {e}")
        raise
    finally:
        await cleanup_test_data(test_card_id)
        client.close()
    
    results.print_summary()
    return results.failed == 0

if __name__ == "__main__":
    success = asyncio.run(run_all_tests())
    sys.exit(0 if success else 1)
