import argparse
import asyncio
import json
import os
from pathlib import Path

from playwright.async_api import async_playwright

from .config import Settings


async def login(profile: Path, cookie_file: Path, executable: str | None):
    profile.mkdir(parents=True, exist_ok=True, mode=0o700)
    async with async_playwright() as p:
        kwargs = {"headless": False}
        if executable:
            kwargs["executable_path"] = executable
        context = await p.chromium.launch_persistent_context(str(profile.resolve()), **kwargs)
        try:
            page = await context.new_page()
            await page.goto("https://gemini.google.com/app")
            print("請在瀏覽器完成 Google 登入，確認能使用 Gemini，然後回到終端機按 Enter。")
            await asyncio.to_thread(input)
            cookies = await context.cookies("https://gemini.google.com")
            values = {
                c["name"]: c["value"]
                for c in cookies
                if c["name"] in ("__Secure-1PSID", "__Secure-1PSIDTS")
            }
            if not values.get("__Secure-1PSID"):
                raise SystemExit("沒有找到 Gemini Cookie；未儲存 Cookie 檔。")
            cookie_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = cookie_file.with_suffix(".tmp")
            temp.write_text(json.dumps(values))
            temp.chmod(0o600)
            temp.replace(cookie_file)
            print("已保存登入 profile 與 Cookie。請關閉此命令後再啟動服務。")
        finally:
            await context.close()


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description="Gemini Web → OpenAI API")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    auth = sub.add_parser("login")
    auth.add_argument("--profile", type=Path)
    auth.add_argument("--cookie-file", type=Path)
    args = parser.parse_args()
    if args.command == "serve":
        import uvicorn

        uvicorn.run(
            "gemini_proxy.api:factory",
            factory=True,
            host=args.host,
            port=args.port,
            access_log=False,
        )
    else:
        settings = Settings()
        asyncio.run(
            login(
                args.profile or settings.browser_profile,
                args.cookie_file or settings.cookie_file,
                settings.browser_executable,
            )
        )


if __name__ == "__main__":
    main()
