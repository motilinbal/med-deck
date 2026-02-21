import re
import logging
import inspect
import asyncio
from datetime import datetime
from enum import Enum
from dataclasses import dataclass
from typing import List, Callable, Optional, Tuple
from abc import ABC, abstractmethod
from google.genai import types
import database as db

from app.services.gemini_client import get_client
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


class OutputDestination(str, Enum):
    """Where the agent's output should be sent."""
    CHAT_ADD = "chat_add"                      # Add to chat as assistant message
    EMAIL_WITH_TRANSLATION = "email_translate"  # Translate to Hebrew, sanitize, then email
    EMAIL_DIRECT = "email_direct"              # Sanitize and email directly (no translation)


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
    simulated_date: Optional[str] = None         # Override "today" date for clinical simulation


@dataclass
class AgentConfig:
    """Flexible configuration for creating modular agents.
    
    This dataclass allows easy configuration of different agent types
    with varying models, prompts, tools, and output destinations.
    """
    
    name: str                                    # Human-readable identifier
    system_prompt_file: str                      # Path to persona-specific prompt
    context_framing: ContextFraming              # How to format chat history
    allowed_tools: List[Callable]                # Tools this agent can use
    model_name: str = "gemini-2.5-flash"        # LLM model to use
    output_dest: OutputDestination = OutputDestination.CHAT_ADD  # Where to send output
    kickoff_message: Optional[str] = None        # Hidden command for phantom agents
    max_turns: int = 10                          # ReAct loop limit
    warning_turn: int = 8                        # Turn to inject hurry-up warning
    
    def to_persona(self) -> AgentPersona:
        """Convert to AgentPersona for backward compatibility with _execute_core_loop."""
        return AgentPersona(
            name=self.name,
            system_prompt_file=self.system_prompt_file,
            context_framing=self.context_framing,
            allowed_tools=self.allowed_tools,
            kickoff_message=self.kickoff_message,
            max_turns=self.max_turns,
            warning_turn=self.warning_turn
        )


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
    get_quantitative_overview,
    get_specific_lab_values,
    get_abnormal_labs,
    get_microbiology_overview,
    get_microbiology_details,
    get_imaging_overview,
    get_imaging_details,
    get_pathology_overview,
    get_pathology_details,
    get_history_overview,
    get_history_details,
    send_email_update,
    calculate_acid_base,
    submit_final_answer,  # Tool-as-Answer pattern
)

# CORE AGENT TOOLS - Always included in any agent's tool list
# These tools are essential for agent operation and termination
CORE_AGENT_TOOLS = [
    submit_final_answer,  # Tool-as-Answer: required for deterministic termination
]

def get_agent_tools(domain_tools: List[Callable]) -> List[Callable]:
    """
    Build a complete agent tool list by combining domain-specific tools
    with core agent tools.
    
    This ensures that ALL agents have access to essential tools like
    submit_final_answer for deterministic termination.
    
    Args:
        domain_tools: List of domain-specific tools (e.g., labs, imaging, etc.)
    
    Returns:
        Complete tool list including core agent tools
    
    Example:
        # For Chat Agent with all tools:
        tools = get_agent_tools(my_tool_list)
        
        # For Admission Agent with read-only tools:
        tools = get_agent_tools(READ_ONLY_TOOLS)
    """
    # Create a set to avoid duplicates, preserve order
    tool_names = set()
    combined = []
    
    # First add core agent tools (they're essential)
    for tool in CORE_AGENT_TOOLS:
        combined.append(tool)
        tool_names.add(tool.__name__)
    
    # Then add domain tools (skip if already added)
    for tool in domain_tools:
        if tool.__name__ not in tool_names:
            combined.append(tool)
            tool_names.add(tool.__name__)
    
    return combined

# Read-only tools for phantom agents (no email capability)
READ_ONLY_TOOLS = [
    # Group A: Quantitative (Blood Work)
    get_quantitative_overview,
    get_specific_lab_values,
    get_abnormal_labs,
    
    # Group B: Microbiology
    get_microbiology_overview,
    get_microbiology_details,
    
    # Group C: Imaging
    get_imaging_overview,
    get_imaging_details,
    
    # Group D: Pathology
    get_pathology_overview,
    get_pathology_details,
    
    # Group E: Clinical History
    get_history_overview,
    get_history_details,

    # Group H: Acid-Base Calculator
    calculate_acid_base,

    # Group G: Agent Control (Tool-as-Answer) - needed for deterministic termination
    submit_final_answer,

    # NOTE: No send_email_update - phantom agents don't send emails directly
]

