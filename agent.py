import os
import logging
from google import genai
from google.genai import types
import database as db
from tools import my_tool_list
from bson.objectid import ObjectId

# Import for chat message handling
from models import ChatMessage, MessageRole

logger = logging.getLogger("MedDeckAgent")

MAX_TURNS = 10
WARNING_TURN = 8

async def generate_trace_summary(run_id: str) -> str:
    """
    Generates an organized and readable summary of the trace events.
    """
    trace = await db.traces_collection.find_one({"_id": ObjectId(run_id)})
    if not trace:
        return "No trace data available."
    
    events = trace.get("events", [])
    summary_parts = []
    
    summary_parts.append("=== EXECUTION SUMMARY ===")
    summary_parts.append(f"Total steps taken: {len(events)}")
    summary_parts.append("")
    
    tool_calls = []
    for i, event in enumerate(events, 1):
        role = event.get("role", "unknown")
        content = event.get("content", "")
        tool_info = event.get("tool_info")
        
        if role == "user":
            summary_parts.append(f"Step {i}: User Request")
            summary_parts.append(f"  {content}")
            summary_parts.append("")
        elif role == "model_call":
            tool_name = tool_info.get("name", "unknown") if tool_info else "unknown"
            tool_args = tool_info.get("args", {}) if tool_info else {}
            summary_parts.append(f"Step {i}: Tool Call - {tool_name}")
            summary_parts.append(f"  Arguments: {tool_args}")
            tool_calls.append(f"  - {tool_name}")
            summary_parts.append("")
        elif role == "tool_result":
            summary_parts.append(f"Step {i}: Tool Result")
            # Truncate long results for readability
            if len(str(content)) > 200:
                summary_parts.append(f"  {str(content)[:200]}...")
            else:
                summary_parts.append(f"  {content}")
            summary_parts.append("")
        elif role == "system_injection":
            summary_parts.append(f"Step {i}: System Message")
            summary_parts.append(f"  {content}")
            summary_parts.append("")
    
    summary_parts.append("=== TOOLS USED ===")
    if tool_calls:
        for call in tool_calls:
            summary_parts.append(call)
    else:
        summary_parts.append("No tools were called.")
    
    return "\n".join(summary_parts)


