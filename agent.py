import os, re
import logging
import inspect
import asyncio
from datetime import datetime
from enum import Enum
from dataclasses import dataclass
from typing import List, Callable, Optional
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
# UNIFIED AGENT ARCHITECTURE: Persona Configuration
# =============================================================================

class ContextFraming(str, Enum):
    """How chat history should be presented to the agent."""
    PARTICIPATORY = "participatory"  # Agent is a participant in conversation
    ANALYTIC = "analytic"            # Agent analyzes chat as read-only data


@dataclass
class AgentPersona:
    """Configuration defining an agent's behavior and capabilities."""
    
    name: str                                    # Human-readable identifier
    system_prompt_file: str                      # Path to persona-specific prompt
    context_framing: ContextFraming              # How to format chat history
    allowed_tools: List[Callable]                # Tools this agent can use
    kickoff_message: Optional[str] = None        # Hidden command for phantom agents
    max_turns: int = 10                          # ReAct loop limit
    warning_turn: int = 8                        # Turn to inject hurry-up warning


# =============================================================================
# TOOL MAP: Dynamic function lookup for the Centralized Tool Executor
# =============================================================================
# Maps tool function names (strings from LLM) to actual Python function objects.
# This enables dynamic signature inspection and safe argument filtering.

TOOL_MAP = {func.__name__: func for func in my_tool_list}

MAX_TURNS = 10
WARNING_TURN = 8


# =============================================================================
# AGENT PERSONA CONFIGURATIONS
# =============================================================================

# Import individual tools for persona-specific tool lists
from tools import (
    tool_get_quantitative_overview,
    tool_get_specific_lab_values,
    tool_get_abnormal_labs,
    tool_get_microbiology_overview,
    tool_get_microbiology_details,
    tool_get_imaging_overview,
    tool_get_imaging_details,
    tool_get_pathology_overview,
    tool_get_pathology_details,
    tool_get_history_overview,
    tool_get_history_details,
    tool_send_email_update,
)

# Read-only tools for phantom agents (no email capability)
READ_ONLY_TOOLS = [
    # Group A: Quantitative (Blood Work)
    tool_get_quantitative_overview,
    tool_get_specific_lab_values,
    tool_get_abnormal_labs,
    
    # Group B: Microbiology
    tool_get_microbiology_overview,
    tool_get_microbiology_details,
    
    # Group C: Imaging
    tool_get_imaging_overview,
    tool_get_imaging_details,
    
    # Group D: Pathology
    tool_get_pathology_overview,
    tool_get_pathology_details,
    
    # Group E: Clinical History
    tool_get_history_overview,
    tool_get_history_details,
    
    # NOTE: No tool_send_email_update - phantom agents don't send emails directly
]

ADMISSION_KICKOFF = """**COMMAND:** Perform a comprehensive file review for admission note generation.

1. Query **Tier 1 Data**: Call tools to fetch the latest Labs, Vitals, and Imaging.
2. Analyze **Tier 2 Data**: Read the provided clinical transcript for the narrative.
3. Review **Tier 3 Data**: Fetch relevant history documents.
4. Synthesize all findings into a professional Hebrew Admission Note.

**Constraint:** Do not generate the final note until you have executed at least 3 data-gathering tool calls to ensure comprehensive coverage.
"""


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


def _build_system_instruction(persona: AgentPersona) -> str:
    """
    Assemble layered system instruction.
    
    Layer 1: base_investigator.md (auto-prepended)
    Layer 2: persona-specific prompt
    Layer 3: date/time and tool usage reminders
    """
    # Layer 1: Base Investigator
    base_path = "prompts/base_investigator.md"
    with open(base_path, "r") as f:
        instruction = f.read()
    
    # Layer 2: Persona-specific
    with open(persona.system_prompt_file, "r") as f:
        instruction += "\n\n" + f.read()
    
    # Layer 3: Dynamic additions
    instruction += (
        f"\n\nFor your reference, the date today is {get_israel_date_str()} "
        f"and the time now is {get_israel_time_str()}."
        "\n\nIMPORTANT: When using tools, you MUST emit a native Tool Call. "
        "Do NOT write Python code or Markdown blocks like ```tool_name(...)```. "
        "Just call the tool directly using the provided function interface."
        "\n\nCRITICAL: When calling ANY tool, you MUST use the exact tool name including "
        "the 'tool_' prefix (e.g., 'tool_send_email_update', NOT 'send_email_update'). "
        "All available tools follow this naming convention - never omit the prefix."
    )
    
    return instruction


def _format_chat_history(
    chat_history: list,
    framing: ContextFraming,
    run_id: str = None
) -> list:
    """
    Format chat history based on context framing mode.
    
    Args:
        chat_history: Raw chat messages from DB
        framing: PARTICIPATORY or ANALYTIC mode
        run_id: Optional trace run ID for logging
        
    Returns:
        List of Gemini Content objects
    """
    if framing == ContextFraming.PARTICIPATORY:
        return _format_participatory(chat_history, run_id)
    else:
        return _format_analytic(chat_history, run_id)


