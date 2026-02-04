#!/usr/bin/env python3
"""
Test script for table detection in PDF files.

This script:
1. Takes a PDF file path as input
2. Detects table boundaries on each page
3. Creates a visualized PDF with red boxes around detected tables

Usage:
    python test_table_detection.py <pdf_path> [--dpi DPI] [--output OUTPUT]

Example:
    python test_table_detection.py document.pdf --dpi 150 --output annotated.pdf
"""

import os
import sys
import argparse
import tempfile
import fitz  # PyMuPDF
import cv2
import numpy as np

import table_manager
from pdf_processor import PDFPreprocessor


def visualize_tables_in_pdf(input_path: str, output_path: str = None, dpi: int = 150):
    """
    Detect tables in a PDF and create an annotated PDF with red boxes around tables.
    
    Args:
        input_path: Path to the source PDF file.
        output_path: Path where the annotated PDF will be saved. If None,
                     saves as '<original_name>_tables_annotated.pdf'.
        dpi: Resolution for rendering PDF pages to images (default: 150).
    
    Returns:
        Path to the annotated PDF file.
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")
    
    # Determine output path
    if output_path is None:
        base, ext = os.path.splitext(input_path)
        output_path = f"{base}_tables_annotated.pdf"
    
    print(f"Processing PDF: {input_path}")
    print(f"Output will be saved to: {output_path}")
    
    # Open the PDF
    doc = fitz.open(input_path)
    
    # Calculate zoom factor based on DPI (72 DPI is default in PyMuPDF)
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    
    # Create a new PDF for output
    output_doc = fitz.open()
    
    # Create temporary directory for processing
    with tempfile.TemporaryDirectory() as temp_dir:
        for page_num in range(len(doc)):
            page = doc[page_num]
            page_number = page_num + 1  # 1-indexed
            
            print(f"\nProcessing page {page_number}...")
            
            # Render page to image at specified DPI
            pix = page.get_pixmap(matrix=mat)
            temp_image_path = os.path.join(temp_dir, f"page_{page_number}.png")
            pix.save(temp_image_path)
            
            # Detect table boundaries using table_manager
            boundary = table_manager.get_table_boundaries_second_pass(temp_image_path)
            
            if boundary:
                x, y, w, h = boundary
                print(f"  Table detected at: x={x}, y={y}, w={w}, h={h}")
                
                # Load the image and draw red rectangle
                image = cv2.imread(temp_image_path)
                cv2.rectangle(image, (x, y), (x + w, y + h), (0, 0, 255), 3)
                
                # Save annotated image
                annotated_image_path = os.path.join(temp_dir, f"page_{page_number}_annotated.png")
                cv2.imwrite(annotated_image_path, image)
                
                # Convert annotated image back to PDF page
                # Create a new page with the same dimensions
                img_rect = fitz.Rect(0, 0, pix.width, pix.height)
                new_page = output_doc.new_page(width=page.rect.width, height=page.rect.height)
                
                # Insert the annotated image into the page
                new_page.insert_image(page.rect, filename=annotated_image_path)
            else:
                print(f"  No table detected on page {page_number}")
                
                # Copy original page without annotation
                new_page = output_doc.new_page(width=page.rect.width, height=page.rect.height)
                new_page.show_pdf_page(new_page.rect, doc, page_num)
    
    # Save the annotated PDF
    output_doc.save(output_path, garbage=4, clean=True)
    output_doc.close()
    doc.close()
    
    print(f"\n✓ Annotated PDF saved to: {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Detect tables in a PDF and create an annotated PDF with red boxes around tables."
    )
    parser.add_argument("pdf_path", help="Path to the input PDF file")
    parser.add_argument(
        "--dpi",
        type=int,
        default=150,
        help="Resolution for rendering PDF pages (default: 150)"
    )
    parser.add_argument(
        "--output",
        "-o",
        default=None,
        help="Output path for the annotated PDF (default: <input>_tables_annotated.pdf)"
    )
    
    args = parser.parse_args()
    
    try:
        # First, test the get_table_boundaries method from PDFPreprocessor
        print("=" * 60)
        print("Step 1: Testing PDFPreprocessor.get_table_boundaries()")
        print("=" * 60)
        
        preprocessor = PDFPreprocessor()
        boundaries = preprocessor.get_table_boundaries(args.pdf_path, dpi=args.dpi)
        
        print("\nDetected table boundaries:")
        for page_num, boundary in boundaries.items():
            if boundary:
                print(f"  Page {page_num}: x={boundary['x']}, y={boundary['y']}, "
                      f"w={boundary['width']}, h={boundary['height']}")
            else:
                print(f"  Page {page_num}: No table detected")
        
        # Now create the annotated PDF
        print("\n" + "=" * 60)
        print("Step 2: Creating annotated PDF with red boxes")
        print("=" * 60)
        
        output_file = visualize_tables_in_pdf(args.pdf_path, args.output, args.dpi)
        
        print("\n" + "=" * 60)
        print("Processing complete!")
        print("=" * 60)
        print(f"Annotated PDF saved to: {output_file}")
        
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error processing PDF: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
