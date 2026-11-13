"""Tool-call smoke test. The client executes only this explicit, harmless demo tool."""

import argparse
import json
import os

from openai import OpenAI

parser = argparse.ArgumentParser()
parser.add_argument("--model", required=True)
parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
args = parser.parse_args()
client = OpenAI(base_url=args.base_url, api_key=os.environ["GP_API_KEY"], max_retries=0)
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_demo_weather",
            "description": "取得展示用天氣，非即時資料。",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
                "additionalProperties": False,
            },
        },
    }
]
messages = [{"role": "user", "content": "請呼叫展示用天氣工具查詢台北，並摘要結果。"}]
for step in range(5):
    response = client.chat.completions.create(
        model=args.model,
        messages=messages,
        tools=tools,
        parallel_tool_calls=False,
        tool_choice="required" if step == 0 else "auto",
    )
    message = response.choices[0].message
    messages.append(message.model_dump(exclude_none=True))
    if not message.tool_calls:
        print(message.content)
        break
    for call in message.tool_calls:
        if call.function.name != "get_demo_weather":
            raise RuntimeError("Unknown tool; refusing to execute")
        arguments = json.loads(call.function.arguments)
        result = {"city": arguments["city"], "weather": "晴", "source": "demo fixture"}
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call.id,
                "content": json.dumps(result, ensure_ascii=False),
            }
        )
else:
    raise RuntimeError("Agent exceeded the five-turn demo limit")
