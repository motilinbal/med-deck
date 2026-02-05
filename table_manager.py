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




def detect_stylized_box(image_path):
    """
    Detects a specific box format with a stylized gray rim (bevel effect),
    distinguishing it from tables (thin lines) and headers (solid blocks).
    
    Returns:
        tuple: (x, y, w, h) of the detected box, or None if not found.
    """
    # 1. Load and Preprocess
    img = cv2.imread(image_path)
    if img is None:
        raise ValueError("Image not found")
        
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape

    # 2. Edge Detection
    # Use adaptive thresholding to be robust against lighting/resolution changes
    thresh = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                                   cv2.THRESH_BINARY_INV, 11, 2)

    # 3. Find Contours
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    candidates = []

    for cnt in contours:
        # Approximate contour to polygon
        epsilon = 0.02 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, epsilon, True)

        # We only care about rectangles (4 corners)
        if len(approx) == 4:
            x, y, w, h = cv2.boundingRect(approx)
            
            # --- Filter 1: Geometric Sanity ---
            # Box must be of reasonable size relative to image (e.g., > 5% of width)
            if w < width * 0.05 or h < height * 0.05:
                continue

            # --- Filter 2: The "Bevel Profile" Check ---
            # We verify the visual signature of the top border.
            
            # Dynamic scan depth: ~5% of image height. 
            # This makes it resolution invariant. A 4k image needs a deeper scan than a 480p one.
            scan_depth = int(height * 0.05) 
            
            # Safety check to avoid index out of bounds
            if y + scan_depth >= height: 
                continue

            # Extract a vertical slice through the center of the top border
            # We look at the pixels from the top edge 'y' downwards
            mid_x = x + w // 2
            roi_slice = gray[y : y + scan_depth, mid_x]

            # Analyze the profile:
            # A Target Box profile looks like: [Black Edge] -> [Gray Rim] -> [White Content]
            
            # Logic:
            # 1. Start is usually dark (the border line).
            # 2. We look for the transition back to "White" (Content).
            # 3. The distance between "Start" and "White Content" determines the type.
            
            white_thresh = 230  # Threshold for "Content White"
            border_end_index = -1
            
            for i, pixel_val in enumerate(roi_slice):
                if pixel_val > white_thresh:
                    border_end_index = i
                    break
            
            if border_end_index == -1:
                # If we never hit white, it's a Solid Header (filled block)
                continue
                
            # Calculate "Rim Thickness" relative to image height
            rim_thickness_ratio = border_end_index / height
            
            # --- The Classification Logic ---
            # Table Line: Very thin (e.g., < 0.3% of image height)
            # Target Box: Thick Rim (e.g., 0.5% - 5% of image height)
            # Solid Header: Logic above handles it (never returns to white) or extremely deep
            
            MIN_RIM_RATIO = 0.004  # 0.4% (Filters out thin table lines)
            MAX_RIM_RATIO = 0.10   # 10%  (Filters out massive layout blocks)

            if MIN_RIM_RATIO < rim_thickness_ratio < MAX_RIM_RATIO:
                # Secondary Check: Ensure the "Rim" area is actually gray/colored, not just white noise
                # Get mean color of the rim area (excluding the first few pixels which are the black line)
                rim_segment = roi_slice[2:border_end_index] # Skip outer black line
                if len(rim_segment) > 0:
                    mean_rim_val = np.mean(rim_segment)
                    # The rim should be Gray (< 230), not White
                    if mean_rim_val < 235:
                        candidates.append((x, y, w, h))

    # Return the largest candidate (assuming the main box is the primary subject)
    if candidates:
        # Sort by area (w*h) descending
        candidates.sort(key=lambda b: b[2] * b[3], reverse=True)
        return candidates[0]
    
    return None

# Usage
# result = detect_stylized_box('image_cc4fa6.png')
# if result:
#     print(f"Box found at: {result}")