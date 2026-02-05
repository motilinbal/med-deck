import cv2
import os
import numpy as np
import matplotlib.pyplot as plt
import fitz  # PyMuPDF
from PIL import Image

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






def detect_beveled_box(img_array, page_num):
    """
    Debug version of the detection logic. 
    Prints exactly what values are being read from the image.
    """
    if img_array is None: return None
    
    # Convert to grayscale for analysis
    gray = cv2.cvtColor(img_array, cv2.COLOR_BGR2GRAY)
    h, w = gray.shape
    detections = {}
    
    print(f"\n--- Analyzing Page {page_num} ---")

    # 1. Top Border Check
    center_col = gray[:, w // 2]
    black_pixels_y = np.where(center_col < 80)[0]
    
    found_top = False
    if len(black_pixels_y) > 0:
        segments = np.split(black_pixels_y, np.where(np.diff(black_pixels_y) > 3)[0] + 1)
        for seg in segments:
            if len(seg) < 2: continue
            y_start = seg[0]
            
            # Debug: Read the context values
            context = center_col[max(0, y_start-3):y_start]
            if len(context) == 0: continue
            mean_val = np.mean(context)
            
            # Check logic
            if 150 < mean_val < 235:
                print(f"  [TOP CANDIDATE] Y={y_start}: MATCH! Halo Value={mean_val:.1f}")
                detections['top_y'] = int(y_start)
                found_top = True
                break
            else:
                 # Print near-misses to diagnose Gamma issues
                 if 100 < mean_val < 250:
                     print(f"  [Top Reject] Y={y_start}: Halo Value={mean_val:.1f} (Target 150-235)")

    # 2. Right Border Check
    center_row = gray[h // 2, :]
    black_pixels_x = np.where(center_row < 80)[0]
    
    found_right = False
    if len(black_pixels_x) > 0:
        segments_x = np.split(black_pixels_x, np.where(np.diff(black_pixels_x) > 3)[0] + 1)
        for seg in segments_x:
            if len(seg) < 2: continue
            x_start, x_end = seg[0], seg[-1]
            
            inner_val = np.mean(center_row[max(0, x_start-4):x_start])
            outer_val = np.mean(center_row[x_end+1:min(w, x_end+5)])
            
            contrast = inner_val - outer_val
            
            # Debug: Check Highlight Value
            if 140 < outer_val < 200: # Wide range for debug
                status = "FAIL"
                if 155 < outer_val < 185 and contrast > 30: status = "MATCH"
                
                print(f"  [RIGHT CANDIDATE] X={x_start}: {status}")
                print(f"    -> Outer Highlight: {outer_val:.1f} (Target 155-185)")
                print(f"    -> Inner Contrast:  {contrast:.1f} (Target > 30)")
                
                if status == "MATCH":
                    detections['right_x'] = int(x_start)
                    found_right = True
                    break

    if found_top and found_right:
        return detections
    return None

def process_pdf_debug(pdf_path, dpi=300):
    doc = fitz.open(pdf_path)
    
    for page_num in range(len(doc)):
        page = doc[page_num]
        
        # 1. Render exactly as before
        zoom = dpi / 72
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        
        # 2. Careful conversion to numpy
        img_data = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
        if pix.n == 3: # RGB
            img_bgr = cv2.cvtColor(img_data, cv2.COLOR_RGB2BGR)
        else:
            img_bgr = cv2.cvtColor(img_data, cv2.COLOR_GRAY2BGR) # Handle greyscale PDFs
            
        # 3. Run Debug Analysis
        detect_beveled_box_debug(img_bgr, page_num + 1)