async def run_agent(card_id: str, chat_history: list) -> str:
    """
    The Medical Agent - reasons about user input and uses tools.
    
    Args:
        card_id: The ID of the current card (for tool context and info logging)
        chat_history: List of chat message dicts from the DB (will be filtered to user/assistant only)
    
    Returns:
        The final assistant response text. The caller is responsible for saving to DB.
    """
    
    # 1. Setup Context - Fetch card for patient context
    card = await db.cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        return "Error: Patient card not found."
    
    # Build patient context from chat history (fallback for legacy cards with processed_note)
    patient_context = card.get('processed_note', '')
    if not patient_context and chat_history:
        # For new cards, we rely on chat history - no separate context needed
        patient_context = "See conversation history below."
    
    system_instruction = f"""
    You are a Medical Clinical Case Manager.
    Patient Context: {patient_context if patient_context else 'No prior context available.'}
    
    Your goal is to answer the user's request accurately using your tools.
    Never hallucinate medical data. If you don't know, use a tool or ask.
    Be concise and professional in your responses.
    """

    # 2. Initialize DB Trace
    # Get the latest user message as the prompt for tracing
    latest_user_msg = ""
    for msg in reversed(chat_history):
        if msg.get("role") == "user":
            latest_user_msg = msg.get("content", "")
            break
    
    run_id = await db.create_trace_run(card_id, latest_user_msg)
    
    # 3. Build Gemini History from chat_history
    # Convert DB format to Gemini format, filtering to only user/assistant roles
    gemini_history = []
    
    for msg in chat_history:
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        # Strict filter: only include user and assistant messages
        if role == "user":
            gemini_history.append({"role": "user", "parts": [content]})
            await db.log_trace_event(run_id, "user", content)
        elif role == "assistant":
            # Gemini uses "model" for assistant messages
            gemini_history.append({"role": "model", "parts": [content]})
            await db.log_trace_event(run_id, "model", content)
        # Skip log, info, error - these are invisible to the AI

    # 4. The ReAct Loop
    client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
    
    # Track previous tool calls for deduplication
    previous_tool_calls_set = set()
    
    for turn in range(MAX_TURNS):
        # --- "Soft Limit" Injection ---
        if turn == WARNING_TURN:
            warning_msg = (
                "SYSTEM MONITOR: You are approaching the computation limit. "
                "Do NOT call any more tools. "
                "Synthesize the data you have collected so far and provide your final response immediately."
            )
            # We inject this as a 'user' message so the model sees it as a new constraint
            gemini_history.append({"role": "user", "parts": [warning_msg]})
            await db.log_trace_event(run_id, "system_injection", "Sent 'Hurry Up' warning")
        
        # A. Call Model
        try:
            response = client.models.generate_content(
                model='gemini-2.0-flash',
                contents=gemini_history,
                config=types.GenerateContentConfig(
                    tools=my_tool_list,
                    system_instruction=system_instruction,
                    temperature=0.1
                )
            )
        except Exception as e:
            logger.error(f"Gemini API error: {e}")
            await db.complete_trace_run(run_id, f"Error: {str(e)}", status="failed")
            return "System Error during AI reasoning."

        # B. Analyze Response
        candidate = response.candidates[0]
        
        # Case 0: Graceful Exit at MAX_TURNS - 1 if model wants to call a tool
        if turn == MAX_TURNS - 1 and candidate.content.parts and candidate.content.parts[0].function_call:
            summary = await generate_trace_summary(run_id)
            graceful_exit_msg = (
                "I have gathered significant data, but I reached my safety limit before concluding perfectly. "
                "Here is what I know so far:\n\n" + summary
            )
            await db.complete_trace_run(run_id, graceful_exit_msg, status="completed")
            return graceful_exit_msg
        
        # Case 1: Model wants to call a tool (Function Call)
        if candidate.content.parts and candidate.content.parts[0].function_call:
            
            # Get the call details
            fc = candidate.content.parts[0].function_call
            tool_name = fc.name
            tool_args = dict(fc.args)
            
            # 1. Log the "Thought/Action" to DB
            await db.log_trace_event(run_id, "model_call", f"Calling {tool_name}", tool_call_info={"name": tool_name, "args": tool_args})
            
            # 2. *** INFO FEEDBACK LOOP ***
            # Emit an "info" message to the chat so the user sees progress
            info_message = f"🔍 Consulting: {tool_name.replace('_', ' ').title()}..."
            try:
                await db.append_chat_message(card_id, MessageRole.INFO, info_message)
            except Exception as e:
                logger.warning(f"Failed to emit info message: {e}")
            
            # 3. Append the Model's "Request" to gemini_history (Required by API)
            gemini_history.append(candidate.content)
            
            # 4. DEDUPLICATION CHECK - Prevent repeating the same tool call
            current_tool_call = (tool_name, str(tool_args))
            
            if current_tool_call in previous_tool_calls_set:
                tool_result = "SYSTEM ERROR: You just called this tool with these exact arguments. Try a different query or stop."
            else:
                previous_tool_calls_set.add(current_tool_call)
                # EXECUTE THE TOOL (The "Act" phase) - pass card_id for context
                tool_result = await execute_tool_router(tool_name, tool_args, card_id)
            
            # 5. Log the "Result" to DB
            await db.log_trace_event(run_id, "tool_result", tool_result)
            
            # 6. Append "Result" to gemini_history
            gemini_history.append({
                "role": "function",
                "parts": [{
                    "function_response": {
                        "name": tool_name,
                        "response": {"result": tool_result}
                    }
                }]
            })
            
            # Loop continues... Model will see the result in next turn.
            
        # Case 2: Model returned text (Final Answer)
        else:
            final_text = candidate.content.parts[0].text
            
            # Log Final Answer to DB trace
            await db.complete_trace_run(run_id, final_text)
            
            # Return to caller - they will save to chat as "assistant" message
            return final_text

    return "Error: Agent reached maximum iteration limit."

# --- Helper Router ---
async def execute_tool_router(name: str, args: dict, card_id: str = None) -> str:
    """
    Maps string names to actual python functions.
    
    Args:
        name: The name of the tool to execute
        args: Dictionary of arguments to pass to the tool
        card_id: The current card ID (passed to tools that need patient context)
    
    Returns:
        The tool result as a string
    """
    # Import tools module here to avoid circular imports
    import tools
    
    if name == "get_lab_results":
        # Pass card_id if the tool needs patient context
        return await tools.get_lab_results(card_id=card_id, **args)
    elif name == "search_internet":
        return await tools.google_search(**args)
    elif name == "search_guidelines":
        # Example: a tool that searches clinical guidelines
        return await tools.search_guidelines(**args)
    # Add more tools as they are implemented
    
    return f"Error: Tool '{name}' not found."