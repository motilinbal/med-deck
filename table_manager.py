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

