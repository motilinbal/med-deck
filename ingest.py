"""
PDF Ingestion Pipeline

Processes medical PDF files through a multi-phase OCR extraction workflow:
1. Phase 1: Table detection and OCR
2. Phase 2: Reference data extraction from table-stripped PDF
3. Phase 3: Narrative section extraction

Usage:
    python ingest.py <path_to_pdf>
"""

import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any

from pdf_processor import PDFPreprocessor
from ocr_engine import extract_data_from_file


class PipelineLogger:
    """
    Handles detailed logging of the entire pipeline execution.
    Logs to both console and file with structured sections.
    """
    
    def __init__(self, log_path: str):
        """
        Initialize the pipeline logger.
        
        Args:
            log_path: Path to the log file.
        """
        self.log_path = log_path
        self.logger = logging.getLogger('ingest_pipeline')
        self.logger.setLevel(logging.DEBUG)
        
        # Clear any existing handlers
        self.logger.handlers = []
        
        # File handler - detailed logs
        file_handler = logging.FileHandler(log_path, mode='w')
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_formatter)
        self.logger.addHandler(file_handler)
        
        # Console handler - info and above
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter('%(message)s')
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)
        
        # Write header
        self._write_header()
    
    def _write_header(self):
        """Write the log file header."""
        header = """
================================================================================
PDF INGESTION PIPELINE
================================================================================
"""
        self.logger.info(header)
    
    def log_pdf_info(self, pdf_path: str, output_dir: str):
        """Log PDF file information."""
        self.logger.info(f"PDF File: {pdf_path}")
        self.logger.info(f"Output Directory: {output_dir}")
        self.logger.info(f"Start Time: {datetime.now().isoformat()}")
        self.logger.info("=" * 80)
    
    def log_phase_start(self, phase_num: int, phase_name: str):
        """Log the start of a pipeline phase."""
        self.logger.info("")
        self.logger.info(f"[PHASE {phase_num}: {phase_name.upper()}]")
        self.logger.info("-" * 80)
    
    def log_phase_end(self, phase_num: int, summary: str = ""):
        """Log the end of a pipeline phase."""
        self.logger.info(f"[PHASE {phase_num} COMPLETE]")
        if summary:
            self.logger.info(summary)
        self.logger.info("")
    
    def log_step(self, step_name: str):
        """Log a processing step."""
        self.logger.info(f"[STEP] {step_name}")
    
    def log_result(self, result: str):
        """Log a result."""
        self.logger.info(f"[RESULT] {result}")
    
    def log_file_saved(self, file_path: str, file_type: str = ""):
        """Log a file being saved."""
        type_str = f" ({file_type})" if file_type else ""
        self.logger.info(f"[FILE SAVED{type_str}] {file_path}")
    
    def log_table_detection(self, page_num: int, boundary: Optional[Dict] = None):
        """Log table detection results."""
        if boundary:
            self.logger.info(f"[TABLE DETECTED] Page {page_num}: "
                           f"x={boundary['x']}, y={boundary['y']}, "
                           f"w={boundary['width']}, h={boundary['height']}")
        else:
            self.logger.info(f"[TABLE DETECTED] Page {page_num}: No table found")
    
    def log_ocr_request(self, file_path: str, ocr_type: str, 
                        page_num: Optional[int] = None):
        """Log OCR request details."""
        page_str = f", Page: {page_num}" if page_num else ""
        self.logger.info(f"[OCR REQUEST] Type: {ocr_type}{page_str}, File: {file_path}")
    
    def log_ocr_response(self, response: str, max_chars: int = 2000):
        """Log OCR response with full payload."""
        # Log full response to file, truncated to console
        self.logger.debug(f"[OCR RESPONSE FULL] {response}")
        
        # For console/info, show truncated version
        display_response = response[:max_chars]
        if len(response) > max_chars:
            display_response += f"... ({len(response) - max_chars} more chars)"
        self.logger.info(f"[OCR RESPONSE] {display_response}")
    
    def log_decision(self, decision: str, reason: str):
        """Log decision points with reasoning."""
        self.logger.info(f"[DECISION] {decision}")
        self.logger.info(f"[REASON] {reason}")
    
    def log_warning(self, message: str):
        """Log a warning message."""
        self.logger.warning(f"[WARNING] {message}")
    
    def log_error(self, message: str):
        """Log an error message."""
        self.logger.error(f"[ERROR] {message}")
    
    def log_completion(self, duration: float, output_summary: Dict[str, Any]):
        """Log pipeline completion."""
        self.logger.info("")
        self.logger.info("=" * 80)
        self.logger.info("PIPELINE COMPLETE")
        self.logger.info(f"Total Duration: {duration:.2f} seconds")
        self.logger.info("Output Files:")
        for key, value in output_summary.items():
            self.logger.info(f"  {key}: {value}")
        self.logger.info("=" * 80)


