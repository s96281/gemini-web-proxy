import asyncio
import json
import re
from dataclasses import asdict, dataclass

from playwright.async_api import async_playwright

from ..config import Settings
from ..errors import ProxyError
from .base import Model


@dataclass
class Selectors:
    editor: str = 'rich-textarea [contenteditable="true"], textarea'
    send: str = (
        'button.send-button, button[aria-label="Send message"], button[aria-label="傳送訊息"]'
    )
    new_chat: str = 'a[href="/app"], button[aria-label="New chat"], button[aria-label="發起新對話"]'
    model_trigger: str = (
        '[data-test-id="bard-mode-menu-button"], button[aria-label*="mode" i], '
        'button[aria-label*="模型"]'
    )
    model_option: str = '[role="menuitemradio"], [role="menuitem"]'
    model_label: str = '.mode-title, [data-test-id="mode-title"]'
    response: str = "model-response"
    response_text: str = ".markdown, .response-content"
    completed: str = 'button[aria-label*="Copy" i], button[aria-label*="複製"]'
    stop: str = 'button[data-test-id="stop-button"], button[aria-label*="Stop response" i]'
    challenge: str = 'iframe[src*="recaptcha"], form[action*="/sorry/"]'
    quota: str = '[data-test-id="usage-limit-reached"]'
    login: str = 'a[href*="accounts.google.com"], button[data-test-id="sign-in-button"]'