def _format_participatory(
    chat_history: list,
    run_id: str = None
) -> list:
    """
    Format as conversational history (current behavior).
    
    Agent sees itself as a participant generating the next turn.
    """
    gemini_history = []
    
    for msg in chat_history:
        role = msg.get("role", "")
        content = msg.get("content", "")
        
        if role == "user":
            gemini_history.append(
                types.Content(role="user", parts=[types.Part(text=content)])
            )
        elif role == "assistant":
            gemini_history.append(
                types.Content(role="model", parts=[types.Part(text=content)])
            )
    
    return gemini_history


def _format_analytic(
    chat_history: list,
    run_id: str = None
) -> list:
    """
    Format as read-only clinical transcript.
    
    Agent sees chat as a data source to analyze, not reply to.
    Returns a SINGLE user message containing the flattened transcript.
    
    IMPORTANT: Only includes user, assistant, and info messages.
    Transient logs (role="log") and unknown roles are explicitly filtered out
    to prevent crashes and ensure clean transcript output.
    """
    transcript_lines = []
    
    for msg in chat_history:
        role = msg.get("role", "")
        content = msg.get("content", "")
        timestamp = msg.get("timestamp", "")
        
        # Skip transient logs and unknown roles entirely
        # This prevents crashes from unexpected role types
        if role not in ["user", "assistant", "info"]:
            continue
        
        # Format timestamp
        if isinstance(timestamp, datetime):
            time_str = timestamp.strftime("%Y-%m-%d %H:%M")
        elif timestamp:
            time_str = timestamp[:16] if len(timestamp) >= 16 else timestamp
        else:
            time_str = "Unknown time"
        
        # Map role to display name with explicit handling for each type
        if role == "user":
            role_display = "Physician"
        elif role == "assistant":
            role_display = "AI Consultant"
        elif role == "info":
            role_display = "System Alert"
        else:
            continue  # Defensive: skip any unexpected roles (should not reach here)
        
        transcript_lines.append(f"[{time_str}] {role_display}: {content}")
    
    # Wrap in XML-style tags for clear boundaries
    transcript_text = "<clinical_transcript_log>\n"
    transcript_text += "\n".join(transcript_lines)
    transcript_text += "\n</clinical_transcript_log>"
    
    # Return as single user message
    return [types.Content(
        role="user",
        parts=[types.Part(text=transcript_text)]
    )]