ADMISSION_KICKOFF = """**COMMAND:** Perform a comprehensive file review for admission note generation.

1. Query **Tier 1 Data**: Call tools to fetch the latest Labs, Vitals, and Imaging.
2. Analyze **Tier 2 Data**: Read the provided clinical transcript for the narrative.
3. Review **Tier 3 Data**: Fetch relevant history documents.
4. Synthesize all findings into a professional **English** Admission Note.

**Constraint:** Do not generate the final note until you have executed at least 3 data-gathering tool calls to ensure comprehensive coverage.
"""

DDX_KICKOFF = """**COMMAND:** Perform a diagnostic analysis to generate a Probabilistic Differential Diagnosis.

1. First, complete the ANCHOR PHASE: Read the clinical transcript and generate at least 3 distinct differential diagnoses.
2. Then, STRESS TEST each hypothesis: Use tools to gather data that PROVES or DISPROVES each diagnosis.
3. Look for PERTINENT NEGATIVES: Evidence that rules OUT a diagnosis is as important as positive findings.
4. Track your RE-RANKING: After each tool result, update the probability of each diagnosis.
5. Synthesize into a DDx Report with Tier 1 (Leading), Tier 2 (Alternatives), and Tier 3 (Must Not Miss).

**Constraint:** Do not submit your final answer until you have executed at least 3 data-gathering tool calls to test your hypotheses.
"""

MORNING_REPORT_KICKOFF = """**COMMAND:** Generate a Morning Report sign-out for the incoming team.

1. Analyze the clinical transcript and patient data
2. Identify the key information: one-liner, overnight events, current status
3. Determine the trajectory: Are they Better, Worse, or Unchanged?
4. Synthesize into a concise script that can be read aloud in under 2 minutes

**Constraint:** Do not submit your final answer until you have gathered key data using at least 2 tool calls. The report must be concise enough to read in 2 minutes or less.
"""

RX_KICKOFF = """**COMMAND:** Generate an Executable Treatment Plan based on the patient's data.

**MANDATORY FIRST STEP (TURN 0):** You must FIRST establish your clinical baseline by outputting your top 3 differential diagnoses.
* **DO NOT CALL ANY TOOLS ON THIS TURN.** The system will block you if you attempt to gather data before stating your differential.
* You must use the exact phrase "differential diagnosis" and the numbering format "1.", "2.", "3.".

**SUBSEQUENT STEPS (TURN 1+):**
1.  **Safety Audit:** Use tools to check renal function, hemodynamics, and home medications.
2.  **Fallback:** If quantitative labs are unavailable, you MUST check the clinical history (`get_history_overview`).
3.  **Synthesize:** Finalize your specific, actionable treatment and workup orders.
"""

