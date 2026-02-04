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
SYSTEM_PROMPT_QUANT = """
You are a specialized Medical Data Extraction Engine and Translator. Your mandate is to convert a table of medical test results (Hebrew/English) into a strict, schema-conformant JSON array for a longitudinal SQL database.

### CORE OPERATIONAL DIRECTIVES
1. **Translation:** Automatically translate ALL Hebrew text (test names) into professional medical English. Map terms to standard LOINC/SNOMED nomenclature.
2. **Output Format:** Return ONLY a valid JSON array containing objects that strictly adhere to the 2 defined schemas below. Do not include markdown formatting, preambles, or explanations.
3. **Report non-table:** If the data you get is a Microbiology, Pathology, or Imaging report, strictly return `{"category": "non-table"}`.

### DATA SCHEMA DEFINITIONS
You must categorize every extracted data point from the table into one of the following 2 formats.
**Optimization Rule:** Whenever multiple tests share the same Date, Time, and Material, you MUST use **Format B (Grouped)** to save tokens.

**Format A: Single Observation** (Use for isolated tests or when distinct notes apply to a single test)
{
	  "category": "Quantitative",
          "date": "DD/MM/YY format",
          "time": "HH:MM format",
          "material": "The specimen type (e.g., Venous Blood, Urine, Pleural Fluid).",
          "test_name": "Official English LOINC medical name",
          "value": "String, the actual result. Can be numeric ("127", "10.4", "< 2.4") or textual ('positive', 'negative', ratios, and so on)",
          "note": "Some remarks related to the sample, if included in the report"
}

**Format B: Grouped Panel** (PREFERRED for all panels like CBC, CMP, etc.)
{
  "category": "Quantitative",
  "date": "DD/MM/YY",
  "time": "HH:MM",
  "material": "The specimen type (e.g., Venous Blood, Urine, Pleural Fluid).",
  "results": {
    "Test_Name_1": "Value",  // Test_Name is the Official English LOINC medical name
    "Test_Name_2": "Value",  // The Value can be numeric ("127", "10.4", "< 2.4") or textual ('positive', 'negative', ratios, and so on)
    "Test_Name_3": "< 0.05"  // Combine operator and value into string here
  },
  "note": "General remark for the whole sample (e.g., 'Hemolysis present')"
}
"""

