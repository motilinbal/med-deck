"""
Test script to validate second-pass table detection on specific pages.

This script extracts specific pages from after_first_pass.pdf and tests
the get_table_boundaries_second_pass function to verify table detection.

Usage:
    python test_second_pass_detection.py <path_to_after_first_pass.pdf> [page_numbers...]
    
Examples:
    # Test pages 1-4 (default)
    python test_second_pass_detection.py output/labs1/narrative/after_first_pass.pdf
    
    # Test specific pages
    python test_second_pass_detection.py output/labs1/narrative/after_first_pass.pdf 1 2 3 4
"""

import os
import sys
import tempfile
import fitz  # PyMuPDF
import cv2
import numpy as np

import table_manager

# DPI for rendering (should match the pipeline)
RENDER_DPI = 300


def extract_page_as_image(pdf_path: str, page_num: int, output_path: str, dpi: int = 300) -> str:
    """
    Extract a single page from a PDF and save it as an image.
    
    Args:
        pdf_path: Path to the PDF file
        page_num: Page number (1-indexed)
        output_path: Path to save the image
        dpi: Resolution for rendering
        
    Returns:
        Path to the saved image
    """
    doc = fitz.open(pdf_path)
    
    if page_num < 1 or page_num > len(doc):
        print(f"Error: Page {page_num} is out of range (PDF has {len(doc)} pages)")
        doc.close()
        return None
    
    page = doc[page_num - 1]  # 0-indexed
    zoom = dpi / 72
    mat = fitz.Matrix(zoom, zoom)
    pix = page.get_pixmap(matrix=mat, alpha=False)
    pix.save(output_path)
    doc.close()
    
    return output_path


def test_table_detection(image_path: str, page_num: int) -> dict:
    """
    Test table detection on a single image.
    
    Args:
        image_path: Path to the image file
        page_num: Page number for reporting
        
    Returns:
        Dictionary with detection results
    """
    print(f"\n{'='*60}")
    print(f"Testing Page {page_num}")
    print(f"{'='*60}")
    
    # Load image to get dimensions
    img = cv2.imread(image_path)
    if img is None:
        print(f"Error: Could not load image: {image_path}")
        return {'detected': False, 'page': page_num}
    
    h, w = img.shape[:2]
    print(f"Image dimensions: {w}x{h} pixels (at {RENDER_DPI} DPI)")
    print(f"Scale factor (relative to 1000px): {w/1000:.2f}x")
    
    # Test detection
    result = table_manager.get_table_boundaries_second_pass(image_path)
    
    if result:
        x, y, w_box, h_box = result
        print(f"✓ Table DETECTED at: x={x}, y={y}, w={w_box}, h={h_box}")
        print(f"  Table area: {w_box * h_box} pixels")
        print(f"  Table coverage: {(w_box * h_box) / (w * h) * 100:.1f}% of page")
        
        # Create visualization
        vis_path = image_path.replace('.png', '_detected.png')
        table_manager.visualize_table_boundary(image_path, vis_path)
        
        return {
            'detected': True,
            'page': page_num,
            'x': x,
            'y': y,
            'width': w_box,
            'height': h_box
        }
    else:
        print(f"✗ No table detected")
        return {'detected': False, 'page': page_num}


def main():
    if len(sys.argv) < 2:
        print("Usage: python test_second_pass_detection.py <path_to_pdf> [page_numbers...]")
        print("\nExamples:")
        print("  # Test pages 1-4 (default)")
        print("  python test_second_pass_detection.py output/labs1/narrative/after_first_pass.pdf")
        print("\n  # Test specific pages")
        print("  python test_second_pass_detection.py output/labs1/narrative/after_first_pass.pdf 1 2 3 4")
        sys.exit(1)
    
    pdf_path = sys.argv[1]
    
    if not os.path.exists(pdf_path):
        print(f"Error: PDF file not found: {pdf_path}")
        sys.exit(1)
    
    # Get page numbers to test
    if len(sys.argv) > 2:
        page_numbers = [int(p) for p in sys.argv[2:]]
    else:
        # Default: test first 4 pages
        page_numbers = [1, 2, 3, 4]
    
    print(f"Testing second-pass table detection on: {pdf_path}")
    print(f"Pages to test: {page_numbers}")
    print(f"Render DPI: {RENDER_DPI}")
    
    # Create temp directory for extracted images
    with tempfile.TemporaryDirectory() as temp_dir:
        results = []
        
        for page_num in page_numbers:
            image_path = os.path.join(temp_dir, f"page_{page_num}.png")
            
            # Extract page as image
            extracted = extract_page_as_image(pdf_path, page_num, image_path, RENDER_DPI)
            if extracted:
                # Test detection
                result = test_table_detection(image_path, page_num)
                results.append(result)
        
        # Summary
        print(f"\n{'='*60}")
        print("SUMMARY")
        print(f"{'='*60}")
        
        detected_count = sum(1 for r in results if r['detected'])
        total_count = len(results)
        
        print(f"Total pages tested: {total_count}")
        print(f"Tables detected: {detected_count}")
        print(f"Detection rate: {detected_count/total_count*100:.1f}%")
        
        print("\nDetailed results:")
        for r in results:
            status = "✓ DETECTED" if r['detected'] else "✗ NOT DETECTED"
            print(f"  Page {r['page']}: {status}")


if __name__ == "__main__":
    main()
