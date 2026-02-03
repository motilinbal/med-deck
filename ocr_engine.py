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
MODEL_ID = "gemini-3.0-flash"

# -------------------------------------------------------------------------
# System Prompt (Derived from schema_implementation.md Section 6.1)
# -------------------------------------------------------------------------
SYSTEM_PROMPT = """
You are an expert Clinical Data Extraction Engine specializing in FHIR R4 standards.
Your task is to extract structured data from the provided medical report text/image and output a valid JSON object matching the DiagnosticReport and Observation schemas.

### 1. EXTRACTION RULES
* **Normalization:** Map all test names to LOINC codes where possible. If the text says "Glucose", output the LOINC 2345-7 in the coding array.
* **Microbiology Hierarchy:**
  * The organism identified (e.g., "E. Coli") is the PARENT Observation.
  * The antibiotic susceptibilities (e.g., "Ampicillin: R") are COMPONENT Observations nested inside the parent.
* **Narrative Segmentation:** For Radiology/Pathology, break long text into sections (Findings, Impression) and map them to component observations with appropriate LOINC section codes.
* **Handling Mixed Flora:**
  * If the report states "Mixed Flora" or "Contaminated", do NOT invent an organism hierarchy.
  * Create a single Observation with code = "Bacteria identified in Urine" (LOINC) and valueCodeableConcept = "Mixed flora" (SNOMED: 442655002).
  * Set status to "final".
* **Biopsy Specimens:**
  * If the report lists "Part A" and "Part B", create distinct Specimen references. Link the specific microscopic findings to the correct specimen via the specimen reference field in the Observation.

### 2. OUTPUT FORMAT (JSON ONLY)
Return a single JSON object containing a list of resources:
{
  "resourceType": "Bundle",
  "entry": [
    { "resource": { ... DiagnosticReport ... } },
    { "resource": { ... Observation ... } }
  ]
}
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

def extract_data_from_image(image_path: str) -> str:
    """
    Sends an image to Gemini-3.0-Flash for FHIR-structured extraction.

    Args:
        image_path: Path to the local image file (JPG/PNG/PDF).

    Returns:
        str: Raw JSON string response from the model.
    """
    client = get_gemini_client()
    
    # Read image file
    try:
        with open(image_path, "rb") as f:
            image_bytes = f.read()
    except FileNotFoundError:
        logger.error(f"File not found: {image_path}")
        raise

    # Create the Content object
    # Note: google-genai handles generic 'image/*' MIME types automatically 
    # when passing bytes, or we can use types.Part.
    
    # Determine basic mime type (simplified)
    mime_type = "application/pdf" if image_path.lower().endswith(".pdf") else "image/jpeg"
    
    logger.info(f"Sending {image_path} to {MODEL_ID} for extraction...")

    try:
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=[
                types.Content(
                    role="user",
                    parts=[
                        types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
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