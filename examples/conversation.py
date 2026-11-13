"""Plain-text system instructions and native Gemini conversation continuation."""

import argparse
import os
import uuid

from openai import OpenAI

parser = argparse.ArgumentParser()
parser.add_argument("--model", required=True)
parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
args = parser.parse_args()
client = OpenAI(base_url=args.base_url, api_key=os.environ["GP_API_KEY"], max_retries=0)
conversation_id = "demo-" + uuid.uuid4().hex
messages = [
    {"role": "system", "content": "使用繁體中文，回答保持簡潔。"},
    {"role": "user", "content": "請記住，我的代號是小熊。"},
]
for question in (None, "我的代號是什麼？"):
    if question:
        messages.append({"role": "user", "content": question})
    response = client.chat.completions.create(
        model=args.model, messages=messages, extra_body={"conversation_id": conversation_id}
    )
    message = response.choices[0].message
    print(message.content)
    messages.append(message.model_dump(exclude_none=True))
