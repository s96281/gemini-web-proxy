"""GP_API_KEY=... python examples/chat.py --model <id>"""

import argparse
import os

from openai import OpenAI

parser = argparse.ArgumentParser()
parser.add_argument("--model", required=True)
parser.add_argument("--base-url", default="http://127.0.0.1:8000/v1")
parser.add_argument("prompt", nargs="?", default="請用繁體中文介紹你自己。")
args = parser.parse_args()
client = OpenAI(base_url=args.base_url, api_key=os.environ["GP_API_KEY"], max_retries=0)
for chunk in client.chat.completions.create(
    model=args.model,
    messages=[{"role": "user", "content": args.prompt}],
    stream=True,
):
    print(chunk.choices[0].delta.content or "", end="", flush=True)
print()
