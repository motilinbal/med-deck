import os
import logging
from google import genai
from google.genai import types
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("MedDeckAI")

# Configure API Key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

def get_gemini_client():
    """Initializes and returns the authenticated Gemini Client."""
    try:
        if not GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY not found in environment variables")
        client = genai.Client(api_key=GEMINI_API_KEY)
        return client
    except Exception as e:
        logger.error(f"Failed to initialize Gemini Client: {e}")
        raise

def get_safety_settings():
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

async def analyze_transcript(raw_history: str):
    """
    Sends the raw transcript history to Gemini 2.5 Flash for structuring.
    """
    client = get_gemini_client()
    
    # The System Prompt: Defines the persona and the transformation rules
    system_instruction = """
    You are an expert medical scribe and clinical documentation specialist.
    Your input is a raw, unstructured audio transcript of a medical encounter.
    The transcript may be in Hebrew, English, or a mix of both.
    
    Your Goal:
    Transform this transcript into a highly organized, readable clinical narrative in ENGLISH.
    
    Rules:
    1. Output Language: ENGLISH ONLY.
    2. Speaker Identification: Infer who is speaking (Physician vs Patient) based on context.
       - Use "Physician:" and "Patient:" labels if it's a dialogue.
       - Or summarize as a narrative (e.g., "Patient reports...") if appropriate.
    3. Style: Professional, "book-like" clinical prose. Fix grammar and remove stuttering.
    4. Accuracy: Capture all medical facts (symptoms, duration, history) accurately.
    5. Formatting: Use bullet points or short paragraphs for readability.
    
    Structure the output into these sections if applicable:
    - Subjective (Patient History)
    - Objective (If any observations were mentioned)
    - Assessment/Plan (If discussed)
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash", # Or "gemini-1.5-flash" if 2.0 isn't available to you yet
            contents=raw_history,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                safety_settings=get_safety_settings(),
                temperature=0.3, # Low temp for factual accuracy
            )
        )
        return response.text
    except Exception as e:
        logger.error(f"Gemini Analysis Failed: {e}")
        return f"Error processing note: {str(e)}"