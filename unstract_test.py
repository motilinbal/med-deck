from dotenv import load_dotenv
import json
from unstract.llmwhisperer import LLMWhispererClientV2
from unstract.llmwhisperer.client_v2 import LLMWhispererClientException

# 1. Setup & Config
load_dotenv()
API_KEY = os.getenv("UNSTRACT_API_KEY")

# Provide the base URL and API key explicitly
client = LLMWhispererClientV2(
    base_url="https://llmwhisperer-api.us-central.unstract.com/api/v2",
    api_key=API_KEY    
)


# Get usage info
usage_info = client.get_usage_info()

# Process a document in async mode
# The client will return with a whisper hash which can be used to check the status and retrieve the result
whisper = client.whisper(file_path="test_data/labs2_cropped.pdf",
                        mode="native_text",
                        output_mode="layout_preserving")

# Get the status of a whisper operation
# whisper_hash is available in the 'whisper_hash' field of the result of the whisper operation
status = client.whisper_status(whisper["whisper_hash"])

# Retrieve the result of a whisper operation
# whisper_hash is available in the 'whisper_hash' field of the result of the whisper operation
whisper = client.whisper_retrieve(whisper["whisper_hash"])

# save to json
with open("unstract_output.json", "w") as f:
    json.dump(whisper['extraction'], f)