DISCHARGE_KICKOFF = """**COMMAND:** Generate a Gold-Standard Hospital Discharge Summary based on the patient's complete file.

**MANDATORY FIRST STEP (TURN 0):** You must FIRST satisfy the system protocols by outputting the top 3 differential diagnoses considered at admission.
* **DO NOT CALL ANY TOOLS ON THIS TURN.** The system will block you if you attempt to gather data before stating your differential.
* You must use the exact phrase "differential diagnosis" and the numbering format "1.", "2.", "3.".

**SUBSEQUENT STEPS (TURN 1+):**
1.  **Data Audit:** Use tools to hunt for the patient's baseline history (`get_history_overview`), lab trajectories (Admission vs Peak vs Discharge), and imaging.
2.  **Synthesize:** Finalize your comprehensive, problem-based discharge summary following the exact requested structure.
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
        elif role == "model_thought":
            summary_parts.append(f"Step {i}: Model Reasoning/Thought")
            # Truncate long thoughts for readability
            if len(str(content)) > 500:
                summary_parts.append(f"  {str(content)[:500]}...")
            else:
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
    # Use simulated date if provided (for clinical simulation mode), otherwise use real date
    if persona.simulated_date:
        date_line = f"\n\n**CLINICAL SIMULATION MODE:** For the purpose of this report, treat the date as {persona.simulated_date}."
    else:
        date_line = (
            f"\n\nFor your reference, the date today is {get_israel_date_str()} "
            f"and the time now is {get_israel_time_str()}."
        )

    instruction += (
        date_line +
        "\n\nIMPORTANT: When using tools, you MUST emit a native Tool Call. "
        "Do NOT write Python code or Markdown blocks like ```tool_name(...)```. "
        "Just call the tool directly using the provided function interface."
        "\n\nNOTE: All tool names are simple and direct (e.g., 'send_email_update', "
        "'get_quantitative_overview'). Use the exact name without any prefix."
        "\n\nCRITICAL - THOUGHT BEFORE ACTION PROTOCOL:"
        "\n1. BEFORE calling ANY tool, you MUST first output your reasoning as a 'scratchpad'."
        "\n2. Your scratchpad MUST use this format:"
        "\n   *THOUGHT:* [What you're trying to figure out]"
        "\n   *ACTION:* [Which tool you're calling and why]"
        "\n   *EXPECTED:* [What you expect to find]"
        "\n3. ONLY after outputting your scratchpad, call the tool."
        "\n4. After receiving tool results, output:"
        "\n   *OBSERVATION:* [What the results show]"
        "\n   *NEXT STEP:* [What you're going to do next]"
        "\n5. Repeat this cycle until you have enough information."
        "\n6. When you have gathered sufficient information, use 'submit_final_answer' tool."
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
    persona: AgentPersona,
    model_name: str = "gemini-2.5-flash"
) -> str:
    """
    The Unified Agent Engine - executes ReAct loop for any persona.
    
    This is the shared "brain" that powers all agent types.
    
    Args:
        card_id: MongoDB ObjectId string of the patient card
        chat_history: Raw chat messages from DB
        persona: Configuration defining agent behavior
        model_name: The LLM model to use (default: gemini-2.5-flash)
        
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
        logger.info(f"Creating trace run for card {card_id}")
        run_id = await db.create_trace_run(
            card_id,
            persona.kickoff_message or "Agent started"
        )
        logger.info(f"Trace run created with ID: {run_id}")
        
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
        
        # 6. Get Gemini client (singleton)
        client = get_client()
        
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
                model=model_name,
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

            # Handle case where candidate.content is None (e.g., blocked response)
            if candidate.content is None:
                logger.warning(f"Model returned no content on turn {turn}. Adding error to history and continuing.")
                error_msg = "SYSTEM ERROR: Your response was empty or blocked. Please try again with a valid response."
                gemini_history.append(types.Content(role="user", parts=[types.Part(text=error_msg)]))
                await db.log_trace_event(run_id, "system_injection", "Empty model response - reprompting")
                continue

            # Handle case where candidate.content.parts is None or empty
            if not candidate.content.parts:  # None or empty list
                logger.warning(f"Model returned empty parts on turn {turn}. Adding error to history and continuing.")
                error_msg = "SYSTEM ERROR: Your response had no parts. Please try again with a valid response."
                gemini_history.append(types.Content(role="user", parts=[types.Part(text=error_msg)]))
                await db.log_trace_event(run_id, "system_injection", "Empty parts response - reprompting")
                continue

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

            # === ANCHOR PHASE ENFORCEMENT ===
            # Only enforce Anchor Phase on ANALYTIC (phantom) agents, not PARTICIPATORY (chat) agents.
            # PARTICIPATORY agents should be able to use tools immediately when responding to user questions.
            anchor_phase_rejected = False
            if turn == 0 and has_function_calls and persona.context_framing == ContextFraming.ANALYTIC:
                # Check if the model has already output Anchor Phase reasoning
                text_parts = [p for p in candidate.content.parts if p.text]
                model_text = text_parts[0].text if text_parts else ""
                # Require ALL three: differential/diagnosis keywords AND 1., 2., 3. present
                # This ensures the model listed a complete set of differentials
                has_anchor_reasoning = (
                    model_text and
                    ("differential" in model_text.lower() or "diagnosis" in model_text.lower() or "rule out" in model_text.lower()) and
                    "1." in model_text and
                    "2." in model_text and
                    "3." in model_text
                )

                if not has_anchor_reasoning:
                    # ANCHOR PHASE VIOLATION - Reject the tool call
                    anchor_phase_rejected = True
                    logger.info(f"Intercepted tool call on turn 0 - enforcing Anchor Phase")

                    # Log the blocked tool call for traceability
                    for part in candidate.content.parts:
                        if part.function_call:
                            fc = part.function_call
                            await db.log_trace_event(
                                run_id,
                                "model_call",
                                f"BLOCKED: Calling {fc.name}",
                                tool_call_info={"name": fc.name, "args": dict(fc.args), "id": getattr(fc, 'id', None)}
                            )

                    # Construct rejection message - forces model to think before acting
                    rejection_msg = (
                        "🔒 **TOOL CALL BLOCKED - ANCHOR PHASE VIOLATION** 🔒\n\n"
                        "PROTOCOL ERROR: You attempted to query patient data (Tier 1) BEFORE establishing "
                        "your clinical baseline.\n\n"
                        "MANDATORY ANCHOR PHASE (DO NOT SKIP):\n"
                        "1. Read the chat history to identify the Chief Complaint and the Resident's working diagnosis.\n"
                        "2. **Generate at least 3 distinct differential diagnoses** based ONLY on the chat.\n"
                        "3. Example: 'Resident suggests Pneumonia. I must also rule out Pulmonary Embolism and Acute Coronary Syndrome.'\n\n"
                        "⚠️ **YOU MUST FIRST OUTPUT YOUR 3 DIFFERENTIAL DIAGNOSES BEFORE CALLING ANY TOOLS.**\n"
                        "Once you have listed your hypotheses, you may proceed to data gathering.\n"
                    )

                    # Add rejection as a user message - this forces the model to retry immediately
                    # but with the constraint that it must first output the Anchor reasoning
                    gemini_history.append(
                        types.Content(role="user", parts=[types.Part(text=rejection_msg)])
                    )

                    await db.log_trace_event(
                        run_id,
                        "system_injection",
                        "Anchor Phase Enforcement: Rejected tool call on turn 0"
                    )

                    # Skip processing this turn's tool calls - model must retry with Anchor reasoning
                    continue

            # Case 1: Model wants to call one or more tools
            if has_function_calls and not anchor_phase_rejected:

                # 1. Add Model's full message to history (includes both text and function calls)
                gemini_history.append(candidate.content)
                
                # 2. Check if there's ANY text in the response (inner monologue)
                text_parts = [p for p in candidate.content.parts if p.text]
                if text_parts:
                    # Log any text as model thought - this includes the *THOUGHT:* inner monologue
                    model_text = text_parts[0].text
                    if model_text.strip():
                        logger.info(f"Logging model thought (alongside tool call): {model_text[:200]}...")
                        await db.log_trace_event(
                            run_id,
                            "model_thought",
                            model_text
                        )
                
                # 3. Collect ALL function calls
                tool_tasks = []
                call_metadata = []
                
                # Check for Tool-as-Answer termination signal
                final_answer_content = None
                
                for part in candidate.content.parts:
                    if part.function_call:
                        fc = part.function_call
                        tool_name = fc.name
                        tool_args = dict(fc.args)
                        call_id = getattr(fc, 'id', None)
                        
                        # === TOOL-AS-ANSWER PATTERN: Deterministic Termination ===
                        # If the model calls submit_final_answer, this is the final answer
                        if tool_name == "submit_final_answer":
                            # Extract the answer from tool arguments
                            final_answer_content = tool_args.get("response_text", str(tool_args))
                            logger.info(f"Model signaled completion via submit_final_answer tool")
                            
                            # Log as final answer and terminate
                            await db.log_trace_event(
                                run_id,
                                "model_call",
                                f"Final Answer Tool Called",
                                tool_call_info={"name": tool_name, "args": tool_args, "id": call_id},
                            )
                            logger.info(f"Completing trace run {run_id} with final answer")
                            await db.complete_trace_run(run_id, final_answer_content)
                            logger.info(f"Trace run {run_id} completed successfully")
                            return final_answer_content
                        # ============================================================
                        
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

            # Case 2: Model returned text (could be reasoning OR final answer)
            else:
                text_parts = [p for p in candidate.content.parts if p.text]
                if text_parts:
                    model_text = text_parts[0].text

                    # --- HALLUCINATION GUARD ---
                    hallucination_pattern = r"```(?:python\n)?\s*(tool_\w+)\s*\("
                    match = re.search(hallucination_pattern, model_text)
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

                    # Determine if this is interim reasoning or final answer
                    # If more turns remain, treat as interim reasoning (inner monologue)
                    # If at max turns, treat as final answer
                    is_final_turn = (turn == persona.max_turns - 1)
                    
                    if is_final_turn:
                        # Last turn - this is the final answer
                        await db.complete_trace_run(run_id, model_text)
                        return model_text
                    else:
                        # More turns remaining - log as model thought/reasoning
                        logger.info(f"Logging model thought for turn {turn}: {model_text[:100]}...")
                        await db.log_trace_event(
                            run_id,
                            "model_thought",
                            model_text
                        )
                        # Add to history and continue the loop (model may call tools next)
                        gemini_history.append(candidate.content)
                        continue

                else:
                    model_text = "No response generated."
                    # Only log as final answer if at last turn, otherwise continue
                    if turn == persona.max_turns - 1:
                        await db.complete_trace_run(run_id, model_text)
                        return model_text
                    else:
                        await db.log_trace_event(run_id, "model_thought", model_text)
                        continue

        # =====================================================================
        # FINAL FALLBACK: Ask model to summarize what it has gathered so far
        # =====================================================================
        # Instead of returning an error, make one final attempt to get the model
        # to summarize all the data it has collected
        logger.warning(f"Agent reached max turns ({persona.max_turns}). Attempting final summary.")

        final_summary_msg = (
            "You have reached the maximum number of turns. Stop gathering data now.\n\n"
            "Your task is to SUMMARIZE everything you have learned so far from the tool results "
            "and provide a final answer to the user's question. "
            "Do NOT call any more tools. Simply synthesize the information you already have "
            "and present it as your final response.\n\n"
            "Provide a clear, concise summary of:\n"
            "1. What data you found\n"
            "2. Any analysis or conclusions you made\n"
            "3. Any limitations or gaps in the data\n\n"
            "This is your FINAL response - present it directly without using any tools."
        )

        try:
            # Add the summary request to history
            gemini_history.append(
                types.Content(role="user", parts=[types.Part(text=final_summary_msg)])
            )

            # Make final API call without tools
            final_response = client.models.generate_content(
                model=model_name,
                contents=gemini_history,
                config=types.GenerateContentConfig(
                    system_instruction=system_instruction,
                    temperature=0.2,  # Slightly higher for summary creativity
                ),
            )

            # Extract the final text
            if final_response.candidates and final_response.candidates[0].content:
                parts = final_response.candidates[0].content.parts
                if parts:
                    final_text = parts[0].text
                    if final_text:
                        # Add disclaimer that this is a truncated response
                        disclaimer = (
                            "⚠️ **Note:** I have reached the maximum number of turns, but this is what I've got so far."
                        )
                        final_text_with_disclaimer = disclaimer + final_text
                        await db.complete_trace_run(run_id, final_text_with_disclaimer, status="completed")
                        logger.info(f"Agent completed via final summary fallback (was at {persona.max_turns} turns)")
                        return final_text_with_disclaimer

        except Exception as e:
            logger.error(f"Final summary attempt failed: {e}")

        # Fallback to trace summary if even the final attempt fails
        summary = await generate_trace_summary(run_id)

        # Add disclaimer that this is a truncated response
        disclaimer = (
            "⚠️ **Note:** I have reached the maximum number of turns for this consultation. "
            "This is a summary of what I had gathered so far, rather than a complete response.\n\n"
        )
        fallback_msg = disclaimer + summary
        await db.complete_trace_run(run_id, fallback_msg, status="completed")
        return fallback_msg
        
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
    # Use get_agent_tools() to automatically include core tools like submit_final_answer
    chat_persona = AgentPersona(
        name="Clinical Consultant",
        system_prompt_file="prompts/consultant.md",
        context_framing=ContextFraming.PARTICIPATORY,
        allowed_tools=get_agent_tools(my_tool_list),  # All tools including email + core
        max_turns=MAX_TURNS,
        warning_turn=WARNING_TURN
    )
    
    # Execute using the unified core loop
    return await _execute_core_loop(card_id, chat_history, chat_persona)


