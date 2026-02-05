#!/usr/bin/env python3
"""
Test script for find_specimen_box_coordinates function.

This script takes a PDF file path as input, converts each page to an image,
detects specimen box coordinates using find_specimen_box_coordinates, draws
red rectangles around detected boundaries, and compiles the annotated images
into a new PDF for visual verification.

Usage:
    python test_find_specimen_box.py <input_pdf_path>

Output:
    Creates <input_name>_visualized.pdf in the same directory as the input.
"""

import os
import sys
import tempfile
import argparse
from pathlib import Path

import cv2
import numpy as np
import fitz  # PyMuPDF

import table_manager


def pdf_page_to_image(doc: fitz.Document, page_num: int, dpi: int = 300) -> np.ndarray:
    """
    Convert a PDF page to a numpy array image using PyMuPDF.
    
    Args:
        doc: PyMuPDF Document object
        page_num: Page number (0-indexed)
        dpi: Resolution for rendering (default: 300)
        
    Returns:
        numpy.ndarray: Image as BGR numpy array (OpenCV format)
    """
    page = doc[page_num]
    
    # Calculate zoom factor based on DPI (72 DPI is default in PyMuPDF)
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    
    # Render page to pixmap
    pix = page.get_pixmap(matrix=mat, alpha=False)
    
    # Convert pixmap to numpy array
    # PyMuPDF returns RGB data, we need to convert to BGR for OpenCV
    img_data = np.frombuffer(pix.samples, dtype=np.uint8)
    img = img_data.reshape(pix.height, pix.width, pix.n)
    
    # Convert RGB to BGR for OpenCV compatibility
    if pix.n == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    elif pix.n == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    
    return img


def draw_boundary_box(image: np.ndarray, coordinates: tuple, color: tuple = (0, 0, 255), 
                      thickness: int = 3) -> np.ndarray:
    """
    Draw a rectangle on the image at the specified coordinates.
    
    Args:
        image: Input image (numpy array)
        coordinates: Tuple of (x, y, w, h)
        color: BGR color tuple (default: red)
        thickness: Line thickness in pixels
        
    Returns:
        numpy.ndarray: Image with drawn rectangle
    """
    output_image = image.copy()
    if coordinates:
        x, y, w, h = coordinates
        cv2.rectangle(output_image, (x, y), (x + w, y + h), color, thickness)
    return output_image


def add_detection_status(image: np.ndarray, coordinates: tuple, font_scale: float = 1.0) -> np.ndarray:
    """
    Add text indicating whether a box was detected or not.
    
    Args:
        image: Input image
        coordinates: Detection result (None if not found)
        font_scale: Font scale for the text
        
    Returns:
        Image with status text added
    """
    output_image = image.copy()
    h, w = image.shape[:2]
    
    if coordinates:
        x, y, bw, bh = coordinates
        status_text = f"Box detected: x={x}, y={y}, w={bw}, h={bh}"
        color = (0, 255, 0)  # Green for success
    else:
        status_text = "No box detected"
        color = (0, 0, 255)  # Red for failure
    
    # Calculate text position (bottom-left corner with padding)
    text_x = 20
    text_y = h - 20
    
    # Add background rectangle for better readability
    (text_w, text_h), _ = cv2.getTextSize(status_text, cv2.FONT_HERSHEY_SIMPLEX, font_scale, 2)
    cv2.rectangle(output_image, 
                  (text_x - 5, text_y - text_h - 5), 
                  (text_x + text_w + 5, text_y + 5), 
                  (0, 0, 0), -1)
    
    # Add text
    cv2.putText(output_image, status_text, (text_x, text_y), 
                cv2.FONT_HERSHEY_SIMPLEX, font_scale, color, 2)
    
    return output_image


def images_to_pdf(images: list, output_path: str, dpi: int = 300) -> None:
    """
    Compile a list of images into a single PDF file.
    
    Args:
        images: List of numpy array images (BGR format)
        output_path: Path for the output PDF
        dpi: DPI for the output PDF
    """
    if not images:
        raise ValueError("No images to compile into PDF")
    
    # Convert first image to get dimensions
    first_img = images[0]
    h, w = first_img.shape[:2]
    
    # Create new PDF document
    doc = fitz.open()
    
    for img in images:
        # Convert BGR to RGB for PyMuPDF
        if len(img.shape) == 3:
            img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        else:
            img_rgb = cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
        
        # Get image dimensions
        img_h, img_w = img_rgb.shape[:2]
        
        # Create new page with image dimensions
        rect = fitz.Rect(0, 0, img_w, img_h)
        page = doc.new_page(width=img_w, height=img_h)
        
        # Insert image into page
        # Convert numpy array to bytes
        img_bytes = cv2.imencode('.png', img_rgb)[1].tobytes()
        page.insert_image(rect, stream=img_bytes)
    
    # Save the PDF
    doc.save(output_path, garbage=4, clean=True)
    doc.close()
    print(f"  Saved visualization PDF: {output_path}")


