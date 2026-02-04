"""
PDF Preprocessing Module
Handles physical manipulation of PDF files using PyMuPDF (fitz).
Used for anonymization (cropping headers) before AI ingestion.
"""

import os
import tempfile
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

    def get_table_boundaries(self, input_path: str, dpi: int = 150) -> dict:
        """
        Detects table boundaries on each page of the PDF.
        
        Converts each page to an image and uses table_manager to detect
        table boundaries. Returns a dictionary with page numbers as keys
        and table boundary coordinates as values.
        
        Args:
            input_path: Path to the source PDF.
            dpi: Resolution for rendering PDF pages to images (default: 150).
                 Higher DPI = better accuracy but slower processing.
        
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
                pix = page.get_pixmap(matrix=mat)
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

