"""Phase 0 sanity check: confirm qwen2.5:7b-instruct reliably emits structured
tool calls via Ollama before building the real MCP tool layer on top of it."""

import ollama

def get_zone_temperature(zone_id: str) -> dict:
    """Fake tool: returns a mock zone temperature reading."""
    return {"zone_id": zone_id, "temp_c": 24.3}


tools = [
    {
        "type": "function",
        "function": {
            "name": "get_zone_temperature",
            "description": "Get the current air temperature of a building zone in Celsius.",
            "parameters": {
                "type": "object",
                "properties": {
                    "zone_id": {
                        "type": "string",
                        "description": "The zone identifier, e.g. 'ZONE1'",
                    }
                },
                "required": ["zone_id"],
            },
        },
    }
]

response = ollama.chat(
    model="qwen2.5:7b-instruct",
    messages=[
        {
            "role": "user",
            "content": "What is the current temperature in ZONE1? Use the tool to find out.",
        }
    ],
    tools=tools,
)

print("Full response:")
print(response)

msg = response["message"]
if msg.get("tool_calls"):
    print("\nPASS: model emitted tool_calls:")
    for call in msg["tool_calls"]:
        print(" -", call["function"]["name"], call["function"]["arguments"])
else:
    print("\nFAIL: no tool_calls in response, model answered directly:")
    print(msg.get("content"))
