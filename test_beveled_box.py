#!/usr/bin/env python3
"""
Test script for detect_beveled_box function from table_manager.py

This script:
1. Takes a PDF file path as a command-line argument
2. Converts each page to an image
3. Runs detect_beveled_box on each page
4. Draws visualization (red crosshair + lines) at detected coordinates
5. Compiles all annotated images into a new PDF for review

Usage:
    python test_beveled_box.py <pdf_path> [output_pdf_path]

Example:
    python test_beveled_box.py input.pdf
    python test_beveled_box.py input.pdf output_review.pdf
"""

import sys
import os
import tempfile
import argparse
from typing import Optional

import cv2
import numpy as np
import fitz  # PyMuPDF
from PIL import Image

import table_manager


def pdf_page_to_image(doc: fitz.Document, page_num: int, dpi: int = 300) -> np.ndarray:
    """
    Convert a PDF page to a numpy array (OpenCV format).
    
    Args:
        doc: PyMuPDF Document object
        page_num: Page number (0-indexed)
        dpi: Resolution for rendering (default: 300)
    
    Returns:
        Image as numpy array (BGR format for OpenCV)
    """
    page = doc[page_num]
    zoom = dpi / 72  # 72 DPI is default in PyMuPDF
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    
    # Convert pixmap to numpy array
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    
    # Convert RGB to BGR for OpenCV
    if pix.n == 3:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    elif pix.n == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    
    return img


