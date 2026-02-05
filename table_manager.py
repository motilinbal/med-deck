import cv2
import os
import numpy as np
import matplotlib.pyplot as plt


def get_table_boundaries(image_path):
    # 1. Load Image
    image = cv2.imread(image_path)
    if image is None:
        return None
    
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape

    # 2. Adaptive Thresholding
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
        cv2.THRESH_BINARY_INV, 11, 2
    )

    # 3. Detect Horizontal Lines
    # Scale: Length is ~2.5% of width
    horiz_kernel_len = w // 40
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (horiz_kernel_len, 1))
    
    # Use Open morphology (Erode -> Dilate) to isolate lines
    detect_horizontal = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, horiz_kernel, iterations=1)

    # 4. Detect Vertical Lines
    # Scale: Height is ~1% of height to catch short header separators
    vert_kernel_len = max(5, h // 100)
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vert_kernel_len))
    
    detect_vertical = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, vert_kernel, iterations=1)

    # --- CRITICAL CHANGE 1: CREATE A "JOINTS" MAP ---
    # A joint is where a horizontal line crosses a vertical line.
    # We will use this later to verify if a box is actually a grid.
    joints_mask = cv2.bitwise_and(detect_horizontal, detect_vertical)

    # 5. Combine to form the "Table Mask"
    mask = cv2.add(detect_horizontal, detect_vertical)

    # Gap Filling (User's logic): Connect header and body if separated
    # Uses a vertical kernel to bridge gaps ~1.5% of page height
    gap_threshold = h // 60
    close_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, gap_threshold))
    final_mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, close_kernel)

    # 6. Find Contours
    contours, _ = cv2.findContours(final_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if not contours:
        return None

    # 7. Filter Candidates
    best_box = None
    max_area = 0
    
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        area = cw * ch
        
        # A. Minimum Area Filter (ignore noise)
        if area < (w * h * 0.005):
            continue

        # --- CRITICAL CHANGE 2: VERIFY "TABLENESS" ---
        # Look inside the bounding box on the 'joints_mask'.
        # A simple frame has ~4 joints (corners). A table has many.
        
        # Crop the joints mask to the current bounding box
        roi_joints = joints_mask[y:y+ch, x:x+cw]
        
        # Count distinct intersection points
        # connectedComponents is robust against line thickness
        num_joints, _ = cv2.connectedComponents(roi_joints)
        
        # We subtract 1 because connectedComponents counts the background as label 0
        num_joints = num_joints - 1

        # Heuristic: A valid table needs at least 2 rows and 2 columns,
        # implying at least ~6-9 intersections depending on the border style.
        # We set threshold > 10 to firmly reject empty text boxes.
        if num_joints > 10:
            
            # If it passes the "Tableness" test, we THEN pick the largest one.
            if area > max_area:
                max_area = area
                best_box = (x, y, cw, ch)

    return best_box


def get_table_boundaries_second_pass(image_path):
    """
    Detect table boundaries using second-pass logic (aggressive detection with blue text heuristic).
    
    This implementation is resolution-invariant - it uses ratios of image dimensions
    rather than hardcoded pixel values, allowing it to work reliably across different
    DPI settings (150 DPI to 300+ DPI).
    
    Args:
        image_path: Path to the input image
        
    Returns:
        tuple: (x, y, w, h) coordinates of the largest valid table, or None if not found
    """
    # 1. Load Image
    original_img = cv2.imread(image_path)
    if original_img is None:
        print(f"Error: Could not load {image_path}")
        return None

    # Convert to grayscale and HSV
    gray = cv2.cvtColor(original_img, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(original_img, cv2.COLOR_BGR2HSV)
    
    # Get image dimensions for resolution-invariant calculations
    img_h, img_w = gray.shape[:2]
    
    # Reference width for scaling (assumes 1000px is "standard" resolution)
    # This allows thresholds to scale proportionally with image size
    scale_factor = img_w / 1000.0

    # 2. Aggressive Grid Detection
    # Use a simple binary threshold since the background is clean white
    # This is often cleaner than adaptive thresholding for these specific medical charts
    _, thresh = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY_INV)

    # Detect Horizontal and Vertical lines separately
    # Scale kernels relative to image dimensions for resolution invariance
    scale = 30
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (int(img_w / scale), 1))
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, int(img_h / scale)))

    # Morphological opening to isolate lines
    h_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, h_kernel, iterations=1)
    v_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, v_kernel, iterations=1)

    # Combine to find the grid structure
    # We dilate slightly to make the thin lines robust
    grid_mask = cv2.add(h_lines, v_lines)
    grid_mask = cv2.dilate(grid_mask, np.ones((3, 3), np.uint8), iterations=1)

    # 3. Find All Candidate Boxes
    contours, _ = cv2.findContours(grid_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    boxes = []
    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        # Filter noise using resolution-invariant thresholds:
        # Width must be > 5% of image width, Height > 1% of image height
        min_width = int(img_w * 0.05)
        min_height = int(img_h * 0.01)
        if w > min_width and h > min_height:
            boxes.append((x, y, w, h))

    # 4. Vertical "Snap" / Merge Logic
    # We need to merge the detached header (top) with the body (bottom)
    # Sort boxes by Y position
    boxes.sort(key=lambda b: b[1])

    merged_boxes = []
    used_indices = set()
    
    # Resolution-invariant gap tolerance: ~6% of image height (was 60px at ~1000px height)
    gap_tolerance = int(img_h * 0.06)

    # Iterate and try to merge
    for i in range(len(boxes)):
        if i in used_indices:
            continue
        
        x1, y1, w1, h1 = boxes[i]
        current_merge = [x1, y1, w1, h1]
        used_indices.add(i)

        # Look ahead for a box directly below this one
        for j in range(i + 1, len(boxes)):
            if j in used_indices:
                continue

            x2, y2, w2, h2 = boxes[j]

            # Check Vertical Proximity: Is Box B just below Box A?
            # Use resolution-invariant gap tolerance
            gap = y2 - (y1 + h1)
            is_close_vertically = 0 < gap < gap_tolerance

            # Check Horizontal Alignment: Do they have roughly the same width and x-pos?
            # We check overlap
            x_start = max(x1, x2)
            x_end = min(x1 + w1, x2 + w2)
            overlap = max(0, x_end - x_start)
            min_width = min(w1, w2)
            
            # If they overlap by at least 80% of their width
            is_aligned = (overlap / min_width) > 0.8 if min_width > 0 else False

            if is_close_vertically and is_aligned:
                # MERGE THEM
                new_x = min(x1, x2)
                new_y = min(y1, y2)
                new_w = max(x1 + w1, x2 + w2) - new_x
                new_h = (y2 + h2) - new_y # Span from top of A to bottom of B
                
                # Update current merge
                current_merge = [new_x, new_y, new_w, new_h]
                
                # Update reference for next iteration (in case there are 3 parts)
                x1, y1, w1, h1 = current_merge
                used_indices.add(j)

        merged_boxes.append(current_merge)

    # 5. Final Validation: The "Blue Text" Heuristic
    final_tables = []
    
    for (x, y, w, h) in merged_boxes:
        # Heuristic: Table must be reasonably large (resolution-invariant)
        # Width > 10% of image, Height > 5% of image
        min_table_width = int(img_w * 0.10)
        min_table_height = int(img_h * 0.05)
        if w < min_table_width or h < min_table_height:
            continue

        # Check the "Header" area for blue text
        # Use resolution-invariant header height: ~6% of image height or 20% of box height
        header_h = min(int(img_h * 0.06), h // 2)
        header_h = max(header_h, int(img_h * 0.03))  # Ensure minimum header height
        roi = hsv[y:y+header_h, x:x+w]

        # Define Blue Range (Broad enough to catch the text font)
        lower_blue = np.array([90, 50, 50])
        upper_blue = np.array([130, 255, 255])
        
        mask_blue = cv2.inRange(roi, lower_blue, upper_blue)
        blue_pixels = cv2.countNonZero(mask_blue)
        
        # Resolution-invariant blue pixel threshold
        # Scale the original threshold (10) by the scale factor
        # Also consider the actual header area size
        blue_threshold = max(10 * scale_factor, int(roi.shape[0] * roi.shape[1] * 0.001))

        # "The rest of the headers contain a text written in blue letters"
        # If we see blue clusters, it's our table.
        # Title boxes (black text) will have ~0 blue pixels.
        if blue_pixels > blue_threshold:
            final_tables.append((x, y, w, h))

    # Return the largest table (similar to get_table_boundaries behavior)
    if not final_tables:
        return None
    
    # Find the largest table by area
    best_box = None
    max_area = 0
    for (x, y, w, h) in final_tables:
        area = w * h
        if area > max_area:
            max_area = area
            best_box = (x, y, w, h)
    
    return best_box



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
    result = get_table_boundaries_second_pass(image_path)
    
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
#     'table1.png',
#     'table2.png',
#     'table3.png',
#     'table4.png'
# ]

# for file in test_files:
#     file_path = os.path.abspath('.') + '/test_data/' + file
#     # x, y, w, h = get_table_boundaries_second_pass(file_path)
#     # print(f"Table found at: x={x}, y={y}, w={w}, h={h}")
#     visualize_table_boundary(file_path)
