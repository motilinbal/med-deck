import os
import logging
import base64
from typing import Optional
from google import genai
from google.genai import types
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Constants
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_ID = "gemini-3-flash-preview"

# ---------------
# System Prompt 
# ---------------
SYSTEM_PROMPT = """
Act as a specialized Medical Data Extraction Engine. Your goal is to convert complex medical documents (lab results, pathology reports, imaging, and clinical summaries) into a highly structured, parseable JSON format suitable for a longitudinal patient database.
Please follow these strict operational guidelines:
1. **Comprehensive Extraction:** Do not summarize or truncate reports. Capture the entirety of the 'Findings' and 'Interpretations' sections in addition to the 'Conclusions.' Every clinical observation, anatomical detail, and descriptive nuance must be preserved.
2. **Professional Translation:** Automatically translate all Hebrew content—including test names, organ sites, clinical indications, and descriptive findings—into standardized, professional medical English. Ensure that the terminology used is consistent with international medical standards (e.g., SNOMED CT or LOINC terminology).
3. **Data Hierarchy & Categorization:** Organize the JSON array by clinical categories (e.g., 'Biochemistry', 'Hematology', 'Microbiology', 'Imaging', 'Pathology'). 
   - For Lab Results: Capture Date, Time, Material, Test Name, Result Value, Units, Reference Range, and any Flags or Alerts.
   - For Narrative Reports (Imaging/Pathology): Capture Metadata (Date, ID, Provider), Clinical Indication, Comparison to previous studies, detailed Findings (broken down by organ/system where applicable), and Final Conclusion.
4. **Temporal Integrity:** Ensure that every observation is correctly mapped to its specific date and time of collection to allow for trend analysis in a database environment.
5. **Output Requirements:** Generate only a single, valid, parseable JSON array. Use double quotes for all strings. Do not include comments, explanations, or text outside of the JSON block. If data is missing for a specific field, use null.
"""

def get_gemini_client() -> genai.Client:
    """Initializes and returns the authenticated Gemini Client."""
    try:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY not found in environment variables")
        return genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        logger.error(f"Failed to initialize Gemini Client: {e}")
        raise

def get_safety_settings() -> list[types.SafetySetting]:
    """Returns standard medical safety configuration (BLOCK_NONE)."""
    return [
        types.SafetySetting(
            category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"
        ),
        types.SafetySetting(
            category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"
        ),
    ]

def extract_data_from_file(file_path: str) -> str:
    """
    Sends an image or PDF to Gemini-3.0-Flash for structured extraction.

    Args:
        file_path: Path to the local file (JPG/PNG/PDF).

    Returns:
        str: Raw JSON string response from the model.
    """
    client = get_gemini_client()
    
    # Read file
    try:
        with open(file_path, "rb") as f:
            file_bytes = f.read()
    except FileNotFoundError:
        logger.error(f"File not found: {file_path}")
        raise

    # Create the Content object
    # Note: google-genai handles generic 'image/*' MIME types automatically
    # when passing bytes, or we can use types.Part.
    
    # Determine basic mime type (simplified)
    mime_type = "application/pdf" if file_path.lower().endswith(".pdf") else "image/jpeg"
    
    logger.info(f"Sending {file_path} to {MODEL_ID} for extraction...")

    try:
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=file_bytes, mime_type=mime_type),
                        types.Part.from_text(text=SYSTEM_PROMPT)
                    ]
                )
            ],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                safety_settings=get_safety_settings(),
                temperature=0.1, # Low temperature for factual extraction
            )
        )
        
        logger.info("Extraction complete.")
        return response.text

    except Exception as e:
        logger.error(f"Gemini Inference Failed: {e}")
        raise

if __name__ == "__main__":
    # Quick test
    import sys
    if len(sys.argv) > 1:
        print(extract_data_from_image(sys.argv[1]))
    else:
        print("Usage: python ocr_engine.py <path_to_image>")