async def run_admission_agent(card_id: str) -> str:
    """
    Run the Admission Generator phantom agent.
    
    This agent runs in the background, analyzes the patient file,
    and generates an English admission note (which will be translated to Hebrew
    by the output pipeline before emailing).
    
    Args:
        card_id: MongoDB ObjectId string
        
    Returns:
        The generated English admission note text
    """
    # Fetch current chat history snapshot
    card = await db.cards_collection.find_one({"_id": ObjectId(card_id)})
    chat_history = card.get("chat", []) if card else []
    
    # Define the Admission persona
    # Use get_agent_tools() to automatically include core tools like submit_final_answer
    admission_persona = AgentPersona(
        name="Admission Generator",
        system_prompt_file="prompts/admission.md",
        context_framing=ContextFraming.ANALYTIC,
        allowed_tools=get_agent_tools(READ_ONLY_TOOLS),  # Read-only + core tools
        kickoff_message=ADMISSION_KICKOFF,
        max_turns=15,  # More turns for comprehensive analysis
        warning_turn=12
    )
    
    # Execute using the unified core loop with gemini-2.5-flash
    return await _execute_core_loop(card_id, chat_history, admission_persona, model_name="gemini-2.5-flash")


async def run_ddx_agent(card_id: str) -> str:
    """
    Run the Differential Diagnosis (DDx) phantom agent.

    This agent runs in the background, analyzes the patient file,
    and generates a Probabilistic Differential Diagnosis report.

    The DDx agent:
    - Is an OBSERVER of the chat (uses ANALYTIC framing)
    - Does NOT send emails (uses READ_ONLY_TOOLS)
    - Outputs to chat as assistant message
    - Uses gemini-2.5-flash model

    Args:
        card_id: MongoDB ObjectId string

    Returns:
        The generated DDx report text
    """
    # Fetch current chat history snapshot
    card = await db.cards_collection.find_one({"_id": ObjectId(card_id)})
    chat_history = card.get("chat", []) if card else []

    # Define the DDx Diagnostician persona
    ddx_persona = AgentPersona(
        name="DDx Diagnostician",
        system_prompt_file="prompts/diagnostician.md",
        context_framing=ContextFraming.ANALYTIC,
        allowed_tools=get_agent_tools(READ_ONLY_TOOLS),  # Read-only + core tools (no email)
        kickoff_message=DDX_KICKOFF,
        max_turns=15,  # More turns for comprehensive analysis
        warning_turn=12
    )

    # Execute using the unified core loop with gemini-2.5-flash
    return await _execute_core_loop(card_id, chat_history, ddx_persona, model_name="gemini-2.5-flash")


