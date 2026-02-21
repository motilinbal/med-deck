import logging
from google.genai import types

from app.services.gemini_client import get_client, get_safety_settings

logger = logging.getLogger("MedDeckAI")

async def analyze_transcript(raw_history: str):
    """
    Sends the raw transcript history to Gemini 2.5 Flash for structuring.
    """
    client = get_client()
    
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


async def check_duplicate_documents(doc1_str: str, doc2_str: str) -> bool:
    """
    Uses a lightweight LLM to determine if two medical documents refer to the same test/procedure.
    
    This function is specifically designed for duplicate detection of narrative documents
    (Microbiology, Pathology, Imaging) where simple field matching is insufficient.
    
    Args:
        doc1_str: Stringified first document
        doc2_str: Stringified second document
        
    Returns:
        bool: True if documents refer to the same test (duplicate), False if different
    """
    client = get_client()
    
    system_instruction = """
    You are a medical document comparison assistant. Your task is to compare two medical documents
    and determine if they refer to the EXACT same test, procedure, or examination for the same patient.
    
    Guidelines:
    - Return "True" ONLY if the documents are clearly duplicates (same test, same date/time, same findings)
    - Return "False" if they are different tests, different specimens, different time points, or different examinations
    - Be conservative - when in doubt, return "False"
    - Consider: specimen type, anatomical site, test modality, date/time, and content
    
    Respond with ONLY "True" or "False". No explanation needed.
    """
    
    prompt = f"""
    Document 1:
    {doc1_str}

    Document 2:
    {doc2_str}

    Are these the same test/procedure? (True/False)
    """
    
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",  # Lightweight model for cost efficiency
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                safety_settings=get_safety_settings(),
                temperature=0.1,  # Low temperature for consistent results
            )
        )
        
        result = response.text.strip().lower()
        is_duplicate = result == "true"
        
        logger.info(f"Duplicate check result: {is_duplicate}")
        return is_duplicate
        
    except Exception as e:
        logger.error(f"LLM duplicate check failed: {e}")
        # If LLM fails, assume not duplicate to avoid data loss
        return False