def process_pdf(input_path: str, output_path: str = None, dpi: int = 300) -> dict:
    """
    Process a PDF file: detect specimen boxes on each page and create visualization.
    
    Args:
        input_path: Path to input PDF
        output_path: Path for output visualization PDF (optional)
        dpi: Resolution for rendering pages
        
    Returns:
        Dictionary with detection results per page
    """
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input file not found: {input_path}")
    
    # Determine output path if not provided
    if output_path is None:
        input_path_obj = Path(input_path)
        output_path = input_path_obj.parent / f"{input_path_obj.stem}_visualized.pdf"
    
    print(f"Processing PDF: {input_path}")
    print(f"Output will be saved to: {output_path}")
    print(f"Using DPI: {dpi}")
    print("-" * 50)
    
    # Open PDF
    doc = fitz.open(input_path)
    num_pages = len(doc)
    print(f"Total pages: {num_pages}")
    
    results = {}
    annotated_images = []
    
    with tempfile.TemporaryDirectory() as temp_dir:
        for page_num in range(num_pages):
            page_number = page_num + 1  # 1-indexed for display
            print(f"\nProcessing page {page_number}/{num_pages}...")
            
            # Convert PDF page to image
            try:
                img = pdf_page_to_image(doc, page_num, dpi=dpi)
                print(f"  Rendered page to image: {img.shape[1]}x{img.shape[0]} pixels")
            except Exception as e:
                print(f"  ERROR: Failed to render page {page_number}: {e}")
                results[page_number] = {"error": str(e), "coordinates": None}
                continue
            
            # Save temporary image for find_specimen_box_coordinates
            temp_image_path = os.path.join(temp_dir, f"page_{page_number}.png")
            cv2.imwrite(temp_image_path, img)
            
            # Call find_specimen_box_coordinates
            try:
                coordinates = table_manager.find_specimen_box_coordinates(temp_image_path)
                results[page_number] = {"coordinates": coordinates}
                
                if coordinates:
                    x, y, w, h = coordinates
                    print(f"  Box detected at: x={x}, y={y}, w={w}, h={h}")
                else:
                    print(f"  No box detected on this page")
                    
            except Exception as e:
                print(f"  ERROR: find_specimen_box_coordinates failed: {e}")
                results[page_number] = {"error": str(e), "coordinates": None}
                coordinates = None
            
            # Draw red rectangle on detected boundary
            annotated_img = draw_boundary_box(img, coordinates, color=(0, 0, 255), thickness=3)
            
            # Add detection status text
            annotated_img = add_detection_status(annotated_img, coordinates)
            
            annotated_images.append(annotated_img)
    
    doc.close()
    
    # Compile all annotated images into a single PDF
    print("\n" + "-" * 50)
    print("Compiling visualization PDF...")
    try:
        images_to_pdf(annotated_images, str(output_path), dpi=dpi)
    except Exception as e:
        print(f"ERROR: Failed to create output PDF: {e}")
        raise
    
    # Print summary
    print("\n" + "=" * 50)
    print("SUMMARY")
    print("=" * 50)
    detected_count = sum(1 for r in results.values() if r.get("coordinates") is not None)
    error_count = sum(1 for r in results.values() if "error" in r)
    print(f"Total pages processed: {num_pages}")
    print(f"Pages with detected boxes: {detected_count}")
    print(f"Pages with errors: {error_count}")
    print(f"Pages with no detection: {num_pages - detected_count - error_count}")
    print(f"\nVisualization saved to: {output_path}")
    
    return results


def main():
    parser = argparse.ArgumentParser(
        description="Test find_specimen_box_coordinates on a PDF file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python test_find_specimen_box.py document.pdf
    python test_find_specimen_box.py /path/to/document.pdf --dpi 200
    python test_find_specimen_box.py document.pdf -o /path/to/output.pdf
        """
    )
    
    parser.add_argument(
        "input_pdf",
        help="Path to the input PDF file"
    )
    
    parser.add_argument(
        "-o", "--output",
        help="Path for the output visualization PDF (default: <input>_visualized.pdf)"
    )
    
    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="DPI for rendering PDF pages (default: 300)"
    )
    
    args = parser.parse_args()
    
    try:
        results = process_pdf(args.input_pdf, args.output, dpi=args.dpi)
        
        # Exit with error code if any page had errors
        if any("error" in r for r in results.values()):
            sys.exit(1)
            
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