async def run_morning_report_agent(card_id: str) -> str:
    """
    Run the Morning Report phantom agent.

    This agent runs in the background, analyzes the patient file,
    and generates a concise morning report for the incoming team.

    The Morning Report agent:
    - Is an OBSERVER of the chat (uses ANALYTIC framing)
    - Does NOT send emails (uses READ_ONLY_TOOLS)
    - Outputs to chat as assistant message
    - Uses gemini-2.5-flash model

    Args:
        card_id: MongoDB ObjectId string

    Returns:
        The generated morning report text
    """
    # Fetch current chat history snapshot
    card = await db.cards_collection.find_one({"_id": ObjectId(card_id)})
    chat_history = card.get("chat", []) if card else []

    # Calculate simulated date: use the latest timestamp from history or labs,
    # then add 1 day to simulate "the morning after" for the report
    from database import get_card_metadata
    metadata = await get_card_metadata(card_id)

    simulated_date = None
    latest_ts = None
    if metadata.get("last_history"):
        latest_ts = metadata["last_history"]
    if metadata.get("last_lab"):
        if latest_ts is None or metadata["last_lab"] > latest_ts:
            latest_ts = metadata["last_lab"]

    if latest_ts:
        # Parse the ISO timestamp and add 1 day
        try:
            from datetime import datetime, timedelta
            # Handle both aware and naive datetime formats
            if '+' in latest_ts or latest_ts.endswith('Z'):
                # ISO format with timezone
                dt = datetime.fromisoformat(latest_ts.replace('Z', '+00:00'))
                dt = dt.replace(tzinfo=None)  # Make naive for date calculation
            else:
                dt = datetime.fromisoformat(latest_ts)
            simulated_dt = dt + timedelta(days=1)
            simulated_date = simulated_dt.strftime("%B %d, %Y")
        except Exception as e:
            logger.warning(f"Could not parse timestamp {latest_ts}: {e}")

    # Define the Morning Report persona
    morning_report_persona = AgentPersona(
        name="Morning Report Agent",
        system_prompt_file="prompts/report.md",
        context_framing=ContextFraming.ANALYTIC,
        allowed_tools=get_agent_tools(READ_ONLY_TOOLS),  # Read-only + core tools (no email)
        kickoff_message=MORNING_REPORT_KICKOFF,
        max_turns=10,  # Shorter - morning reports are concise
        warning_turn=7,
        simulated_date=simulated_date  # Inject simulated date for clinical simulation
    )

    # Execute using the unified core loop with gemini-2.5-flash
    return await _execute_core_loop(card_id, chat_history, morning_report_persona, model_name="gemini-2.5-flash")


