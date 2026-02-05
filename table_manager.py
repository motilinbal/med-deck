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




def detect_beveled_box(image_path):
    """
    Detects a specific box style with a 3D beveled border.
    Robust against resolution changes and false positives (headers/tables).
    """
    img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
    if img is None: return None
    
    h, w = img.shape
    detections = {}
    
    # --- 1. Top Border Check (The Gray Halo) ---
    # We scan the center column for a horizontal black line with a "Gray Halo" above it.
    center_col = img[:, w // 2]
    black_pixels_y = np.where(center_col < 80)[0] # Threshold for black line
    
    if len(black_pixels_y) > 0:
        # Group pixels into line segments
        segments = np.split(black_pixels_y, np.where(np.diff(black_pixels_y) > 3)[0] + 1)
        for seg in segments:
            if len(seg) < 2: continue
            y_start = seg[0]
            
            # Context Sampling: 3 pixels immediately ABOVE the line
            # Logic: The bevel blur creates a light gray halo (~200) distinct from white paper (255)
            context = center_col[max(0, y_start-3):y_start]
            if len(context) == 0: continue
            mean_val = np.mean(context)
            
            if 150 < mean_val < 235:
                detections['top_y'] = int(y_start)
                break # Found the top border

    # --- 2. Right Border Check (The Highlight + Asymmetry) ---
    # We scan the center row for a vertical black line with an "Outer Highlight".
    center_row = img[h // 2, :]
    black_pixels_x = np.where(center_row < 80)[0]
    
    if len(black_pixels_x) > 0:
        segments_x = np.split(black_pixels_x, np.where(np.diff(black_pixels_x) > 3)[0] + 1)
        for seg in segments_x:
            if len(seg) < 2: continue
            x_start, x_end = seg[0], seg[-1]
            
            # Context Sampling: 
            # INNER (Left) = Inside the box
            # OUTER (Right) = The bevel highlight
            inner_val = np.mean(center_row[max(0, x_start-4):x_start])
            outer_val = np.mean(center_row[x_end+1:min(w, x_end+5)])
            
            # Logic A: Outer Highlight Signature
            # The bevel always casts a specific gray shadow/highlight ~169
            has_highlight = (155 < outer_val < 185)
            
            # Logic B: Asymmetry Check (Anti-False-Positive)
            # True Box: White Inside (245) vs Gray Outside (169) -> High Contrast
            # False Header: Gray Inside (197) vs Gray Outside (171) -> Low Contrast
            contrast = inner_val - outer_val
            is_high_contrast = contrast > 30
            
            if has_highlight and is_high_contrast:
                detections['right_x'] = int(x_start)
                break # Found the right border

    # Only return if BOTH specific borders are identified
    if 'top_y' in detections and 'right_x' in detections:
        return detections
    
    return None # No valid box found
