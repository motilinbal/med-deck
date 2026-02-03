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
    return [
        types.SafetySetting(category="HARM_CATEGORY_HARASSMENT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_HATE_SPEECH", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_SEXUALLY_EXPLICIT", threshold="BLOCK_NONE"),
        types.SafetySetting(category="HARM_CATEGORY_DANGEROUS_CONTENT", threshold="BLOCK_NONE"),
    ]

async def process_transcript_with_gemini(current_note: str, new_transcript: str) -> str:
    client = get_gemini_client()
    
    # SYSTEM PROMPT: Strict Persona & Formatting Rules
    system_instruction = """
    You are an expert Medical Scribe.
    
    TASK:
    1. Read the <current_note> (context) and <new_transcript> (raw dictation).
    2. Merge the new information into the note.
    3. **TRANSLATE EVERYTHING TO PROFESSIONAL ENGLISH.** (The input may be Hebrew/mixed).
    4. Correct medical terminology and transcription errors.
    5. Output the result as a clean, formatted medical narrative.
    
    CRITICAL RULES:
    - OUTPUT ONLY THE FINAL ENGLISH TEXT. Do not output markdown code blocks (```), headers, or comments.
    - If the input is Hebrew, translate it accurately to English medical terms.
    - Do not repeat the input tags in your output.
    """

    # USER PROMPT: XML Tags for clear separation
    prompt = f"""
    <current_note>
    {current_note if current_note else "(No previous notes)"}
    </current_note>

    <new_transcript>
    {new_transcript}
    </new_transcript>
    
    Please provide the updated, English medical note:
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction, # Move instructions here
                safety_settings=get_safety_settings(),
                temperature=0.1, 
            )
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini Error: {e}")
        return f"Error processing note: {e}"