import google.generativeai as genai
from google.generativeai.types import FunctionDeclaration, Tool
import database as db
from tools import my_tool_list

MAX_TURNS = 10
WARNING_TURN = 8

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
            
            # 3. EXECUTE THE TOOL (The "Act" phase)
            # You need a router here.
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