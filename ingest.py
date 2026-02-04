"""
PDF Ingestion Pipeline

Processes medical PDF documents by:
1. Anonymizing (cropping headers)
2. Detecting and extracting quantitative tables via OCR
3. Removing processed tables from the PDF
4. Processing remaining content (narrative reports) via OCR

All steps are logged for debugging and quality control.
"""

import os
import json
import logging
import tempfile
import fitz  # PyMuPDF
from datetime import datetime
from typing import Optional
from pathlib import Path

import pdf_processor
from ocr_engine import extract_data_from_file


class PDFIngestionPipeline:
    """
    Pipeline for processing medical PDFs through table extraction and narrative processing.
    """
    
    # DPI for rendering PDF pages to images (must match pdf_processor)
    RENDER_DPI = 150
    
    def __init__(self, output_base_dir: str = "output"):
        """
        Initialize the ingestion pipeline.
        
        Args:
            output_base_dir: Base directory for all output files
        """
        self.output_base_dir = output_base_dir
        self.logger = self._setup_logger()
        
    def _setup_logger(self) -> logging.Logger:
        """Setup logger that will be configured per PDF processing run."""
        return logging.getLogger(__name__)
    
    def _configure_logging(self, log_file_path: str):
        """Configure logging to file and console."""
        self.logger.setLevel(logging.DEBUG)
        self.logger.handlers = []  # Clear existing handlers
        
        # File handler with detailed formatting
        file_handler = logging.FileHandler(log_file_path, mode='w')
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter(
            '[%(asctime)s] [%(levelname)s] [%(funcName)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        file_handler.setFormatter(file_formatter)
        self.logger.addHandler(file_handler)
        
        # Console handler for progress
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_formatter = logging.Formatter('%(levelname)s: %(message)s')
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)
    
    def _create_output_directories(self, pdf_name: str) -> dict:
        """
        Create organized output directory structure.
        
        Args:
            pdf_name: Name of the PDF file (without extension)
            
        Returns:
            Dictionary with paths to all output directories
        """
        base_dir = os.path.join(self.output_base_dir, pdf_name)
        
        dirs = {
            'base': base_dir,
            'tables': os.path.join(base_dir, 'tables'),
            'narrative': os.path.join(base_dir, 'narrative'),
            'debug_images': os.path.join(base_dir, 'debug_images'),
            'logs': os.path.join(base_dir, 'logs')
        }
        
        for dir_path in dirs.values():
            os.makedirs(dir_path, exist_ok=True)
            
        return dirs
    
    def _get_category_from_response(self, response_data) -> str:
        """
        Extract category from OCR response.
        
        Args:
            response_data: Parsed JSON response (dict or list)
            
        Returns:
            Category string (e.g., "Quantitative", "Microbiology", etc.)
        """
        if isinstance(response_data, list):
            if len(response_data) > 0:
                return response_data[0].get("category", "Unknown")
            return "Unknown"
        elif isinstance(response_data, dict):
            return response_data.get("category", "Unknown")
        else:
            return "Unknown"
    
    def _crop_table_from_page(self, page: fitz.Page, boundary: dict, 
                              output_path: str, dpi: int = 150) -> str:
        """
        Crop a table region from a PDF page and save as image.
        
        Args:
            page: PyMuPDF page object
            boundary: Dict with 'x', 'y', 'width', 'height' in pixels at specified DPI
            output_path: Path to save the cropped table image
            dpi: Resolution used for boundary detection
            
        Returns:
            Path to the saved image
        """
        # Calculate zoom factor (72 DPI is default in PyMuPDF)
        zoom = dpi / 72
        
        # Convert pixel coordinates to PDF points
        x = boundary['x'] / zoom
        y = boundary['y'] / zoom
        width = boundary['width'] / zoom
        height = boundary['height'] / zoom
        
        # Define crop rectangle in PDF coordinates
        rect = fitz.Rect(x, y, x + width, y + height)
        
        # Render the page at the specified DPI
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, clip=rect)
        
        # Save the cropped image
        pix.save(output_path)
        
        return output_path
    
    def _remove_tables_from_pdf(self, input_path: str, table_regions: list, 
                                 output_path: str):
        """
        Remove table regions from PDF using page reconstruction.
        Tables are removed by their vertical coordinates only (full width strips).
        
        Args:
            input_path: Path to input PDF
            table_regions: List of tuples (page_num, y, height) in PDF points
                          page_num is 1-indexed, y and height are in PDF points
            output_path: Path to save the reconstructed PDF
        """
        self.logger.info(f"Starting table removal from PDF: {input_path}")
        self.logger.debug(f"Table regions to remove: {table_regions}")
        
        doc = fitz.open(input_path)
        new_doc = fitz.open()  # Create new PDF for output
        
        # Group table regions by page
        regions_by_page = {}
        for page_num, y, height in table_regions:
            if page_num not in regions_by_page:
                regions_by_page[page_num] = []
            regions_by_page[page_num].append((y, y + height))
        
        for page_num in range(1, len(doc) + 1):
            page = doc[page_num - 1]  # 0-indexed
            page_width = page.rect.width
            page_height = page.rect.height
            
            if page_num not in regions_by_page:
                # No tables on this page, copy as-is
                new_doc.insert_pdf(doc, from_page=page_num - 1, to_page=page_num - 1)
                self.logger.debug(f"Page {page_num}: Copied as-is (no tables)")
                continue
            
            # Get table regions on this page and sort by y-coordinate
            table_bands = sorted(regions_by_page[page_num], key=lambda x: x[0])
            self.logger.debug(f"Page {page_num}: Table bands to remove: {table_bands}")
            
            # Calculate keep regions (areas between table bands)
            keep_regions = []
            current_y = 0
            
            for band_start, band_end in table_bands:
                if current_y < band_start:
                    # There's content above this table band
                    keep_regions.append((current_y, band_start))
                current_y = band_end
            
            # Add final region after last table
            if current_y < page_height:
                keep_regions.append((current_y, page_height))
            
            self.logger.debug(f"Page {page_num}: Keep regions: {keep_regions}")
            
            # Create new pages from keep regions
            for idx, (keep_start, keep_end) in enumerate(keep_regions):
                keep_height = keep_end - keep_start
                
                if keep_height < 10:  # Skip very small regions
                    self.logger.debug(f"Page {page_num}: Skipping small region {keep_start}-{keep_end}")
                    continue
                
                # Create new page with the height of the keep region
                new_page = new_doc.new_page(width=page_width, height=keep_height)
                
                # Define source rectangle from original page
                source_rect = fitz.Rect(0, keep_start, page_width, keep_end)
                
                # Copy content from source region to new page
                # Using show_pdf_page to copy the content
                new_page.show_pdf_page(
                    new_page.rect,  # Destination rectangle (full new page)
                    doc,            # Source document
                    page_num - 1,   # Source page number (0-indexed)
                    clip=source_rect  # Source clip rectangle
                )
                
                self.logger.debug(f"Page {page_num}: Created new page from y={keep_start} to y={keep_end}")
            
            self.logger.info(f"Page {page_num}: Removed {len(table_bands)} table bands, created {len(keep_regions)} page segments")
        
        # Save the reconstructed PDF
        new_doc.save(output_path, garbage=4, clean=True)
        new_doc.close()
        doc.close()
        
        self.logger.info(f"Table removal complete. Saved to: {output_path}")
    
    def process_pdf(self, pdf_path: str) -> dict:
        """
        Process a medical PDF through the complete ingestion pipeline.
        
        Args:
            pdf_path: Path to the input PDF file
            
        Returns:
            Dictionary with processing results:
            - output_dir: Path to output directory
            - tables_processed: List of table JSON files created
            - narrative_file: Path to narrative JSON file
            - log_file: Path to log file
            - stopped_at_category: Category that caused table processing to stop (if any)
            - anonymized_pdf: Path to anonymized PDF
            - cleaned_pdf: Path to cleaned PDF (tables removed)
        """
        # Validate input
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"Input PDF not found: {pdf_path}")
        
        pdf_name = Path(pdf_path).stem
        
        # Create output directories
        dirs = self._create_output_directories(pdf_name)
        
        # Configure logging
        log_file = os.path.join(dirs['logs'], 'pipeline.log')
        self._configure_logging(log_file)
        
        self.logger.info(f"Starting PDF ingestion pipeline for: {pdf_path}")
        self.logger.info(f"Output directory: {dirs['base']}")
        
        result = {
            'output_dir': dirs['base'],
            'tables_processed': [],
            'narrative_file': None,
            'log_file': log_file,
            'stopped_at_category': None,
            'anonymized_pdf': None,
            'cleaned_pdf': None
        }
        
        # Step 1: Header Cropping (Anonymization)
        self.logger.info("=" * 60)
        self.logger.info("STEP 1: Header Cropping (Anonymization)")
        self.logger.info("=" * 60)
        
        preprocessor = pdf_processor.PDFPreprocessor(output_dir=dirs['base'])
        anonymized_path = os.path.join(dirs['base'], 'anonymized.pdf')
        
        # Use crop_header but we'll save to our specific location
        temp_anonymized = preprocessor.crop_header(pdf_path)
        # Move to our desired location
        os.rename(temp_anonymized, anonymized_path)
        
        result['anonymized_pdf'] = anonymized_path
        self.logger.info(f"Anonymized PDF saved to: {anonymized_path}")
        
        # Step 2: Table Boundary Detection
        self.logger.info("=" * 60)
        self.logger.info("STEP 2: Table Boundary Detection")
        self.logger.info("=" * 60)
        
        table_boundaries = preprocessor.get_table_boundaries(anonymized_path, dpi=self.RENDER_DPI)
        self.logger.info(f"Detected table boundaries for {len(table_boundaries)} pages")
        
        for page_num, boundary in table_boundaries.items():
            if boundary:
                self.logger.info(f"  Page {page_num}: Table at x={boundary['x']}, y={boundary['y']}, "
                               f"w={boundary['width']}, h={boundary['height']}")
            else:
                self.logger.info(f"  Page {page_num}: No table detected")
        
        # Step 3 & 4: Table Processing Loop
        self.logger.info("=" * 60)
        self.logger.info("STEP 3 & 4: Table Processing with OCR")
        self.logger.info("=" * 60)
        
        doc = fitz.open(anonymized_path)
        zoom = self.RENDER_DPI / 72
        table_count = 0
        processed_table_regions = []  # List of (page_num, y, height) in PDF points
        stop_processing = False
        
        # Process pages in order
        for page_num in sorted(table_boundaries.keys()):
            if stop_processing:
                break
            
            boundary = table_boundaries[page_num]
            if not boundary:
                self.logger.info(f"Page {page_num}: No table boundary, skipping")
                continue
            
            self.logger.info(f"Page {page_num}: Processing table")
            
            # Get the page
            page = doc[page_num - 1]  # 0-indexed
            
            # Crop table to image
            table_count += 1
            table_image_path = os.path.join(dirs['debug_images'], f'page_{page_num}_table_{table_count}.png')
            
            try:
                self._crop_table_from_page(page, boundary, table_image_path, dpi=self.RENDER_DPI)
                self.logger.info(f"  Cropped table saved to: {table_image_path}")
            except Exception as e:
                self.logger.error(f"  Failed to crop table from page {page_num}: {e}")
                continue
            
            # OCR the table
            self.logger.info(f"  Sending to OCR (is_table=True)")
            self.logger.debug(f"  OCR Input: File path = {table_image_path}")
            
            try:
                ocr_response = extract_data_from_file(table_image_path, is_table=True)
                self.logger.debug(f"  OCR Raw Response: {ocr_response}")
            except Exception as e:
                self.logger.error(f"  OCR failed for page {page_num}: {e}")
                continue
            
            # Parse and check category
            try:
                response_data = json.loads(ocr_response)
            except json.JSONDecodeError as e:
                self.logger.error(f"  Failed to parse OCR response as JSON: {e}")
                self.logger.debug(f"  Raw response: {ocr_response}")
                continue
            
            category = self._get_category_from_response(response_data)
            self.logger.info(f"  OCR returned category: {category}")
            
            # Check if we should stop processing tables
            if category in ["Microbiology", "Pathology", "Imaging"]:
                self.logger.info(f"  Detected '{category}' report - stopping table processing")
                self.logger.info(f"  Remaining table boundaries are false positives")
                result['stopped_at_category'] = category
                stop_processing = True
                break
            
            elif category == "Quantitative":
                # Save the table JSON
                table_json_path = os.path.join(dirs['tables'], f'table_p{page_num}_t{table_count}.json')
                with open(table_json_path, 'w', encoding='utf-8') as f:
                    json.dump(response_data, f, indent=2, ensure_ascii=False)
                
                result['tables_processed'].append(table_json_path)
                self.logger.info(f"  Saved quantitative table to: {table_json_path}")
                
                # Record this table region for removal (convert to PDF points)
                y_pts = boundary['y'] / zoom
                height_pts = boundary['height'] / zoom
                processed_table_regions.append((page_num, y_pts, height_pts))
                self.logger.debug(f"  Recorded table region for removal: page={page_num}, y={y_pts}, height={height_pts}")
            
            else:
                # Unexpected category
                self.logger.warning(f"  Unexpected category returned: {category}")
                self.logger.debug(f"  Response data: {response_data}")
        
        doc.close()
        
        self.logger.info(f"Table processing complete. Processed {len(result['tables_processed'])} quantitative tables.")
        
        # Step 5 & 6: Remove processed tables from PDF
        self.logger.info("=" * 60)
        self.logger.info("STEP 5 & 6: Removing Processed Tables from PDF")
        self.logger.info("=" * 60)
        
        cleaned_pdf_path = os.path.join(dirs['narrative'], 'cleaned_for_narrative.pdf')
        
        if processed_table_regions:
            self.logger.info(f"Removing {len(processed_table_regions)} table regions from PDF")
            self._remove_tables_from_pdf(anonymized_path, processed_table_regions, cleaned_pdf_path)
            result['cleaned_pdf'] = cleaned_pdf_path
        else:
            self.logger.info("No tables to remove, using anonymized PDF for narrative processing")
            cleaned_pdf_path = anonymized_path
            result['cleaned_pdf'] = anonymized_path
        
        # Step 7: Second-Pass OCR (Narrative)
        self.logger.info("=" * 60)
        self.logger.info("STEP 7: Second-Pass OCR (Narrative)")
        self.logger.info("=" * 60)
        
        self.logger.info(f"Processing cleaned PDF: {cleaned_pdf_path}")
        self.logger.info(f"Sending to OCR (is_table=False)")
        self.logger.debug(f"OCR Input: File path = {cleaned_pdf_path}")
        
        try:
            narrative_response = extract_data_from_file(cleaned_pdf_path, is_table=False)
            self.logger.debug(f"OCR Raw Response: {narrative_response}")
        except Exception as e:
            self.logger.error(f"Narrative OCR failed: {e}")
            narrative_response = None
        
        if narrative_response:
            # Save narrative JSON
            narrative_json_path = os.path.join(dirs['narrative'], 'narrative_final.json')
            
            try:
                narrative_data = json.loads(narrative_response)
                with open(narrative_json_path, 'w', encoding='utf-8') as f:
                    json.dump(narrative_data, f, indent=2, ensure_ascii=False)
                
                result['narrative_file'] = narrative_json_path
                self.logger.info(f"Saved narrative data to: {narrative_json_path}")
            except json.JSONDecodeError as e:
                self.logger.error(f"Failed to parse narrative response as JSON: {e}")
                # Save raw response anyway
                narrative_raw_path = os.path.join(dirs['narrative'], 'narrative_final_raw.txt')
                with open(narrative_raw_path, 'w', encoding='utf-8') as f:
                    f.write(narrative_response)
                self.logger.info(f"Saved raw narrative response to: {narrative_raw_path}")
        
        # Step 8: Finalization
        self.logger.info("=" * 60)
        self.logger.info("STEP 8: Pipeline Complete")
        self.logger.info("=" * 60)
        
        self.logger.info(f"Summary:")
        self.logger.info(f"  - Tables processed: {len(result['tables_processed'])}")
        self.logger.info(f"  - Stopped at category: {result['stopped_at_category'] or 'N/A (all pages processed)'}")
        self.logger.info(f"  - Narrative file: {result['narrative_file'] or 'N/A'}")
        self.logger.info(f"  - Log file: {result['log_file']}")
        self.logger.info(f"  - Output directory: {result['output_dir']}")
        
        return result


def process_pdf(pdf_path: str, output_base_dir: str = "output") -> dict:
    """
    Convenience function to process a PDF through the ingestion pipeline.
    
    Args:
        pdf_path: Path to the input PDF file
        output_base_dir: Base directory for all output files
        
    Returns:
        Dictionary with processing results
    """
    pipeline = PDFIngestionPipeline(output_base_dir=output_base_dir)
    return pipeline.process_pdf(pdf_path)


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) < 2:
        print("Usage: python ingest.py <path_to_pdf>")
        print("       python ingest.py <path_to_pdf> <output_base_dir>")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    output_dir = sys.argv[2] if len(sys.argv) > 2 else "output"
    
    try:
        result = process_pdf(pdf_path, output_dir)
        print("\n" + "=" * 60)
        print("Processing Complete!")
        print("=" * 60)
        print(f"Output directory: {result['output_dir']}")
        print(f"Tables processed: {len(result['tables_processed'])}")
        print(f"Narrative file: {result['narrative_file']}")
        print(f"Log file: {result['log_file']}")
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)
