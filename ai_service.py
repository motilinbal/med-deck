import os
import logging
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("MedDeckAI")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def get_gemini_client():
    try:
        return genai.Client(api_key=GEMINI_API_KEY)
    except Exception as e:
        logger.error(f"Failed to initialize Gemini Client: {e}")
        raise

def get_safety_settings():
    """BLOCK_NONE is crucial for medical contexts."""
    return [
        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
    ]

async def process_transcript_with_gemini(current_note: str, new_transcript: str) -> str:
    """
    Takes the existing medical note (context) and the new raw transcript.
    Returns a unified, updated medical note in English.
    """
    client = get_gemini_client()
    
    # SYSTEM PROMPT: The core instructions for the "Scribe" persona
    system_instruction = """
    You are an expert AI Medical Scribe. Your task is to maintain a high-quality, professional medical record.
    
    INPUT DATA:
    1. "Current Note": The existing medical history for this session (if any).
    2. "New Raw Transcript": A possibly messy, mixed-language (Hebrew/English) real-time transcription of a conversation or dictation.

    YOUR GOAL:
    Merge the "New Raw Transcript" into the "Current Note" to produce a single, cohesive, up-to-date English medical record.
    
    GUIDELINES:
    1. **Translation**: The output must be entirely in professional Medical English. Translate any Hebrew or slang.
    2. **Correction**: Fix transcription errors based on medical context (e.g., "hyper tension" -> "hypertension").
    3. **Organization**: 
       - If the input is a conversation, identify "Physician" vs "Patient" roles.
       - Use professional formatting (HPI, ROS, Plan) if the content fits those categories.
       - If it's a narrative, keep it flowing logically.
    4. **Consolidation**: Do not just append. Integrate the information. If the new text corrects the old text, update it.
    
    OUTPUT:
    Return ONLY the updated text of the medical note. Do not add markdown like ```json or intro text.
    """

    prompt = f"""
    --- CURRENT NOTE START ---
    {current_note if current_note else "(No previous notes)"}
    --- CURRENT NOTE END ---

    --- NEW RAW TRANSCRIPT START ---
    {new_transcript}
    --- NEW RAW TRANSCRIPT END ---
    
    Please provide the updated medical note:
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                safety_settings=get_safety_settings(),
                temperature=0.1, # Low temp for factual consistency
            )
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        return f"Error processing note: {e}"