async def run_rx_agent(card_id: str) -> str:
    """
    Run the Rx Treatment Agent.

    This agent generates detailed, executable treatment orders with absolute specificity.
    It follows the base_investigator protocol with Anchor Phase and Safety Audit.

    The Rx Agent:
    - Is an OBSERVER of the chat (uses ANALYTIC framing)
    - Does NOT send emails (uses READ_ONLY_TOOLS)
    - Outputs to chat as assistant message
    - Uses gemini-2.5-flash model
    - Injects simulated date for clinical simulation

    Args:
        card_id: MongoDB ObjectId string

    Returns:
        The generated treatment plan text
    """
    # Fetch current chat history snapshot
    card = await db.cards_collection.find_one({"_id": ObjectId(card_id)})
    chat_history = card.get("chat", []) if card else []

    # Calculate simulated date: use the latest timestamp from history or labs
    # This represents "today" for treatment planning
    from database import get_card_metadata
    metadata = await get_card_metadata(card_id)

    simulated_date = None
    latest_ts = None
    if metadata.get("last_history"):
        latest_ts = metadata["last_history"]
    if metadata.get("last_lab"):
        if latest_ts is None or metadata["last_lab"] > latest_ts:
            latest_ts = metadata["last_lab"]

    if latest_ts:
        # Parse the ISO timestamp - use the latest data date as "today"
        try:
            from datetime import datetime, timedelta
            # Handle both aware and naive datetime formats
            if '+' in latest_ts or latest_ts.endswith('Z'):
                # ISO format with timezone
                dt = datetime.fromisoformat(latest_ts.replace('Z', '+00:00'))
                dt = dt.replace(tzinfo=None)  # Make naive for date calculation
            else:
                dt = datetime.fromisoformat(latest_ts)
            # Use the latest data date as "today" for treatment planning
            simulated_date = dt.strftime("%B %d, %Y")
        except Exception as e:
            logger.warning(f"Could not parse timestamp {latest_ts}: {e}")

    logger.info(f"[Rx Agent] simulated_date = {simulated_date}, latest_ts = {latest_ts}")

    # Define the Rx Agent persona
    rx_agent_persona = AgentPersona(
        name="Rx Treatment Agent",
        system_prompt_file="prompts/treatment.md",
        context_framing=ContextFraming.ANALYTIC,
        allowed_tools=get_agent_tools(READ_ONLY_TOOLS),  # Read-only + core tools (no email)
        kickoff_message=RX_KICKOFF,
        max_turns=10,  # Treatment planning is focused, shorter runs
        warning_turn=7,
        simulated_date=simulated_date  # Inject simulated date for clinical simulation
    )

    # Execute using the unified core loop with gemini-2.5-flash
    return await _execute_core_loop(card_id, chat_history, rx_agent_persona, model_name="gemini-2.5-flash")