class PipelineOrchestrator:
    """
    Main orchestrator for the PDF ingestion pipeline.
    """
    
    # Categories that indicate false positives or section boundaries
    STOP_CATEGORIES = {"Microbiology", "Pathology", "Imaging"}
    
    def __init__(self, output_base_dir: str = "output"):
        """
        Initialize the pipeline orchestrator.
        
        Args:
            output_base_dir: Base directory for all output files.
        """
        self.output_base_dir = output_base_dir
        self.pdf_processor = PDFPreprocessor()
        self.logger = None
        self.run_dir = None
        self.pdf_name = None
    
    def _setup_run_directory(self, pdf_path: str) -> str:
        """
        Create the organized output directory structure for this run.
        
        Args:
            pdf_path: Path to the input PDF.
        
        Returns:
            Path to the run directory.
        """
        # Create timestamped directory name
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        pdf_basename = Path(pdf_path).stem
        self.pdf_name = pdf_basename
        
        run_dir = os.path.join(self.output_base_dir, f"{timestamp}_{pdf_basename}")
        self.run_dir = run_dir
        
        # Create directory structure
        dirs = [
            run_dir,
            os.path.join(run_dir, "tables", "images"),
            os.path.join(run_dir, "tables", "json"),
            os.path.join(run_dir, "refs", "json"),
            os.path.join(run_dir, "narrative", "pdf"),
            os.path.join(run_dir, "narrative", "json"),
            os.path.join(run_dir, "pdfs"),
        ]
        
        for d in dirs:
            os.makedirs(d, exist_ok=True)
        
        return run_dir
    
    def _is_stop_category(self, ocr_response: str) -> Tuple[bool, Optional[str]]:
        """
        Check if OCR response contains a stop category marker.
        
        Args:
            ocr_response: The JSON response from OCR.
        
        Returns:
            Tuple of (is_stop, category_name).
        """
        try:
            data = json.loads(ocr_response)
            
            # Handle both single object and array responses
            if isinstance(data, list) and len(data) > 0:
                data = data[0]
            
            if isinstance(data, dict) and "category" in data:
                category = data["category"]
                if category in self.STOP_CATEGORIES:
                    return True, category
        except json.JSONDecodeError:
            pass
        
        return False, None
    
    def _save_json(self, data: str, output_path: str):
        """
        Save OCR response to JSON file.
        
        Args:
            data: JSON string from OCR.
            output_path: Path to save the file.
        """
        # Ensure directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        # Parse and pretty-print JSON
        try:
            parsed = json.loads(data)
            with open(output_path, 'w', encoding='utf-8') as f:
                json.dump(parsed, f, indent=2, ensure_ascii=False)
        except json.JSONDecodeError:
            # If not valid JSON, save as-is
            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(data)
    
    def _extract_non_category_data(self, ocr_response: str) -> Optional[str]:
        """
        Extract data from a response that contains category markers.
        If the response is ONLY a category marker, return None.
        If the response contains other data, return that data as JSON string.
        
        Args:
            ocr_response: The OCR response JSON string.
        
        Returns:
            JSON string of non-category data, or None if only category marker.
        """
        try:
            data = json.loads(ocr_response)
            
            # If it's an array, filter out category-only entries
            if isinstance(data, list):
                filtered = []
                for item in data:
                    if isinstance(item, dict):
                        # Check if item has only category field
                        if len(item) == 1 and "category" in item:
                            continue
                        filtered.append(item)
                
                if filtered:
                    return json.dumps(filtered)
                return None
            
            # If it's a single object with only category
            if isinstance(data, dict):
                if len(data) == 1 and "category" in data:
                    return None
                return json.dumps(data)
                
        except json.JSONDecodeError:
            pass
        
        return None
    
    def _phase1_table_extraction(self, pdf_path: str) -> Tuple[Dict[int, List[Dict]], List[Dict]]:
        """
        Phase 1: Detect and OCR tables.
        
        Args:
            pdf_path: Path to the anonymized PDF.
        
        Returns:
            Tuple of (true_tables_coords, table_results).
            true_tables_coords: {page_num: [{'y': y, 'height': h}, ...]}
            table_results: List of dicts with table extraction info.
        """
        self.logger.log_phase_start(1, "Table Detection and OCR")
        
        # Detect table boundaries
        self.logger.log_step("Detecting table boundaries")
        table_boundaries = self.pdf_processor.get_table_boundaries(pdf_path)
        
        true_tables_coords: Dict[int, List[Dict]] = {}
        table_results = []
        table_idx = 0
        stop_encountered = False
        stop_page = None
        
        # Iterate through pages in order
        sorted_pages = sorted(table_boundaries.keys())
        
        for page_num in sorted_pages:
            boundary = table_boundaries[page_num]
            
            if boundary is None:
                self.logger.log_table_detection(page_num, None)
                continue
            
            self.logger.log_table_detection(page_num, boundary)
            
            # Extract table image
            table_idx += 1
            image_filename = f"{self.pdf_name}_page{page_num}_table{table_idx}.png"
            image_path = os.path.join(self.run_dir, "tables", "images", image_filename)
            
            self.logger.log_step(f"Extracting table image for Page {page_num}, Table {table_idx}")
            self.pdf_processor.extract_table_image(
                pdf_path, page_num,
                boundary['x'], boundary['y'],
                boundary['width'], boundary['height'],
                image_path
            )
            self.logger.log_file_saved(image_path, "Table Image")
            
            # OCR the table
            self.logger.log_ocr_request(image_path, "table", page_num)
            ocr_response = extract_data_from_file(image_path, type='table')
            self.logger.log_ocr_response(ocr_response)
            
            # Check for false positive
            is_stop, category = self._is_stop_category(ocr_response)
            
            if is_stop:
                self.logger.log_decision(
                    f"Stop processing tables",
                    f"Detected category '{category}' at page {page_num}. "
                    f"All subsequent tables are false positives."
                )
                stop_encountered = True
                stop_page = page_num
                break
            
            # Save valid table result
            json_filename = f"{self.pdf_name}_page{page_num}_table{table_idx}.json"
            json_path = os.path.join(self.run_dir, "tables", "json", json_filename)
            self._save_json(ocr_response, json_path)
            self.logger.log_file_saved(json_path, "Table JSON")
            
            # Collect coordinates for table removal
            if page_num not in true_tables_coords:
                true_tables_coords[page_num] = []
            true_tables_coords[page_num].append({
                'y': boundary['y'],
                'height': boundary['height']
            })
            
            table_results.append({
                'page': page_num,
                'table_idx': table_idx,
                'image_path': image_path,
                'json_path': json_path,
                'boundary': boundary
            })
        
        summary = f"Tables processed: {len(table_results)}"
        if stop_encountered:
            summary += f", Stop encountered at page {stop_page}"
        
        self.logger.log_phase_end(1, summary)
        
        return true_tables_coords, table_results
    
    def _phase2_reference_extraction(self, pdf_path: str, 
                                     true_tables: Dict[int, List[Dict]]) -> Tuple[int, List[Dict]]:
        """
        Phase 2: Extract reference data from table-stripped PDF.
        
        Args:
            pdf_path: Path to the anonymized PDF.
            true_tables: Dictionary of true table coordinates by page.
        
        Returns:
            Tuple of (last_ref_page, ref_results).
            last_ref_page: The last page processed before narrative section.
            ref_results: List of reference extraction results.
        """
        self.logger.log_phase_start(2, "Reference Data Extraction")
        
        # Remove tables from PDF
        no_tables_pdf = os.path.join(self.run_dir, "pdfs", "02_no_tables.pdf")
        
        if true_tables:
            self.logger.log_step("Removing tables vertically from PDF")
            self.pdf_processor.remove_tables_vertically(pdf_path, true_tables, no_tables_pdf, dpi=300)
            self.logger.log_file_saved(no_tables_pdf, "PDF without tables")
        else:
            self.logger.log_warning("No true tables found, using original PDF")
            no_tables_pdf = pdf_path
        
        # Get page count
        import fitz
        doc = fitz.open(no_tables_pdf)
        total_pages = len(doc)
        doc.close()
        
        ref_results = []
        last_ref_page = total_pages  # Default to end if no stop category found
        
        # Process each page
        for page_num in range(1, total_pages + 1):
            self.logger.log_step(f"Processing Page {page_num}")
            
            # Convert page to image
            image_filename = f"{self.pdf_name}_page{page_num}_ref.png"
            image_path = os.path.join(self.run_dir, "refs", image_filename)
            self.pdf_processor.page_to_image(no_tables_pdf, page_num, image_path)
            
            # OCR the page
            self.logger.log_ocr_request(image_path, "ref", page_num)
            ocr_response = extract_data_from_file(image_path, type='ref')
            self.logger.log_ocr_response(ocr_response)
            
            # Check for stop category
            is_stop, category = self._is_stop_category(ocr_response)
            
            if is_stop:
                self.logger.log_decision(
                    f"Stop processing reference pages",
                    f"Detected category '{category}' at page {page_num}. "
                    f"This is the start of the narrative section."
                )
                
                # Extract any non-category data
                non_category_data = self._extract_non_category_data(ocr_response)
                
                if non_category_data:
                    json_filename = f"{self.pdf_name}_page{page_num}_ref_partial.json"
                    json_path = os.path.join(self.run_dir, "refs", "json", json_filename)
                    self._save_json(non_category_data, json_path)
                    self.logger.log_file_saved(json_path, "Partial Reference JSON")
                    
                    ref_results.append({
                        'page': page_num,
                        'json_path': json_path,
                        'partial': True,
                        'category': category
                    })
                
                # The narrative section starts from this page (the one with stop signal)
                # So we include this page in the narrative PDF
                last_ref_page = page_num
                break
            
            # Save full reference result
            json_filename = f"{self.pdf_name}_page{page_num}_ref.json"
            json_path = os.path.join(self.run_dir, "refs", "json", json_filename)
            self._save_json(ocr_response, json_path)
            self.logger.log_file_saved(json_path, "Reference JSON")
            
            ref_results.append({
                'page': page_num,
                'json_path': json_path,
                'partial': False
            })
            
            last_ref_page = page_num
        
        summary = f"Reference pages processed: {len(ref_results)}, Last page: {last_ref_page}"
        self.logger.log_phase_end(2, summary)
        
        return last_ref_page, ref_results
    
    def _phase3_narrative_extraction(self, no_tables_pdf: str,
                                     start_page: int) -> Dict:
        """
        Phase 3: Extract narrative from remaining pages.
        
        Args:
            no_tables_pdf: Path to the PDF without tables.
            start_page: First page of narrative section (1-indexed).
        
        Returns:
            Dictionary with narrative extraction results.
        """
        self.logger.log_phase_start(3, "Narrative Extraction")
        
        # Get total pages
        import fitz
        doc = fitz.open(no_tables_pdf)
        total_pages = len(doc)
        doc.close()
        
        if start_page > total_pages:
            self.logger.log_warning("No narrative pages found")
            self.logger.log_phase_end(3, "No narrative section")
            return {}
        
        # Extract narrative pages to new PDF
        narrative_pdf = os.path.join(self.run_dir, "narrative", "pdf", "narrative.pdf")
        
        self.logger.log_step(f"Extracting narrative pages {start_page} to {total_pages}")
        self.pdf_processor.extract_page_range(no_tables_pdf, start_page, narrative_pdf)
        self.logger.log_file_saved(narrative_pdf, "Narrative PDF")
        
        # OCR the narrative PDF
        self.logger.log_ocr_request(narrative_pdf, "narrative")
        ocr_response = extract_data_from_file(narrative_pdf, type='narrative')
        self.logger.log_ocr_response(ocr_response)
        
        # Save narrative result
        json_path = os.path.join(self.run_dir, "narrative", "json", f"{self.pdf_name}_narrative.json")
        self._save_json(ocr_response, json_path)
        self.logger.log_file_saved(json_path, "Narrative JSON")
        
        summary = f"Narrative pages: {total_pages - start_page + 1}"
        self.logger.log_phase_end(3, summary)
        
        return {
            'pdf_path': narrative_pdf,
            'json_path': json_path,
            'start_page': start_page,
            'end_page': total_pages
        }
    
    def process_pdf(self, pdf_path: str) -> Dict[str, Any]:
        """
        Main entry point - processes a PDF through all phases.
        
        Args:
            pdf_path: Path to the input PDF file.
        
        Returns:
            Dictionary with paths to all generated files and summary.
        """
        import time
        start_time = time.time()
        
        # Validate input
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"Input PDF not found: {pdf_path}")
        
        # Setup output directory
        run_dir = self._setup_run_directory(pdf_path)
        
        # Initialize logger
        log_path = os.path.join(run_dir, "pipeline.log")
        self.logger = PipelineLogger(log_path)
        self.logger.log_pdf_info(pdf_path, run_dir)
        
        try:
            # Phase 0: Header cropping (anonymization)
            self.logger.log_step("Cropping header from PDF")
            import shutil
            # First crop to a temp location, then move to final location
            temp_cropped = self.pdf_processor.crop_header(pdf_path)
            anonymized_pdf = os.path.join(run_dir, "pdfs", "01_anonymized.pdf")
            shutil.move(temp_cropped, anonymized_pdf)
            self.logger.log_file_saved(anonymized_pdf, "Anonymized PDF")
            
            # Phase 1: Table extraction
            true_tables, table_results = self._phase1_table_extraction(anonymized_pdf)
            
            # Phase 2: Reference extraction
            last_ref_page, ref_results = self._phase2_reference_extraction(
                anonymized_pdf, true_tables
            )
            
            # Phase 3: Narrative extraction
            no_tables_pdf = os.path.join(run_dir, "pdfs", "02_no_tables.pdf")
            if not os.path.exists(no_tables_pdf):
                no_tables_pdf = anonymized_pdf
            
            narrative_result = self._phase3_narrative_extraction(
                no_tables_pdf, last_ref_page
            )
            
            # Calculate duration
            duration = time.time() - start_time
            
            # Prepare output summary
            output_summary = {
                'run_directory': run_dir,
                'log_file': log_path,
                'anonymized_pdf': anonymized_pdf,
                'tables_count': len(table_results),
                'reference_pages_count': len(ref_results),
                'has_narrative': bool(narrative_result)
            }
            
            if narrative_result:
                output_summary['narrative_pdf'] = narrative_result['pdf_path']
                output_summary['narrative_json'] = narrative_result['json_path']
            
            self.logger.log_completion(duration, output_summary)
            
            return output_summary
            
        except Exception as e:
            self.logger.log_error(f"Pipeline failed: {str(e)}")
            raise


def main():
    """CLI entry point."""
    if len(sys.argv) < 2:
        print("Usage: python ingest.py <path_to_pdf>")
        print("       python ingest.py <path_to_pdf> [output_base_dir]")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    output_base_dir = sys.argv[2] if len(sys.argv) > 2 else "output"
    
    orchestrator = PipelineOrchestrator(output_base_dir)
    
    try:
        result = orchestrator.process_pdf(pdf_path)
        print("\n" + "=" * 80)
        print("PIPELINE COMPLETED SUCCESSFULLY")
        print("=" * 80)
        print(f"Output Directory: {result['run_directory']}")
        print(f"Tables Extracted: {result['tables_count']}")
        print(f"Reference Pages: {result['reference_pages_count']}")
        print(f"Narrative Section: {'Yes' if result['has_narrative'] else 'No'}")
        print(f"Log File: {result['log_file']}")
    except Exception as e:
        print(f"\nPipeline failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