async def _execute_core_loop(
    card_id: str,
    chat_history: list,
    persona: AgentPersona
) -> str:
    """
    The Unified Agent Engine - executes ReAct loop for any persona.
    
    This is the shared "brain" that powers all agent types.
    
    Args:
        card_id: MongoDB ObjectId string of the patient card
        chat_history: Raw chat messages from DB
        persona: Configuration defining agent behavior
        
    Returns:
        Final text artifact (caller decides where to send it)
        
    Raises:
        Exception: If agent fails (caller handles error)
    """
    # Validate card_id
    if not card_id or not ObjectId.is_valid(card_id):
        return "System Error: Invalid Card ID provided."
    
    # Set context for tools
    token = active_card_id.set(card_id)
    
    try:
        # 1. Load layered prompts
        system_instruction = _build_system_instruction(persona)
        
        # 2. Initialize trace
        run_id = await db.create_trace_run(
            card_id,
            persona.kickoff_message or "Agent started"
        )
        
        # 3. Format history per persona's framing mode
        gemini_history = _format_chat_history(
            chat_history,
            persona.context_framing,
            run_id
        )
        
        # 4. Inject kickoff message for phantom agents
        # CRITICAL: This MUST be appended AFTER the formatted history so the model
        # reads the data first (transcript), then receives the command to act on it.
        # The kickoff message serves as the "trigger" that tells the agent what to do.
        if persona.kickoff_message:
            gemini_history.append(
                types.Content(
                    role="user",
                    parts=[types.Part(text=persona.kickoff_message)]
                )
            )
            await db.log_trace_event(
                run_id,
                "system_injection",
                f"Kickoff: {persona.kickoff_message[:50]}..."
            )
        
        # 5. Build tool list for this persona
        tool_list = persona.allowed_tools
        
        # 6. Initialize Gemini client
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        
        # 7. ReAct Loop
        previous_tool_calls_set = set()
        
        for turn in range(persona.max_turns):
            # --- "Soft Limit" Injection ---
            if turn == persona.warning_turn:
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

            # A. Call Model
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=gemini_history,
                config=types.GenerateContentConfig(
                    tools=tool_list,
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
            logger.info(f"Model Response Parts: {len(candidate.content.parts)}")
            for i, part in enumerate(candidate.content.parts):
                if part.text:
                    logger.info(f"Part {i} [TEXT/THOUGHT]: {part.text[:200]}...")
                elif part.function_call:
                    logger.info(f"Part {i} [CALL]: {part.function_call.name}")

            # Case 0: Graceful Exit at max_turns - 1 if model wants to call a tool
            has_function_calls = any(part.function_call for part in candidate.content.parts)
            if (
                turn == persona.max_turns - 1
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

            # Case 1: Model wants to call one or more tools
            if has_function_calls:
                
                # 1. Add Model's full message to history
                gemini_history.append(candidate.content)
                
                # 2. Collect ALL function calls
                tool_tasks = []
                call_metadata = []
                
                for part in candidate.content.parts:
                    if part.function_call:
                        fc = part.function_call
                        tool_name = fc.name
                        tool_args = dict(fc.args)
                        call_id = getattr(fc, 'id', None)
                        
                        # Deduplication check
                        call_signature = (tool_name, str(tool_args))
                        if call_signature in previous_tool_calls_set:
                            async def duplicate_error():
                                return "SYSTEM ERROR: You just called this tool with these exact arguments. Try a different query or stop."
                            task = asyncio.create_task(duplicate_error())
                        else:
                            previous_tool_calls_set.add(call_signature)
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
                
                # 4. Build response parts for ALL results
                response_parts = []
                for i, result in enumerate(results):
                    if isinstance(result, Exception):
                        safe_result = f"Error: {str(result)}"
                        logger.error(f"Tool {call_metadata[i]['name']} failed: {result}")
                    else:
                        safe_result = str(result)
                    
                    await db.log_trace_event(run_id, "tool_result", safe_result)
                    
                    func_response = types.FunctionResponse(
                        name=call_metadata[i]["name"],
                        response={"result": safe_result}
                    )
                    if call_metadata[i]["id"] is not None:
                        func_response.id = call_metadata[i]["id"]
                    
                    response_parts.append(types.Part(function_response=func_response))
                
                # 5. Append SINGLE message with ALL function responses
                gemini_history.append(types.Content(role="tool", parts=response_parts))
                
                # Loop continues...

            # Case 2: Model returned text (Final Answer)
            else:
                text_parts = [p for p in candidate.content.parts if p.text]
                if text_parts:
                    final_text = text_parts[0].text

                    # --- HALLUCINATION GUARD ---
                    hallucination_pattern = r"```(?:python\n)?\s*(tool_\w+)\s*\("
                    match = re.search(hallucination_pattern, final_text)
                    if match:
                        hallucinated_tool = match.group(1)
                        logger.warning(f"Hallucinated tool call detected: {hallucinated_tool}")
                        
                        gemini_history.append(candidate.content)
                        
                        error_msg = (
                            f"SYSTEM ERROR: You attempted to call '{hallucinated_tool}' by writing Python code in Markdown. "
                            "This is FORBIDDEN. You must use the Native Tool Call feature. "
                            "Do not write code. Retry immediately by emitting a proper Tool Call."
                        )
                        gemini_history.append(types.Content(role="user", parts=[types.Part(text=error_msg)]))
                        
                        await db.log_trace_event(run_id, "system_injection", f"Hallucination Guard: Reprompting for {hallucinated_tool}")
                        
                        continue
                    # --- END HALLUCINATION GUARD ---

                else:
                    final_text = "No response generated."

                # Log Final Answer to DB trace
                await db.complete_trace_run(run_id, final_text)

                # Return to caller
                return final_text

        return "Error: Agent reached maximum iteration limit."
        
    finally:
        active_card_id.reset(token)


async def run_agent(card_id: str, chat_history: list) -> str:
    """
    The Medical Agent - reasons about user input and uses tools.
    
    This is now a thin wrapper around _execute_core_loop() using the
    Clinical Consultant persona configuration.

    Args:
        card_id: The ID of the current card (set in context for tools to access)
        chat_history: List of chat message dicts from the DB

    Returns:
        The final assistant response text. The caller is responsible for saving to DB.
    """
    # Define the Chat persona configuration
    chat_persona = AgentPersona(
        name="Clinical Consultant",
        system_prompt_file="prompts/consultant.md",
        context_framing=ContextFraming.PARTICIPATORY,
        allowed_tools=my_tool_list,  # All tools including email
        max_turns=MAX_TURNS,
        warning_turn=WARNING_TURN
    )
    
    # Execute using the unified core loop
    return await _execute_core_loop(card_id, chat_history, chat_persona)


async def run_admission_agent(card_id: str) -> str:
    """
    Run the Admission Generator phantom agent.
    
    This agent runs in the background, analyzes the patient file,
    and generates a Hebrew admission note.
    
    Args:
        card_id: MongoDB ObjectId string
        
    Returns:
        The generated Hebrew admission note text
    """
    # Fetch current chat history snapshot
    card = await db.cards_collection.find_one({"_id": ObjectId(card_id)})
    chat_history = card.get("chat", []) if card else []
    
    # Define the Admission persona
    admission_persona = AgentPersona(
        name="Admission Generator",
        system_prompt_file="prompts/admission.md",
        context_framing=ContextFraming.ANALYTIC,
        allowed_tools=READ_ONLY_TOOLS,  # No email tool
        kickoff_message=ADMISSION_KICKOFF,
        max_turns=15,  # More turns for comprehensive analysis
        warning_turn=12
    )
    
    # Execute using the unified core loop
    return await _execute_core_loop(card_id, chat_history, admission_persona)


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