async def run_discharge_agent(card_id: str) -> str:
    """
    Run the Discharge Agent (Master of Transitions of Care).

    This agent generates a gold-standard hospital discharge summary.
    It follows the base_investigator protocol with Anchor Phase and Discharge Audit.

    The Discharge Agent:
    - Is an OBSERVER of the chat (uses ANALYTIC framing)
    - Does NOT send emails (uses READ_ONLY_TOOLS)
    - Outputs to chat as assistant message
    - Uses gemini-2.5-flash model

    Args:
        card_id: MongoDB ObjectId string

    Returns:
        The generated discharge summary text
    """
    # Fetch current chat history snapshot
    card = await db.cards_collection.find_one({"_id": ObjectId(card_id)})
    chat_history = card.get("chat", []) if card else []

    # Define the Discharge Agent persona
    discharge_agent_persona = AgentPersona(
        name="Discharge Agent",
        system_prompt_file="prompts/discharge.md",
        context_framing=ContextFraming.ANALYTIC,
        allowed_tools=get_agent_tools(READ_ONLY_TOOLS),  # Read-only + core tools (no email)
        kickoff_message=DISCHARGE_KICKOFF,
        max_turns=15,  # Discharge summaries need more data gathering
        warning_turn=10,
        simulated_date=None  # No simulated date - this is a retrospective summary
    )

    # Execute using the unified core loop with gemini-2.5-flash
    return await _execute_core_loop(card_id, chat_history, discharge_agent_persona, model_name="gemini-2.5-flash")


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


