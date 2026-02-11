import os, re
import logging
import inspect
import asyncio
from datetime import datetime
from google import genai
from google.genai import types
import database as db
from tools import my_tool_list
from bson.objectid import ObjectId

# Import for chat message handling
from models import ChatMessage, MessageRole

# Import context management for automatic card_id injection
from app.context import active_card_id

# Import TransientLog for self-cleaning status messages
from app.utils.transient import TransientLog
from app.utils import get_israel_date_str, get_israel_time_str

logger = logging.getLogger("MedDeckAgent")

# =============================================================================
# TOOL MAP: Dynamic function lookup for the Centralized Tool Executor
# =============================================================================
# Maps tool function names (strings from LLM) to actual Python function objects.
# This enables dynamic signature inspection and safe argument filtering.

TOOL_MAP = {func.__name__: func for func in my_tool_list}

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
        card_id: The ID of the current card (set in context for tools to access)
        chat_history: List of chat message dicts from the DB (will be filtered to user/assistant only)

    Returns:
        The final assistant response text. The caller is responsible for saving to DB.
    """

    # 0. Validate card_id format BEFORE setting context
    if not card_id or not ObjectId.is_valid(card_id):
        return "System Error: Invalid Card ID provided."

    # 1. Set the card_id in context - tools will retrieve it via get_card_id()
    # This ensures all subsequent tool calls can access the card_id automatically
    token = active_card_id.set(card_id)

    try:
        # 2. Setup Context - Fetch card for patient context
        card = await db.cards_collection.find_one({"_id": ObjectId(card_id)})
        if not card:
            return "Error: Patient card not found."

        # Build patient context from chat history (fallback for legacy cards with processed_note)
        patient_context = card.get("processed_note", "")
        if not patient_context and chat_history:
            # For new cards, we rely on chat history - no separate context needed
            patient_context = "See conversation history below."

        with open("prompts/consultant.md", "r") as f:
            system_instruction = f.read()

        system_instruction += (
            f"\n\nFor your reference, the date today is {get_israel_date_str()} and the time now is {get_israel_time_str()}."
            "\n\nIMPORTANT: When using tools, you MUST emit a native Tool Call. "
            "Do NOT write Python code or Markdown blocks like ```tool_name(...)```. "
            "Just call the tool directly using the provided function interface."
        )

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
        # Using google.genai types for proper serialization
        gemini_history = []

        for msg in chat_history:
            role = msg.get("role", "")
            content = msg.get("content", "")

            # Strict filter: only include user and assistant messages
            if role == "user":
                gemini_history.append(
                    types.Content(role="user", parts=[types.Part(text=content)])
                )
                await db.log_trace_event(run_id, "user", content)
            elif role == "assistant":
                # Gemini uses "model" for assistant messages
                gemini_history.append(
                    types.Content(role="model", parts=[types.Part(text=content)])
                )
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
                gemini_history.append(
                    types.Content(role="user", parts=[types.Part(text=warning_msg)])
                )
                await db.log_trace_event(
                    run_id, "system_injection", "Sent 'Hurry Up' warning"
                )

            # A. Call Model - let exceptions bubble up so caller can handle them properly
            # IMPORTANT: Disable automatic function calling - our manual loop handles async tools
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=gemini_history,
                config=types.GenerateContentConfig(
                    tools=my_tool_list,
                    system_instruction=system_instruction,
                    temperature=0.1,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )

            # B. Analyze Response
            candidate = response.candidates[0]

            # --- ENHANCED DEBUG LOGGING ---
            # Log the raw structure to debug "Inner Thoughts" vs "Tool Calls"
            logger.info(f"Model Response Parts: {len(candidate.content.parts)}")
            for i, part in enumerate(candidate.content.parts):
                if part.text:
                    logger.info(f"Part {i} [TEXT/THOUGHT]: {part.text[:200]}...")
                elif part.function_call:
                    logger.info(f"Part {i} [CALL]: {part.function_call.name}")

            # Case 0: Graceful Exit at MAX_TURNS - 1 if model wants to call a tool
            # Check if ANY part is a function call (handles parallel calls)
            has_function_calls = any(part.function_call for part in candidate.content.parts)
            if (
                turn == MAX_TURNS - 1
                and has_function_calls
            ):
                summary = await generate_trace_summary(run_id)
                graceful_exit_msg = (
                    "I have gathered significant data, but I reached my safety limit before concluding perfectly. "
                    "Here is what I know so far:\n\n" + summary
                )
                await db.complete_trace_run(
                    run_id, graceful_exit_msg, status="completed"
                )
                return graceful_exit_msg

            # Case 1: Model wants to call one or more tools (Function Call(s))
            if has_function_calls:
                
                # 1. Add Model's full message to history (Required by API contract)
                gemini_history.append(candidate.content)
                
                # 2. Collect ALL function calls from all parts
                tool_tasks = []
                call_metadata = []  # Track name/args/id for logging and response mapping
                
                for part in candidate.content.parts:
                    if part.function_call:
                        fc = part.function_call
                        tool_name = fc.name
                        tool_args = dict(fc.args)
                        call_id = getattr(fc, 'id', None)  # Future-proof: capture ID if present
                        
                        # Deduplication check
                        call_signature = (tool_name, str(tool_args))
                        if call_signature in previous_tool_calls_set:
                            # Schedule an error result for this duplicate call
                            # NOTE: Using asyncio.create_task preserves index alignment - CRITICAL
                            async def duplicate_error():
                                return "SYSTEM ERROR: You just called this tool with these exact arguments. Try a different query or stop."
                            task = asyncio.create_task(duplicate_error())
                        else:
                            previous_tool_calls_set.add(call_signature)
                            # Schedule actual tool execution
                            task = asyncio.create_task(execute_tool_router(tool_name, tool_args))
                        
                        tool_tasks.append(task)
                        call_metadata.append({"name": tool_name, "args": tool_args, "id": call_id})
                        
                        # Log each call
                        await db.log_trace_event(
                            run_id,
                            "model_call",
                            f"Calling {tool_name}",
                            tool_call_info={"name": tool_name, "args": tool_args, "id": call_id},
                        )
                
                # 3. Execute ALL tools in parallel with UX-friendly TransientLog
                tool_names = [m["name"] for m in call_metadata]
                if len(tool_names) > 3:
                    display_str = f"{', '.join(tool_names[:2])} and {len(tool_names)-2} others"
                else:
                    display_str = ", ".join(tool_names)
                
                log_msg = f"🔍 Consulting {len(tool_tasks)} sources: {display_str}..."
                
                async with TransientLog(card_id, log_msg):
                    results = await asyncio.gather(*tool_tasks, return_exceptions=True)
                
                # 4. Build response parts for ALL results (must match call count exactly)
                response_parts = []
                for i, result in enumerate(results):
                    # Handle exceptions from parallel execution
                    if isinstance(result, Exception):
                        safe_result = f"Error: {str(result)}"
                        logger.error(f"Tool {call_metadata[i]['name']} failed: {result}")
                    else:
                        safe_result = str(result)
                    
                    # Log result
                    await db.log_trace_event(run_id, "tool_result", safe_result)
                    
                    # Build function response part with ID matching (future-proof)
                    func_response = types.FunctionResponse(
                        name=call_metadata[i]["name"],
                        response={"result": safe_result}
                    )
                    # Only set ID if it was present in the original call
                    if call_metadata[i]["id"] is not None:
                        func_response.id = call_metadata[i]["id"]
                    
                    response_parts.append(types.Part(function_response=func_response))
                
                # 5. Append SINGLE message with ALL function responses
                gemini_history.append(types.Content(role="tool", parts=response_parts))
                
                # Loop continues... Model will see all results in next turn.

            # Case 2: Model returned text (Final Answer)
            else:
                # Handle case where parts might be empty or only text parts
                text_parts = [p for p in candidate.content.parts if p.text]
                if text_parts:
                    final_text = text_parts[0].text

                    # --- HALLUCINATION GUARD START ---
                    # Check if model wrote code like ```tool_send_email(...) instead of calling it
                    hallucination_pattern = r"```(?:python\n)?\s*(tool_\w+)\s*\("
                    
                    match = re.search(hallucination_pattern, final_text)
                    if match:
                        hallucinated_tool = match.group(1)
                        logger.warning(f"Hallucinated tool call detected: {hallucinated_tool}")
                        
                        # 1. Add the "bad" message to history (so the model sees its mistake)
                        gemini_history.append(candidate.content)
                        
                        # 2. Inject a SYSTEM ERROR as a 'user' message to force correction
                        error_msg = (
                            f"SYSTEM ERROR: You attempted to call '{hallucinated_tool}' by writing Python code in Markdown. "
                            "This is FORBIDDEN. You must use the Native Tool Call feature. "
                            "Do not write code. Retry immediately by emitting a proper Tool Call."
                        )
                        gemini_history.append(types.Content(role="user", parts=[types.Part(text=error_msg)]))
                        
                        await db.log_trace_event(run_id, "system_injection", f"Hallucination Guard: Reprompting for {hallucinated_tool}")
                        
                        # 3. Force loop to retry immediately
                        continue
                    # --- HALLUCINATION GUARD END ---

                else:
                    final_text = "No response generated."

                # Log Final Answer to DB trace
                await db.complete_trace_run(run_id, final_text)

                # Return to caller - they will save to chat as "assistant" message
                return final_text

        return "Error: Agent reached maximum iteration limit."

    finally:
        # CRITICAL: Clean up context to prevent leakage between requests
        # This ensures that if another request comes in, it doesn't see this card_id
        active_card_id.reset(token)


# =============================================================================
# CENTRALIZED TOOL EXECUTOR: Safe argument filtering via signature inspection
# =============================================================================


async def _call_tool_safely(tool_name: str, llm_args: dict) -> str:
    """
    Execute a tool function with intelligent argument filtering.

    This function inspects the target tool's signature and passes only the
    arguments it actually expects, preventing crashes from LLM hallucinated
    parameters.

    NOTE: card_id is NO LONGER passed here. Tools retrieve it from context
    via the @require_card_id decorator and get_card_id() function.

    Args:
        tool_name: The name of the tool function to call
        llm_args: Raw arguments from the LLM (may contain extra/hallucinated keys)

    Returns:
        The tool's output as a string

    Raises:
        KeyError: If tool_name is not found in TOOL_MAP
    """
    # 1. Retrieve the function object
    if tool_name not in TOOL_MAP:
        raise KeyError(f"Tool '{tool_name}' not found in TOOL_MAP")

    tool_func = TOOL_MAP[tool_name]

    # 2. Inspect the function signature
    sig = inspect.signature(tool_func)

    # 3. Use ONLY LLM args - tools get card_id from context
    #    Do NOT merge in card_id here!
    available_data = {**llm_args}

    # 4. Filter: Build final_args with only parameters the function expects
    final_args = {}
    for param_name in sig.parameters:
        if param_name in available_data:
            final_args[param_name] = available_data[param_name]

    # 5. Hallucination Check: Log any args the LLM provided that aren't valid
    #    This includes if the LLM tries to hallucinate a card_id parameter
    valid_params = set(sig.parameters.keys())
    provided_args = set(llm_args.keys())
    hallucinated_args = provided_args - valid_params

    if hallucinated_args:
        logger.warning(
            f"LLM hallucinated args for {tool_name}: {hallucinated_args}. "
            f"These were filtered out."
        )

    # 6. Execute the tool with filtered arguments
    result = await tool_func(**final_args)
    return result


# --- Dynamic Tool Router ---
async def execute_tool_router(name: str, args: dict) -> str:
    """
    Route tool calls to their implementations using dynamic lookup.

    This function serves as the entry point for all tool execution. It uses
    TOOL_MAP for dynamic function lookup and _call_tool_safely for intelligent
    argument filtering.

    NOTE: card_id parameter removed - tools get it from context via
    the @require_card_id decorator and get_card_id() function.

    Args:
        name: The name of the tool to execute
        args: Dictionary of arguments from the LLM

    Returns:
        The tool result as a string
    """
    if name in TOOL_MAP:
        return await _call_tool_safely(name, args)

    return f"Error: Unknown tool '{name}'"
