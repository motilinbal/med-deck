import cv2
import os
import numpy as np

def get_table_boundaries(image_path, debug=False):
    """
    Detects the main table in a document image using morphological operations.
    Returns: (x, y, w, h) of the table boundary.
    """
    # 1. Load Image
    image = cv2.imread(image_path)
    if image is None:
        raise ValueError(f"Could not open or find the image: {image_path}")
        
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    
    # 2. Adaptive Thresholding
    # Inverts image: Text/Lines become White, Background becomes Black
    # We use adaptive to handle potential shadows in scanned documents
    thresh = cv2.adaptiveThreshold(
        gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
        cv2.THRESH_BINARY_INV, 11, 2
    )

    # 3. Define Kernels for Morphological Operations
    # The scale factors (img_w // 30) ensure the code works on both 
    # low-res screenshots and high-res scans.
    h, w = image.shape[:2]
    
    # Kernel for horizontal lines (wide and short)
    horiz_kernel_len = max(1, w // 30) 
    horiz_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (horiz_kernel_len, 1))

    # Kernel for vertical lines (tall and narrow)
    vert_kernel_len = max(1, h // 30)
    vert_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, vert_kernel_len))

    # 4. Extract Lines
    # Erode captures the structure, Dilate restores the thickness
    
    # Detect Horizontal Lines
    img_bin_h = cv2.erode(thresh, horiz_kernel, iterations=1)
    img_bin_h = cv2.dilate(img_bin_h, horiz_kernel, iterations=1)
    
    # Detect Vertical Lines
    img_bin_v = cv2.erode(thresh, vert_kernel, iterations=1)
    img_bin_v = cv2.dilate(img_bin_v, vert_kernel, iterations=1)

    # 5. Combine and Connect
    # Add horizontal and vertical masks
    mask = cv2.add(img_bin_h, img_bin_v)
    
    # Dilate the combined mask slightly to close gaps at intersections
    # This ensures the header row connects to the body even if lines are thin
    joint_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    mask = cv2.dilate(mask, joint_kernel, iterations=1)
    
    # 6. Find Contours
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if not contours:
        return None

    # 7. Select the Table
    # We assume the table is the largest rectangular structure on the page.
    # We filter out noise by requiring a minimum area.
    table_candidates = []
    
    for cnt in contours:
        x, y, cw, ch = cv2.boundingRect(cnt)
        area = cw * ch
        
        # Filter: Area must be > 0.5% of total image area to be considered a table
        if area > (w * h * 0.005):
            table_candidates.append((x, y, cw, ch, area))
            
    if not table_candidates:
        return None

    # Sort by area (largest first) and pick the top one
    table_candidates.sort(key=lambda x: x[4], reverse=True)
    best_table = table_candidates[0]
    
    final_x, final_y, final_w, final_h, _ = best_table

    # (Optional) Debug visualization
    if debug:
        debug_img = image.copy()
        cv2.rectangle(debug_img, (final_x, final_y), 
                      (final_x + final_w, final_y + final_h), (0, 0, 255), 3)
        # Show specific mask steps if needed for tuning
        cv2.imshow("Mask", mask) 
        cv2.imshow("Detected Table", debug_img)
        cv2.waitKey(0)
        cv2.destroyAllWindows()

    return final_x, final_y, final_w, final_h


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
    result = get_table_boundaries(image_path, debug=False)
    
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


# --- Example Usage ---

# Convert relative path to absolute path
file_path = '/test_data/table3.png'
file_path = os.path.abspath('.') + file_path 
try:
    x, y, w, h = get_table_boundaries(file_path, debug=False)
    print(f"Table found at: x={x}, y={y}, w={w}, h={h}")
except Exception as e:
    print(e)

# Or use the visualization function:
visualize_table_boundary(file_path)