import cv2
import os
import numpy as np


def get_table_boundaries(image_path):
    # 1. Load Image
    image = cv2.imread(image_path)
    if image is None:
        return None
    
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # 2. Adaptive Thresholding
    # Creates a binary map where lines/text are white, background is black
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
        cv2.THRESH_BINARY_INV, 11, 2
    )

    # 3. Detect Horizontal Lines
    # Kernel: Wide and Short. (e.g., 50x1)
    # This isolates the "rungs" of the table ladder.
    horiz_kernel_len = w // 40
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (horiz_kernel_len, 1))
    
    detect_horizontal = cv2.erode(thresh, horiz_kernel, iterations=1)
    detect_horizontal = cv2.dilate(detect_horizontal, horiz_kernel, iterations=1)

    # 4. Detect Vertical Lines
    # Kernel: Narrow and Tall. 
    # CRITICAL CHANGE: Reduced height factor from h//30 to h//100.
    # Previous code used h//30 (approx 100px), which erased header separators 
    # that were shorter than 100px. Now we capture lines as short as ~20-30px.
    vert_kernel_len = max(5, h // 100) 
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vert_kernel_len))
    
    detect_vertical = cv2.erode(thresh, vert_kernel, iterations=1)
    detect_vertical = cv2.dilate(detect_vertical, vert_kernel, iterations=1)

    # 5. Combine and Fuse (The Fix)
    mask = cv2.add(detect_horizontal, detect_vertical)

    # APPLY MORPHOLOGICAL CLOSING
    # This operation connects nearby objects. We use a vertical kernel.
    # If the gap between the header and body is smaller than 'gap_threshold',
    # they will be fused into a single contour.
    # A gap of h//60 is roughly 1.5% of the page height (approx 30-50px),
    # which is enough to bridge the header gap but small enough to avoid 
    # merging the table with the document title.
    gap_threshold = h // 60
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, gap_threshold))
    final_mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_kernel)

    # 6. Find Contours
    contours, _ = cv2.findContours(final_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if not contours:
        return None

    # 7. Filter: Get largest box by area
    largest_box = None
    max_area = 0
    
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        area = cw * ch
        
        # Minimum area threshold (0.5% of page) to ignore noise
        if area > (w * h * 0.005):
            if area > max_area:
                max_area = area
                largest_box = (x, y, cw, ch)

    return largest_box


def visualize_table_boundary(image_path, output_path=None):
    """
    Creates a visualization of the detected table boundary by drawing a red border
    on a copy of the original image.
    
    Args:
        image_path: Path to the input image containing a table
        output_path: Path where the output image will be saved. If None,
                     saves as '<original_name>_table_detected.<ext>'
    
    Returns:
        tuple: (x, y, w, h) coordinates of the detected table, or None if not found
    """
    # Get table boundaries
    result = get_table_boundaries(image_path)
    
    if result is None:
        print(f"No table detected in: {image_path}")
        return None
    
    x, y, w, h = result
    
    # Load the original image
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"Could not open or find the image: {image_path}")
    
    # Create a copy and draw red rectangle
    output_image = image.copy()
    cv2.rectangle(output_image, (x, y), (x + w, y + h), (0, 0, 255), 3)
    
    # Determine output path
    if output_path is None:
        import os
        base, ext = os.path.splitext(image_path)
        output_path = f"{base}_table_detected{ext}"
    
    # Save the output image
    cv2.imwrite(output_path, output_image)
    print(f"Saved visualization to: {output_path}")
    print(f"Table detected at: x={x}, y={y}, w={w}, h={h}")
    
    return x, y, w, h



# test_files = [
#     'labs_p1.png',
#     'table1.png',
#     'table2.png',
#     'table3.png'
# ]

# for file in test_files:
#     file_path = os.path.abspath('.') + '/test_data/' + file
#     x, y, w, h = get_table_boundaries(file_path)
#     print(f"Table found at: x={x}, y={y}, w={w}, h={h}")
#     visualize_table_boundary(file_path)

