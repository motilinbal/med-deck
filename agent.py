import google.generativeai as genai
from google.generativeai.types import FunctionDeclaration, Tool
import database as db
from tools import my_tool_list
import os
from bson.objectid import ObjectId
import datetime

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


async def run_medical_agent_manual_loop(card_id: str, user_prompt: str):
    
    # 1. Setup Context
    card = await db.cards_collection.find_one({"_id": ObjectId(card_id)})
    if not card:
        return "Error: Patient card not found."
    
    system_instruction = f"""
    You are a Medical Clinical Case Manager.
    Patient Context (Processed Note): {card.get('processed_note', 'N/A')}
    
    Your goal is to answer the user's request accurately using your tools.
    Never hallucinate medical data. If you don't know, use a tool or ask.
    """

    # 2. Initialize DB Trace
    run_id = await db.create_trace_run(card_id, user_prompt)
    
    # 3. Initialize Gemini History (Stateless List)
    # We start with the User's prompt.
    gemini_history = [
        {"role": "user", "parts": [user_prompt]}
    ]
    
    # We also log this first step to our DB
    await db.log_trace_event(run_id, "user", user_prompt)

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
            # We use the standard generate_content, passing the WHOLE history every time.
            response = client.models.generate_content(
                model='gemini-2.0-flash', # Or Pro
                contents=gemini_history,
                config=genai.types.GenerateContentConfig(
                    tools=my_tool_list, # From your tools definition
                    system_instruction=system_instruction,
                    temperature=0.1 # Low temp for precision
                )
            )
        except Exception as e:
            await db.complete_trace_run(run_id, f"Error: {str(e)}", status="failed")
            return "System Error during AI reasoning."

        # B. Analyze Response
        # The model might return Text (answer) OR a Function Call.
        
        candidate = response.candidates[0]
        
        # Case 0: Graceful Exit at MAX_TURNS - 1 if model wants to call a tool
        if turn == MAX_TURNS - 1 and candidate.content.parts and candidate.content.parts[0].function_call:
            # Force a "Graceful Exit" instead of an error
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
            
            # 2. Append the Model's "Request" to gemini_history (Required by API)
            gemini_history.append(candidate.content)
            
            # 3. DEDUPLICATION CHECK - Prevent repeating the same tool call
            current_tool_call = (tool_name, str(tool_args))
            
            if current_tool_call in previous_tool_calls_set:
                # The agent is looping. Don't run the tool.
                # Inject an error to force it to try something else.
                tool_result = "SYSTEM ERROR: You just called this tool with these exact arguments. Try a different query or stop."
            else:
                previous_tool_calls_set.add(current_tool_call)
                # EXECUTE THE TOOL (The "Act" phase)
                tool_result = await execute_tool_router(tool_name, tool_args)
            
            # 4. Log the "Result" to DB
            await db.log_trace_event(run_id, "tool_result", tool_result)
            
            # 5. Append "Result" to gemini_history
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
            
            # 1. Log Final Answer to DB
            await db.complete_trace_run(run_id, final_text)
            
            # 2. Return to User
            return final_text

    return "Error: Agent reached maximum iteration limit."

# --- Helper Router ---
async def execute_tool_router(name, args):
    """Maps string names to actual python functions"""
    if name == "get_lab_results":
        # Call your actual implementation
        return await tools.get_lab_results(**args)
    elif name == "search_internet":
        return await tools.google_search(**args)
    # ... handle others
    return "Error: Tool not found."