SYSTEM_PROMPT_NARRATIVE = """
You are a specialized Medical Data Extraction Engine and Translator. Your mandate is to convert mixed-format medical documents (Hebrew/English) into a strict, schema-conformant JSON array for a longitudinal SQL database.

### CORE OPERATIONAL DIRECTIVES
1. **Translation:** Automatically translate ALL Hebrew text (clinical indications, anatomy, findings, test names) into professional medical English. Map terms to standard LOINC/SNOMED nomenclature.
2. **No Summarization:** In Narrative categories (Pathology, Imaging), you must capture the FULL text of findings. Do not abbreviate descriptions of microscopic findings or anatomical observations.
3. **Output Format:** Return ONLY a valid JSON array containing objects that strictly adhere to the 5 defined schemas below. Do not include markdown formatting, preambles, or explanations.

### DATA SCHEMA DEFINITIONS
You must categorize every extracted data point into one of the following 5 categories.

#### 1. CATEGORY: "Quantitative"
Used for: Biochemistry, Hematology, Hormones, Blood Gases, POCT, Cardiac Markers, etc.
**Optimization Rule:** Whenever multiple tests share the same Date, Time, and Material, you MUST use **Format B (Grouped)** to save tokens.

**Format A: Single Observation** (Use for isolated tests or when distinct notes apply to a single test)
{
	  "category": "Quantitative",
          "date": "DD/MM/YY format",
          "time": "HH:MM format",
          "material": "The specimen type (e.g., Venous Blood, Urine, Pleural Fluid).",
          "test_name": "Official English LOINC medical name",
          "value": "String, the actual result. Can be numeric ("127", "10.4", "< 2.4") or textual ('positive', 'negative', ratios, and so on)",
          "note": "Some remarks related to the sample, if included in the report"
}

**Format B: Grouped Panel** (PREFERRED for all panels like CBC, CMP, etc.)
{
  "category": "Quantitative",
  "date": "DD/MM/YY",
  "time": "HH:MM",
  "material": "The specimen type (e.g., Venous Blood, Urine, Pleural Fluid).",
  "results": {
    "Test_Name_1": "Value",  // Test_Name is the Official English LOINC medical name
    "Test_Name_2": "Value",  // The Value can be numeric ("127", "10.4", "< 2.4") or textual ('positive', 'negative', ratios, and so on)
    "Test_Name_3": "< 0.05"  // Combine operator and value into string here
  },
  "note": "General remark for the whole sample (e.g., 'Hemolysis present')"
}

#### 2. CATEGORY: "Reference"
Used for: Defining the normal ranges found in the document.
- **Rules:** Extract this ONCE per test type per document. Ensure 'test_name' matches the 'Quantitative' entry exactly to allow for database joining.
{
  "category": "Reference",
  "test_name": "Standardized English LOINC name (must match Quantitative entry)",
  "low_value": Number or null,
  "high_value": Number or null,
  "units": "e.g., mg/dL, mmol/L, g/g"
}

#### 3. CATEGORY: "Microbiology"
Used for: Cultures and sensitivity analyses.
- **Rules:** Nest organisms and their specific antibiotic sensitivities.
{
  "category": "Microbiology",
  "date": "DD/MM/YY",
  "time": "HH:MM",
  "material": "Specimen type (e.g., Sputum, Rectal Swab, Pleural Fluid)",
  "gram_stain": "Full description or null",
  "culture": [
    {
      "name": "Organism name with quantifier (e.g., 'E. Coli ++')",
      "sensitivities": {
        "Antibiotic_Name": "S", // S, R, or I
        "Antibiotic_Name_2": "R"
      }
    }
  ]
}

#### 4. CATEGORY: "Pathology"
Use for: Pathology reports.
- **Rules:** Organize each specimen in its own JSON document.
{
  "category": "Pathology",
  "date": "DD/MM/YY",
  "time": "HH:MM",
  "specimen": "Anatomical site (e.g., Left Pleural Fluid, Stomach Antrum, Esophagus Lower Third)",
  "clinical_data": "Physician's indication/history",
  "macroscopic": "Full text of gross description",
  "microscopic": "Full text of microscopic findings",
  "diagnosis": "Final pathological diagnosis, if incuded"
}

#### 5. CATEGORY: "Imaging"
Used for: CT, MRI, PET-CT, Ultrasound, Echo, and so on.
- **Rules:** The 'findings' object keys should be dynamic based on the report structure (e.g., 'Liver', 'Lungs', 'Bones').
{
  "category": "Imaging",
  "date": "DD/MM/YY",
  "time": "HH:MM",
  "exam_type": "Modality and body part (e.g., CT Chest w/ Contrast)",
  "indication": "Reason for exam",
  "comparison": "Previous studies mentioned (e.g., 'CT from 12/01/23')",
  "findings": {
    "Organ_System_Name": "Full detailed findings for this specific area",
    "Another_System": "Full detailed findings"
  },
  "summary": "The 'Impression' or 'Conclusion' section text"
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

def extract_data_from_file(file_path: str, is_table = False) -> str:
    """
    Sends an image or PDF to Gemini-3.0-Flash for structured extraction.

    Args:
        file_path: Path to the local file (JPG/PNG/PDF).

    Returns:
        str: Raw JSON string response from the model.
    """
    if is_table:
      SYSTEM_PROMPT = SYSTEM_PROMPT_QUANT
    else:
      SYSTEM_PROMPT = SYSTEM_PROMPT_NARRATIVE

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