"""
PDF Preprocessing Module
Handles physical manipulation of PDF files using PyMuPDF (fitz).
Used for anonymization (cropping headers) before AI ingestion.
"""

import os
import tempfile
from typing import List, Dict, Optional
import fitz  # PyMuPDF

import table_manager

class PDFPreprocessor:
    """
    Handles PDF manipulation to prepare files for AI ingestion.
    """
    
    # 1 inch = 2.54 cm = 72 points
    CM_TO_PTS = 72 / 2.54
    DEFAULT_CROP_HEIGHT_CM = 4.4

    def __init__(self, output_dir: str = "processed_docs"):
        """
        Initialize the preprocessor.
        
        Args:
            output_dir: Directory to save processed files.
        """
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)

    def crop_header(self, input_path: str, crop_amount_cm: float = DEFAULT_CROP_HEIGHT_CM) -> str:
        """
        Crops the top X cm from every page of the PDF to anonymize headers.
        
        Args:
            input_path: Path to the source PDF.
            crop_amount_cm: Amount to cut from the top in centimeters.
            
        Returns:
            Path to the processed PDF file.
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

        # Calculate crop amount in PDF points
        crop_amount_pts = crop_amount_cm * self.CM_TO_PTS
        
        doc = fitz.open(input_path)
        print(f"Processing {os.path.basename(input_path)} with PyMuPDF...")
        print(f"Target crop: {crop_amount_cm}cm ({crop_amount_pts:.2f} pts)")

        for page_num, page in enumerate(doc):
            # PyMuPDF Rect is (x0, y0, x1, y1)
            # (0,0) is usually Top-Left.
            # To crop the top, we simply increase y0.
            
            original_rect = page.rect
            
            # Safety check: Ensure we don't crop the entire page
            if crop_amount_pts >= original_rect.height:
                print(f"Warning [Page {page_num+1}]: Crop amount > page height. Skipping.")
                continue

            # Define new rectangle:
            # x0 remains same
            # y0 moves DOWN by crop_amount (starts lower)
            # x1 remains same
            # y1 remains same (bottom)
            new_rect = fitz.Rect(
                original_rect.x0,
                original_rect.y0 + crop_amount_pts,
                original_rect.x1,
                original_rect.y1
            )
            
            # Set the CropBox to this new rectangle.
            # This hides the content above y0 + crop_amount
            page.set_cropbox(new_rect)

        # Generate output path
        base_name = os.path.basename(input_path)
        name, ext = os.path.splitext(base_name)
        output_filename = f"{name}_anonymized{ext}"
        output_path = os.path.join(self.output_dir, output_filename)

        # Garbage=4 defragments the file, clean=True ensures validity
        doc.save(output_path, garbage=4, clean=True)
        doc.close()
            
        print(f"✓ Anonymized PDF saved to: {output_path}")
        return output_path

    def get_table_boundaries(self, input_path: str, dpi: int = 300) -> dict:
        """
        Detects table boundaries on each page of the PDF.
        
        Converts each page to an image and uses table_manager to detect
        table boundaries. Returns a dictionary with page numbers as keys
        and table boundary coordinates as values.
        
        Args:
            input_path: Path to the source PDF.
            dpi: Resolution for rendering PDF pages to images (default: 300).
                 Higher DPI = better accuracy but slower processing.
                 300 DPI is recommended for reliable table detection.
        
        Returns:
            Dictionary with structure:
            {
                page_number: {
                    'x': int,      # x-coordinate of top-left corner
                    'y': int,      # y-coordinate of top-left corner
                    'width': int,  # width of table
                    'height': int  # height of table
                } or None if no table detected
            }
            Page numbers are 1-indexed.
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")

        doc = fitz.open(input_path)
        print(f"Detecting table boundaries in: {os.path.basename(input_path)}")
        
        # Calculate zoom factor based on DPI (72 DPI is default in PyMuPDF)
        zoom = dpi / 72
        mat = fitz.Matrix(zoom, zoom)
        
        table_boundaries = {}
        
        # Create temporary directory for page images
        with tempfile.TemporaryDirectory() as temp_dir:
            for page_num in range(len(doc)):
                page = doc[page_num]
                page_number = page_num + 1  # 1-indexed
                
                # Render page to image
                pix = page.get_pixmap(matrix=mat, alpha=False)
                temp_image_path = os.path.join(temp_dir, f"page_{page_number}.png")
                pix.save(temp_image_path)
                
                # Detect table boundaries using table_manager
                boundary = table_manager.get_table_boundaries(temp_image_path)
                
                if boundary:
                    x, y, w, h = boundary
                    table_boundaries[page_number] = {
                        'x': x,
                        'y': y,
                        'width': w,
                        'height': h
                    }
                    print(f"  Page {page_number}: Table found at x={x}, y={y}, w={w}, h={h}")
                else:
                    table_boundaries[page_number] = None
                    print(f"  Page {page_number}: No table detected")
        
        doc.close()
        print(f"✓ Table boundary detection complete for {len(table_boundaries)} pages")
        
        return table_boundaries

    def extract_table_image(self, input_path: str, page_num: int,
                            x: int, y: int, width: int, height: int,
                            output_path: str, dpi: int = 300) -> str:
        """
        Extracts a specific region from a PDF page and saves as PNG.
        
        Converts pixel coordinates (from table detection) to PDF points,
        renders the region at specified DPI, and saves as PNG.
        
        Args:
            input_path: Path to the PDF file.
            page_num: 1-indexed page number.
            x: X-coordinate of top-left corner in pixels.
            y: Y-coordinate of top-left corner in pixels.
            width: Width of the region in pixels.
            height: Height of the region in pixels.
            output_path: Path where the PNG will be saved.
            dpi: Resolution for rendering (default: 300).
        
        Returns:
            Path to the saved PNG file.
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")
        
        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        doc = fitz.open(input_path)
        
        try:
            page = doc[page_num - 1]  # Convert to 0-indexed
            
            # Calculate zoom factor based on DPI (72 DPI is default in PyMuPDF)
            zoom = dpi / 72
            
            # Convert pixel coordinates to PDF points
            # Points = Pixels / Zoom
            x_pts = x / zoom
            y_pts = y / zoom
            width_pts = width / zoom
            height_pts = height / zoom
            
            # Define the clip rectangle in PDF coordinates
            clip_rect = fitz.Rect(x_pts, y_pts, x_pts + width_pts, y_pts + height_pts)
            
            # Render the clipped region
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, clip=clip_rect, alpha=False)
            
            # Save the image
            pix.save(output_path)
            
        finally:
            doc.close()
        
        return output_path

    def page_to_image(self, input_path: str, page_num: int,
                      output_path: str, dpi: int = 300) -> str:
        """
        Converts a single PDF page to PNG image.
        
        Args:
            input_path: Path to the PDF file.
            page_num: 1-indexed page number.
            output_path: Path where the PNG will be saved.
            dpi: Resolution for rendering (default: 300).
        
        Returns:
            Path to the saved PNG file.
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")
        
        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        doc = fitz.open(input_path)
        
        try:
            page = doc[page_num - 1]  # Convert to 0-indexed
            
            # Calculate zoom factor based on DPI
            zoom = dpi / 72
            mat = fitz.Matrix(zoom, zoom)
            
            # Render the full page
            pix = page.get_pixmap(matrix=mat, alpha=False)
            
            # Save the image
            pix.save(output_path)
            
        finally:
            doc.close()
        
        return output_path

    def remove_tables_vertically(self, input_path: str,
                                  table_coords: Dict[int, List[Dict[str, int]]],
                                  output_path: str, dpi: int = 300) -> str:
        """
        Removes table regions from PDF by cutting vertical strips.
        
        For each page with tables, splits the page into regions to keep
        (above and below each table) and stitches them together to create
        shortened pages without the table content.
        
        Args:
            input_path: Path to the source PDF.
            table_coords: Dictionary mapping page_number (1-indexed) to a list
                         of table coordinate dicts with 'y' and 'height' keys (in pixels).
                         Example: {1: [{'y': 100, 'height': 200}], 2: [...]}
            output_path: Path where the modified PDF will be saved.
            dpi: DPI used for table detection (default: 300). Must match the DPI
                 used when detecting table boundaries.
        
        Returns:
            Path to the modified PDF file.
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")
        
        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        source_doc = fitz.open(input_path)
        new_doc = fitz.open()
        
        # Calculate zoom factor for coordinate conversion
        zoom = dpi / 72
        
        try:
            for page_idx in range(len(source_doc)):
                page_num = page_idx + 1  # 1-indexed
                source_page = source_doc[page_idx]
                source_rect = source_page.rect
                
                if page_num not in table_coords or not table_coords[page_num]:
                    # No tables on this page, copy as-is
                    new_doc.insert_pdf(source_doc, from_page=page_idx, to_page=page_idx)
                    continue
                
                # Get tables for this page and sort by y-coordinate (top to bottom)
                tables = sorted(table_coords[page_num], key=lambda t: t['y'])
                
                # Calculate regions to keep (non-table regions)
                # We work in PDF points
                keep_regions = []
                current_y = 0
                
                for table in tables:
                    table_y_px = table['y']
                    table_height_px = table['height']
                    
                    # Convert pixel coordinates to PDF points
                    # The y coordinate from table detection is relative to the page image
                    table_y_pts = table_y_px / zoom
                    table_height_pts = table_height_px / zoom
                    table_bottom_pts = table_y_pts + table_height_pts
                    
                    # Add region from current_y to table_y (content above table)
                    if table_y_pts > current_y:
                        keep_regions.append((current_y, table_y_pts))
                    
                    # Move current_y to below the table
                    current_y = table_bottom_pts
                
                # Add final region from last table to page bottom
                if current_y < source_rect.height:
                    keep_regions.append((current_y, source_rect.height))
                
                # If no regions to keep, skip this page entirely
                if not keep_regions:
                    print(f"  Page {page_num}: No content to keep after table removal, skipping page")
                    continue
                
                # Calculate total height of new page
                total_height = sum(end - start for start, end in keep_regions)
                
                print(f"  Page {page_num}: Creating new page with height {total_height:.1f} pts "
                      f"(original: {source_rect.height:.1f} pts), "
                      f"keeping {len(keep_regions)} region(s)")
                
                # Create new page with same width but reduced height
                new_page = new_doc.new_page(width=source_rect.width, height=total_height)
                
                # Copy content from each keep region
                target_y = 0
                for start_y, end_y in keep_regions:
                    region_height = end_y - start_y
                    
                    # Define source clip rectangle (full width, vertical slice)
                    source_clip = fitz.Rect(0, start_y, source_rect.width, end_y)
                    
                    # Define target rectangle where content will be placed
                    target_rect = fitz.Rect(0, target_y, source_rect.width, target_y + region_height)
                    
                    # Copy the content using show_pdf_page
                    new_page.show_pdf_page(
                        target_rect,
                        source_doc,
                        page_idx,
                        clip=source_clip
                    )
                    
                    target_y += region_height
                
        finally:
            source_doc.close()
        
        # Save the new document
        new_doc.save(output_path, garbage=4, clean=True)
        new_doc.close()
        
        print(f"✓ PDF with tables removed saved to: {output_path}")
        return output_path

    def extract_page_range(self, input_path: str, start_page: int,
                           output_path: str, end_page: Optional[int] = None) -> str:
        """
        Extracts a range of pages from a PDF into a new PDF.
        
        Args:
            input_path: Path to the source PDF.
            start_page: 1-indexed starting page number.
            output_path: Path where the extracted PDF will be saved.
            end_page: 1-indexed ending page number (inclusive).
                     If None, extracts from start_page to end of document.
        
        Returns:
            Path to the extracted PDF file.
        """
        if not os.path.exists(input_path):
            raise FileNotFoundError(f"Input file not found: {input_path}")
        
        # Ensure output directory exists
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        
        source_doc = fitz.open(input_path)
        
        try:
            # Convert to 0-indexed
            from_page = start_page - 1
            to_page = end_page - 1 if end_page is not None else len(source_doc) - 1
            
            # Validate page range
            if from_page < 0 or from_page >= len(source_doc):
                raise ValueError(f"Invalid start_page: {start_page}")
            if to_page < 0 or to_page >= len(source_doc):
                raise ValueError(f"Invalid end_page: {end_page}")
            if from_page > to_page:
                raise ValueError(f"start_page ({start_page}) must be <= end_page ({end_page})")
            
            # Create new document with extracted pages
            new_doc = fitz.open()
            new_doc.insert_pdf(source_doc, from_page=from_page, to_page=to_page)
            new_doc.save(output_path, garbage=4, clean=True)
            new_doc.close()
            
        finally:
            source_doc.close()
        
        return output_path