def draw_detection_visualization(
    image: np.ndarray, 
    detection: Optional[dict], 
    page_num: int
) -> np.ndarray:
    """
    Draw visualization on the image showing the detected beveled box coordinates.
    
    Draws:
    - A red horizontal line at top_y (spanning full width)
    - A red vertical line at right_x (spanning full height)
    - A red circle at the intersection point (right_x, top_y)
    - Text labels showing the coordinate values
    
    Args:
        image: OpenCV image (numpy array)
        detection: Dictionary with 'top_y' and 'right_x' or None
        page_num: Page number for labeling
    
    Returns:
        Annotated image
    """
    h, w = image.shape[:2]
    
    # Create a copy to draw on
    annotated = image.copy()
    
    if detection is None:
        # No detection - draw "NOT DETECTED" text
        text = f"Page {page_num}: NO BEVELED BOX DETECTED"
        cv2.putText(
            annotated, 
            text, 
            (50, 50), 
            cv2.FONT_HERSHEY_SIMPLEX, 
            1.0, 
            (0, 0, 255),  # Red
            2
        )
        return annotated
    
    top_y = detection['top_y']
    right_x = detection['right_x']
    
    # Draw red horizontal line at top_y (full width)
    cv2.line(
        annotated, 
        (0, top_y), 
        (w, top_y), 
        (0, 0, 255),  # Red in BGR
        2
    )
    
    # Draw red vertical line at right_x (full height)
    cv2.line(
        annotated, 
        (right_x, 0), 
        (right_x, h), 
        (0, 0, 255),  # Red in BGR
        2
    )
    
    # Draw a larger circle at the intersection point
    cv2.circle(
        annotated, 
        (right_x, top_y), 
        15,  # Radius
        (0, 0, 255),  # Red
        3   # Thickness
    )
    
    # Draw a smaller filled circle at the intersection point
    cv2.circle(
        annotated, 
        (right_x, top_y), 
        5,  # Radius
        (0, 0, 255),  # Red
        -1  # Filled
    )
    
    # Draw crosshair lines at the intersection
    crosshair_len = 30
    cv2.line(
        annotated,
        (right_x - crosshair_len, top_y),
        (right_x + crosshair_len, top_y),
        (0, 255, 255),  # Yellow
        2
    )
    cv2.line(
        annotated,
        (right_x, top_y - crosshair_len),
        (right_x, top_y + crosshair_len),
        (0, 255, 255),  # Yellow
        2
    )
    
    # Add text labels
    # Label for top_y
    cv2.putText(
        annotated,
        f"top_y = {top_y}",
        (10, top_y - 10),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 255),  # Yellow
        2
    )
    
    # Label for right_x
    cv2.putText(
        annotated,
        f"right_x = {right_x}",
        (right_x + 10, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 255, 255),  # Yellow
        2
    )
    
    # Page number and status at top left
    status_text = f"Page {page_num}: DETECTED"
    cv2.putText(
        annotated,
        status_text,
        (10, h - 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.8,
        (0, 255, 0),  # Green
        2
    )
    
    return annotated


def save_images_to_pdf(image_paths: list, output_path: str):
    """
    Save a list of image paths to a single PDF file.
    
    Args:
        image_paths: List of paths to image files
        output_path: Path for the output PDF
    """
    if not image_paths:
        print("No images to save!")
        return
    
    # Open first image to get dimensions
    first_image = Image.open(image_paths[0])
    
    # Convert remaining images to PIL Image objects
    other_images = []
    for path in image_paths[1:]:
        img = Image.open(path)
        # Convert to RGB if necessary (in case of RGBA)
        if img.mode != 'RGB':
            img = img.convert('RGB')
        other_images.append(img)
    
    # Convert first image to RGB if necessary
    if first_image.mode != 'RGB':
        first_image = first_image.convert('RGB')
    
    # Save as PDF
    first_image.save(
        output_path,
        "PDF",
        resolution=100.0,
        save_all=True,
        append_images=other_images
    )
    
    print(f"✓ Review PDF saved to: {output_path}")


def process_pdf(input_path: str, output_path: str, dpi: int = 300):
    """
    Process a PDF file: detect beveled boxes on each page and create a review PDF.
    
    Args:
        input_path: Path to input PDF
        output_path: Path for output review PDF
        dpi: Resolution for rendering pages (default: 300)
    """
    if not os.path.exists(input_path):
        print(f"Error: Input file not found: {input_path}")
        sys.exit(1)
    
    print(f"Processing PDF: {input_path}")
    print(f"Output will be saved to: {output_path}")
    print(f"Using DPI: {dpi}")
    print("-" * 50)
    
    # Open PDF
    doc = fitz.open(input_path)
    num_pages = len(doc)
    print(f"Total pages: {num_pages}")
    
    # Create temporary directory for intermediate images
    temp_image_paths = []
    
    with tempfile.TemporaryDirectory() as temp_dir:
        for page_num in range(num_pages):
            print(f"\nProcessing page {page_num + 1}/{num_pages}...")
            
            # Convert page to image
            img = pdf_page_to_image(doc, page_num, dpi)
            
            # Save temporary image for detect_beveled_box
            temp_img_path = os.path.join(temp_dir, f"page_{page_num + 1}.png")
            cv2.imwrite(temp_img_path, img)
            
            # Run detection
            detection = table_manager.detect_beveled_box(temp_img_path)
            
            if detection:
                print(f"  ✓ Detected: top_y={detection['top_y']}, right_x={detection['right_x']}")
            else:
                print(f"  ✗ No beveled box detected")
            
            # Draw visualization
            annotated_img = draw_detection_visualization(img, detection, page_num + 1)
            
            # Save annotated image
            annotated_path = os.path.join(temp_dir, f"annotated_{page_num + 1}.png")
            cv2.imwrite(annotated_path, annotated_img)
            temp_image_paths.append(annotated_path)
        
        doc.close()
        
        # Compile all annotated images into PDF
        print("\n" + "-" * 50)
        print("Compiling review PDF...")
        save_images_to_pdf(temp_image_paths, output_path)
    
    print("\n" + "=" * 50)
    print("Processing complete!")
    print(f"Review the output PDF to verify detections: {output_path}")


def main():
    parser = argparse.ArgumentParser(
        description="Test detect_beveled_box function on a PDF file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python test_beveled_box.py input.pdf
  python test_beveled_box.py input.pdf output.pdf
  python test_beveled_box.py input.pdf --dpi 200
        """
    )
    
    parser.add_argument(
        "input_pdf",
        help="Path to the input PDF file"
    )
    parser.add_argument(
        "output_pdf",
        nargs="?",
        help="Path for the output review PDF (default: <input>_beveled_box_review.pdf)"
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="DPI for rendering PDF pages (default: 300)"
    )
    
    args = parser.parse_args()
    
    # Determine output path
    if args.output_pdf:
        output_path = args.output_pdf
    else:
        base_name = os.path.splitext(args.input_pdf)[0]
        output_path = f"{base_name}_beveled_box_review.pdf"
    
    # Process the PDF
    process_pdf(args.input_pdf, output_path, args.dpi)


if __name__ == "__main__":
    main()