# =============================================================================
# OUTPUT PIPELINE: Translation and Sanitization
# =============================================================================


def sanitize_for_email(text: str) -> str:
    """
    Apply MedicalLetterSanitizer to remove LLM scent from text.
    
    This should ALWAYS be applied to text before sending via email.
    
    Args:
        text: The text to sanitize
        
    Returns:
        Sanitized text with LLM artifacts removed
    """
    from app.utils.text_sanitizer import MedicalLetterSanitizer
    sanitizer = MedicalLetterSanitizer()
    return sanitizer.process(text)


async def translate_to_hebrew(english_text: str) -> str:
    """
    Translate English medical text to Hebrew using gemini-2.5-flash.
    
    Uses the prompts/translator.md prompt for professional Israeli
    medical Hebrew translation.
    
    Args:
        english_text: The English text to translate
        
    Returns:
        Hebrew translation
    """
    # Load translator prompt
    translator_prompt_path = "prompts/translator.md"
    with open(translator_prompt_path, "r") as f:
        translator_prompt = f.read()
    
    # Build the full prompt
    full_prompt = f"{translator_prompt}\n\n{english_text}"
    
    # Call Gemini (singleton)
    client = get_client()
    
    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[full_prompt],
        config=types.GenerateContentConfig(
            temperature=0.1,
        ),
    )
    
    # Extract the translation
    if response.candidates and response.candidates[0].content.parts:
        return response.candidates[0].content.parts[0].text
    
    return "Translation failed."


async def process_agent_output(
    output_dest: OutputDestination,
    content: str,
    card_id: str,
    subject: str = "Medical Note"
) -> str:
    """
    Route agent output based on output_dest configuration.
    
    Args:
        output_dest: Where to send the output
        content: The content to process
        card_id: The card ID for chat operations
        subject: Subject for email (if applicable)
        
    Returns:
        The final processed content
    """
    from app.services.email_sender import send_email_broadcast
    
    if output_dest == OutputDestination.CHAT_ADD:
        # Add to chat as assistant message
        await db.append_chat_message(card_id, MessageRole.ASSISTANT, content)
        return content
    
    elif output_dest == OutputDestination.EMAIL_WITH_TRANSLATION:
        # 1. Translate English → Hebrew
        hebrew_text = await translate_to_hebrew(content)
        # 2. Sanitize (remove LLM scent)
        sanitized_text = sanitize_for_email(hebrew_text)
        # 3. Send email
        send_email_broadcast(subject=subject, body=sanitized_text)
        return sanitized_text
    
    elif output_dest == OutputDestination.EMAIL_DIRECT:
        # 1. Sanitize and email directly (no translation)
        sanitized_text = sanitize_for_email(content)
        # 2. Send email
        send_email_broadcast(subject=subject, body=sanitized_text)
        return sanitized_text
    
    # Fallback: just return content
    return content
