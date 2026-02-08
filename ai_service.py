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

async def refine_input_transcript(new_transcript: str, chat_history: list) -> str:
    """
    Refines raw dictation into a professional medical 'User Message'.
    
    Args:
        new_transcript: Raw text from speech recognition (may be Hebrew/English/mixed)
        chat_history: List of chat message dicts from the DB (will be filtered to user/assistant only)
    
    Returns:
        Refined, professional English text representing what the user intended to say.
    """
    client = get_gemini_client()
    
    # Build context string from chat history - ONLY user and assistant roles
    context_lines = []
    for msg in chat_history:
        role = msg.get("role", "")
        # Strict filter: only include user and assistant messages
        if role in ("user", "assistant"):
            content = msg.get("content", "")
            # Capitalize role for display
            role_label = "Doctor" if role == "user" else "Assistant"
            context_lines.append(f"{role_label}: {content}")
    
    context_string = "\n".join(context_lines) if context_lines else "(No previous conversation)"
    
    # SYSTEM PROMPT: Scribe Persona - Translation & Refinement Only
    system_instruction = """
    You are an expert Medical Scribe.

    YOUR TASK:
    You will receive a history of a medical consultation and a new raw transcript.
    The raw transcript may be in Hebrew, English, or mixed languages.
    Your job is to translate and refine the NEW TRANSCRIPT into professional Medical English.

    CRITICAL CONSTRAINTS:
    - Do NOT answer the user's questions.
    - Do NOT act as a medical consultant or provide medical advice.
    - Do NOT generate a full note or summary.
    - Just output the refined, translated text of what the user SAID.
    - Preserve the meaning and intent of the original dictation.
    - Use proper medical terminology.
    - Output ONLY the refined text, no markdown code blocks, headers, or explanations.

    EXAMPLE:
    Raw input: "Patient has been having chest pain for two days, also reports some parasternal discomfort"
    Your output: "The patient reports chest pain persisting for the past two days, accompanied by parasternal discomfort."
    """

    # USER PROMPT: Context + New Input
    prompt = f"""
    <conversation_history>
    {context_string}
    </conversation_history>

    <new_transcript>
    {new_transcript}
    </new_transcript>

    Refine the new transcript into professional Medical English, preserving the user's intent:
    """

    try:
        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_instruction,
                safety_settings=get_safety_settings(),
                temperature=0.1,
            )
        )
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini Scribe Error: {e}")
        return f"Error refining transcript: {e}"