class BrowserBackend:
    name = "browser"

    def __init__(self, settings: Settings, *, url="https://gemini.google.com/app"):
        self.settings = settings
        self.url = url
        self.selectors = Selectors()
        if settings.browser_selectors_file:
            overrides = json.loads(settings.browser_selectors_file.read_text())
            self.selectors = Selectors(**(asdict(self.selectors) | overrides))
        self.playwright = None
        self.context = None
        self.page = None
        self.selected_model = None

    async def _open(self):
        if self.page is not None and not self.page.is_closed():
            return
        await self.close()
        self.settings.browser_profile.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.playwright = await async_playwright().start()
        try:
            # Use full Chromium in both headed login and headless inference. This avoids
            # requiring a second headless-shell browser installation in the image.
            kwargs = {"headless": self.settings.browser_headless, "channel": "chromium"}
            if self.settings.browser_executable:
                kwargs["executable_path"] = self.settings.browser_executable
            if self.settings.upstream_proxy:
                kwargs["proxy"] = {"server": self.settings.upstream_proxy}
            self.context = await self.playwright.chromium.launch_persistent_context(
                str(self.settings.browser_profile.resolve()),
                **kwargs,
            )
            self.page = await self.context.new_page()
            self.page.set_default_timeout(self.settings.preflight_timeout * 1000)
        except BaseException:
            await self.close()
            raise

    async def _check_errors(self):
        p, s = self.page, self.selectors
        if "/sorry/" in p.url or await p.locator(s.challenge).count():
            raise ProxyError(
                "upstream_challenge", "Complete the browser verification manually.", 503
            )
        if await p.locator(s.quota).count():
            raise ProxyError(
                "upstream_rate_limit", "Gemini quota exceeded; no automatic resend.", 429
            )

    async def _fresh_page(self):
        await self._open()
        await self.page.goto(self.url, wait_until="domcontentloaded")
        await self._check_errors()
        if "accounts.google.com" in self.page.url:
            raise ProxyError("browser_login_required", "Browser requires manual Google login.", 503)
        login = self.page.locator(self.selectors.login).first
        if await login.count() and await login.is_visible():
            raise ProxyError("browser_login_required", "Browser requires manual Google login.", 503)
        try:
            await self.page.locator(self.selectors.editor).first.wait_for(state="visible")
        except Exception:
            raise ProxyError(
                "browser_login_required",
                "Browser login required: stop service and run gemini-proxy login.",
                503,
            ) from None
        if await self.page.locator(self.selectors.response).count():
            link = self.page.locator(self.selectors.new_chat).first
            if not await link.count():
                raise ProxyError(
                    "browser_ui_changed", "Cannot establish a new browser conversation."
                )
            await link.click()
            await self.page.wait_for_function(
                "selector => document.querySelectorAll(selector).length === 0",
                arg=self.selectors.response,
            )
        editor = self.page.locator(self.selectors.editor).first
        if await editor.evaluate("e => e.value || e.textContent"):
            await editor.fill("")
        self.selected_model = None

    async def _options(self):
        p, s = self.page, self.selectors
        await p.locator(s.model_trigger).first.click()
        await p.locator(s.model_option).first.wait_for(state="visible")
        options = []
        for option in await p.locator(s.model_option).all():
            if not await option.is_visible():
                continue
            if await option.get_attribute("aria-disabled") == "true" or await option.is_disabled():
                continue
            label_el = option.locator(s.model_label).first
            label = (
                await label_el.inner_text() if await label_el.count() else await option.inner_text()
            ).strip()
            if label:
                options.append((label, option))
        if not options:
            raise ProxyError("browser_ui_changed", "Cannot discover browser models.")
        return options

    @staticmethod
    def model_id(label):
        slug = re.sub(r"[^\w-]+", "-", label.lower()).strip("-")
        return "web-" + slug

    async def models(self):
        try:
            async with asyncio.timeout(self.settings.preflight_timeout):
                await self._fresh_page()
                options = await self._options()
                result = [Model(self.model_id(label), label) for label, _ in options]
                labels = {label for label, _ in options}
                result += [
                    Model(key, label)
                    for key, label in self.settings.browser_model_map.items()
                    if label in labels and key not in {m.id for m in result}
                ]
                await self.page.keyboard.press("Escape")
                return result
        except ProxyError:
            raise
        except Exception:
            await self.close()
            raise ProxyError(
                "browser_unavailable", "Browser model discovery failed; check login/UI.", 503
            ) from None

    async def prepare(self, model):
        try:
            async with asyncio.timeout(self.settings.preflight_timeout):
                await self._fresh_page()
                options = await self._options()
                label = self.settings.browser_model_map.get(model)
                matches = [
                    (name, option)
                    for name, option in options
                    if (name == label if label else self.model_id(name) == model)
                ]
                if len(matches) != 1:
                    raise ProxyError(
                        "model_not_found",
                        "Cannot confirm this model in browser. "
                        "Configure GP_BROWSER_MODEL_MAP with an exact UI label.",
                        404,
                    )
                label, option = matches[0]
                await option.click()
                # A selected menu item alone is insufficient: verify the closed trigger too.
                trigger = self.page.locator(self.selectors.model_trigger).first
                selected = " ".join(
                    [await trigger.inner_text(), await trigger.get_attribute("aria-label") or ""]
                )
                confirmed = {line.strip().casefold() for line in selected.splitlines()}
                confirmed.add((await trigger.inner_text()).strip().casefold())
                if label.casefold() not in confirmed:
                    raise ProxyError(
                        "browser_model_unconfirmed", "Browser did not confirm the selected model."
                    )
                self.selected_model = model
        except ProxyError:
            raise
        except Exception:
            await self.close()
            raise ProxyError(
                "browser_unavailable", "Browser preparation failed; check login/UI.", 503
            ) from None

    async def stream(self, prompt, model):
        if self.selected_model != model:
            raise ProxyError("browser_model_unconfirmed", "Browser model was not prepared.")
        s, p = self.selectors, self.page
        previous = ""
        finished = False
        try:
            await p.locator(s.editor).first.fill(prompt)
            # From this click onward an error must never trigger a fallback/retry.
            await p.locator(s.send).first.click()
            while True:
                await self._check_errors()
                responses = p.locator(s.response)
                if await responses.count():
                    response = responses.last
                    text_el = response.locator(s.response_text).first
                    text = await text_el.inner_text() if await text_el.count() else ""
                    if text and not text.startswith(previous):
                        raise ProxyError(
                            "browser_output_rewritten",
                            "Browser rewrote text already streamed; request stopped.",
                        )
                    if text != previous:
                        yield text[len(previous) :]
                        previous = text
                    done = response.locator(s.completed).first
                    stop = p.locator(s.stop).first
                    stopping = await stop.count() and await stop.is_visible()
                    if previous and await done.count() and await done.is_visible() and not stopping:
                        finished = True
                        break
                await asyncio.sleep(0.15)
        except ProxyError:
            raise
        except Exception:
            raise ProxyError(
                "browser_generation_failed",
                "Browser generation failed; request may already be processed.",
            ) from None
        finally:
            self.selected_model = None
            if not finished:
                # Closing the page prevents a cancelled turn from contaminating the next request.
                await self.abort()

    async def abort(self):
        await self.close()

    async def close(self):
        context, self.context = self.context, None
        playwright, self.playwright = self.playwright, None
        self.page = None
        self.selected_model = None
        try:
            if context:
                await context.close()
        finally:
            if playwright:
                await playwright.stop()
