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




def find_specimen_box_coordinates(image_path):
    """
    Identifies the coordinates of a specific medical/administrative box 
    while excluding tables and gray headers.
    
    Args:
        image_path (str): Path to the input image.
        
    Returns:
        tuple: (x, y, w, h) of the target box, or None if not found.
    """
    # 1. Load Image
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError("Could not load image.")
    
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h_img, w_img = gray.shape

    # --- Resolution Invariance Scale ---
    # We normalize parameters based on image width. 
    # Assuming a reference width of ~1000px, we scale kernels accordingly.
    scale = w_img / 1000.0
    if scale < 0.5: scale = 0.5 # Clamp for very small images

    # 2. Preprocessing (Adaptive Thresholding)
    # Invert image: Background becomes black, Text/Lines become white.
    # Adaptive helps with varying lighting or scan quality.
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C, cv2.THRESH_BINARY_INV, 15, 2
    )

    # 3. Morphological Line Extraction
    # We want to separate structural lines from text.
    
    # Horizontal Kernel: Wide but 1px tall. Detects long horizontal lines.
    h_kernel_len = int(30 * scale)
    h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (h_kernel_len, 1))
    
    # Vertical Kernel: Tall but 1px wide. Detects vertical dividers.
    # Length is crucial: must be taller than text font, but shorter than box height.
    v_kernel_len = int(15 * scale)
    v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, v_kernel_len))

    # Extract lines
    h_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, h_kernel)
    v_lines = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, v_kernel)

    # 4. Combine and Connect
    # Combine horizontal and vertical lines to form the grid
    grid_mask = cv2.add(h_lines, v_lines)
    
    # Dilate slightly to close small gaps in corners
    kernel_dilate = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    grid_mask = cv2.dilate(grid_mask, kernel_dilate, iterations=1)

    # 5. Find Contours
    contours, _ = cv2.findContours(grid_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best_candidate = None
    max_area = 0

    for cnt in contours:
        x, y, w, h = cv2.boundingRect(cnt)
        area = w * h

        # --- Filter 1: Basic Geometry ---
        # Discard noise or lines that aren't boxes
        if w < 100 * scale or h < 50 * scale:
            continue
            
        # --- Filter 2: The "Table" Check ---
        # A table has vertical dividers in the middle. The target box does not.
        # We look at the 'v_lines' mask (pure vertical lines) inside the box.
        
        # Define a Region of Interest (ROI) strip just below the top border
        # We skip the first few pixels (border) and look at the top 25% of the box
        roi_top = y + int(10 * scale)
        roi_bottom = y + int(h * 0.25)
        
        # We look at the middle 80% of the width (excluding left/right borders)
        roi_left = x + int(w * 0.1)
        roi_right = x + int(w * 0.9)
        
        if roi_bottom > roi_top and roi_right > roi_left:
            v_roi = v_lines[roi_top:roi_bottom, roi_left:roi_right]
            
            # Count white pixels in this vertical-only mask
            # If there are meaningful vertical lines here, it's a table.
            if cv2.countNonZero(v_roi) > (5 * scale): 
                continue # Rejected: Contains internal vertical dividers (Table)

        # --- Filter 3: The "Header" Check (Color Analysis) ---
        # A header box has a gray background. The target box has white.
        # We analyze the original grayscale image, not the binary mask.
        
        gray_roi = gray[roi_top:roi_bottom, roi_left:roi_right]
        if gray_roi.size > 0:
            mean_intensity = np.mean(gray_roi)
            
            # Thresholding: 
            # Scanned white paper is typically > 230. 
            # Gray headers are typically < 210.
            # We use 220 as a safe cutoff.
            if mean_intensity < 220:
                continue # Rejected: Background is too dark (Gray Header)

        # --- Selection ---
        # If it passed all filters, it is a valid candidate.
        # We pick the largest one found (assuming the main form is the primary subject).
        if area > max_area:
            max_area = area
            best_candidate = (x, y, w, h)

    return best_candidate

