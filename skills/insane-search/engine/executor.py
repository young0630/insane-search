"""Capability-matched executor for fallback attempts.

The fetch_chain's probe/grid phase uses curl_cffi directly. When curl can't
punch through (JS challenge, real-TLS detection), this module routes to the
right browser executor based on the profile's `capabilities_needed` tags:

    needs_real_tls_stack + needs_js_exec  → playwright_real_chrome.js
    needs_js_exec only                    → Playwright MCP (if available)
    needs_mobile_context (+ real_tls)     → playwright_mobile_chrome.js

The JS templates live in `engine/templates/` and accept only generic
parameters ({{url}}, {{waitSelector}}, {{profileDir}}, {{device}}). No
site-specific logic.

Playwright MCP invocation requires caller's tool access; this module
provides the subprocess path for local JS templates but only stubs the MCP
path (MCP must be driven from the Claude session itself).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
from typing import Optional

from .validators import Verdict, validate
from .waf_detector import load_profile
from .fetch_chain import Attempt


TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")


def _profile_dir_for(url: str, choice: str) -> str:
    """Per-host + per-device Chrome profile directory.

    The host is hashed (never stored as a site name) so the No-Site-Name Rule
    holds while each host keeps an isolated, reusable profile. Desktop and
    mobile get separate subdirs so emulation state never bleeds across.
    """
    import hashlib
    from urllib.parse import urlsplit
    host = (urlsplit(url).hostname or "unknown").lower()
    host_hash = hashlib.sha1(host.encode("utf-8", "ignore")).hexdigest()[:16]
    device = "mobile" if "mobile" in choice else "desktop"
    return os.path.join(tempfile.gettempdir(), ".insane_pw", host_hash, device)


def _node_available() -> bool:
    return shutil.which("node") is not None


def _chrome_channel_available() -> bool:
    """Heuristic: try `node -e` to import playwright. Fallback to True, let script fail loudly."""
    if not _node_available():
        return False
    if shutil.which("npx") is None:
        return False
    return True


def _pick_executor(capabilities: list[str], device_class: str) -> str:
    caps = set(capabilities or [])
    if device_class == "mobile" or "needs_mobile_context" in caps:
        if "needs_real_tls_stack" in caps:
            return "playwright_mobile_chrome"
        return "playwright_mcp_mobile"
    if "needs_real_tls_stack" in caps:
        return "playwright_real_chrome"
    if "needs_js_exec" in caps:
        return "playwright_mcp"
    return "playwright_real_chrome"  # safest general fallback


def _run_python_patchright(
    url: str,
    *,
    profile_dir: str = "",
    wait_selector: Optional[str] = None,
    timeout: int = 90,
    headless: bool = False,
    device: str = "",
) -> tuple[int, str, str]:
    """Fetch a URL using Python Patchright with stealth patches."""
    try:
        import patchright
        from patchright.sync_api import sync_playwright
    except ImportError:
        return 127, "", "patchright not installed (pip install patchright)"

    t0 = time.time()
    deadline = t0 + timeout

    try:
        with sync_playwright() as p:
            ctx_opts = {
                "channel": "chrome",
                "headless": headless,
                "viewport": None,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            pd = profile_dir or tempfile.mkdtemp(prefix="insane_pw_")
            ctx = p.chromium.launch_persistent_context(pd, **ctx_opts)
            page = ctx.new_page()

            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => false });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
            """)

            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                root_url = f"{parsed.scheme}://{parsed.netloc}/"
                if root_url != url:
                    page.goto(root_url, wait_until="domcontentloaded",
                              timeout=min(30000, int((deadline - time.time()) * 1000)))
                    page.wait_for_timeout(3500)
            except Exception:
                pass

            rem = max(1000, int((deadline - time.time()) * 1000))
            resp = page.goto(url, wait_until="domcontentloaded", timeout=rem)
            page.wait_for_timeout(2500)

            if wait_selector:
                try:
                    rem_sel = max(1000, int((deadline - time.time()) * 1000))
                    page.wait_for_selector(wait_selector, timeout=rem_sel)
                except Exception:
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=rem)
                        page.wait_for_timeout(2000)
                        rem_sel = max(1000, int((deadline - time.time()) * 1000))
                        page.wait_for_selector(wait_selector, timeout=rem_sel)
                    except Exception:
                        pass
            else:
                page.wait_for_timeout(2000)

            html = page.content()
            cookies = ctx.cookies()

            try:
                from .transport import POOL, pool_enabled, _host_of
                if pool_enabled() and cookies:
                    ua = page.evaluate("() => navigator.userAgent")
                    cookie_list = [{"name": c["name"], "value": c["value"],
                                    "domain": c.get("domain", "")} for c in cookies]
                    POOL.inject_cookies(_host_of(url), "chrome", cookie_list, user_agent=ua)
            except Exception:
                pass

            ctx.close()
            return 0, html, ""
    except Exception as e:
        return 1, "", f"patchright: {type(e).__name__}: {e}"


def _run_python_camoufox(
    url: str,
    *,
    profile_dir: str = "",
    wait_selector: Optional[str] = None,
    timeout: int = 90,
    headless: bool = True,
) -> tuple[int, str, str]:
    """Fetch a URL using Camoufox (hardened Firefox — no CDP surface, different detection profile).

    Falls back to this when Chromium-based Patchright is detected/blocked.
    Camoufox is a Firefox fork with built-in stealth: navigator.webdriver,
    plugins, WebGL, canvas fingerprinting all patched at the binary level.
    """
    try:
        from camoufox import NewBrowser, AsyncNewBrowser
    except ImportError:
        return 127, "", "camoufox not installed (pip install camoufox && camoufox fetch)"

    t0 = time.time()
    deadline = t0 + timeout

    try:
        # Camoufox sync API: NewBrowser is a context manager
        with NewBrowser(
            headless=headless,
            humanize=True,  # built-in stealth + human-like behavior
            geoip=True,    # match timezone to IP
        ) as browser:
            page = browser.new_page()

            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                root_url = f"{parsed.scheme}://{parsed.netloc}/"
                if root_url != url:
                    page.goto(root_url, wait_until="domcontentloaded",
                              timeout=min(30000, int((deadline - time.time()) * 1000)))
                    page.wait_for_timeout(3500)
            except Exception:
                pass

            rem = max(5000, int((deadline - time.time()) * 1000))
            resp = page.goto(url, wait_until="domcontentloaded", timeout=rem)
            page.wait_for_timeout(2500)

            if wait_selector:
                try:
                    rem_sel = max(2000, int((deadline - time.time()) * 1000))
                    page.wait_for_selector(wait_selector, timeout=rem_sel)
                except Exception:
                    pass
            else:
                page.wait_for_timeout(2000)

            html = page.content()
            return 0, html, ""
    except Exception as e:
        return 1, "", f"camoufox: {type(e).__name__}: {e}"


class _FakeResp:
    """Minimal response shim so validators.validate() works on Playwright HTML."""
    def __init__(self, html: str, status: int = 200, final_url: str = ""):
        self.text = html
        self.status_code = status
        self.url = final_url
        self.cookies = _FakeCookies()
        self.headers = {}


class _FakeCookies:
    class _Jar:
        def __iter__(self):
            return iter([])
    def __init__(self):
        self.jar = self._Jar()
    def __iter__(self):
        return iter([])


def run_playwright_fallback(
    url: str,
    *,
    profile_id: str,
    success_selectors: Optional[list[str]] = None,
    device_class: str = "auto",
    timeout: int = 90,
    profile_dir: Optional[str] = None,
    force_executor: Optional[str] = None,
) -> tuple[Attempt, str]:
    """Invoke the appropriate Playwright executor.

    force_executor: caller-specified executor name (from a profile's
    `fallback_when_challenge` list). When set, it overrides capability-based
    inference. Recognized values: "playwright_real_chrome",
    "playwright_mobile_chrome", "playwright_mcp".

    Returns (Attempt, html_content). Attempt.verdict reflects validation.
    """
    profile = load_profile(profile_id)
    capabilities = profile.get("capabilities_needed") or []
    choice = force_executor or _pick_executor(capabilities, device_class)

    t0 = time.time()
    att = Attempt(
        phase="fallback",
        executor=choice,
        url=url,
        url_transform="original",
        impersonate=None,
        referer="",
    )

    if choice.startswith("playwright_mcp"):
        att.error = (
            "Playwright MCP must be invoked from the Claude session — "
            "call mcp__playwright__* tools directly instead of fetch_chain."
        )
        att.verdict = Verdict.UNKNOWN.value
        att.elapsed_s = round(time.time() - t0, 3)
        return att, ""

    # Python Patchright path (preferred — no Node.js dependency)
    if choice in ("playwright_real_chrome", "playwright_mobile_chrome"):
        try:
            import patchright  # noqa: F401
            pd = profile_dir or _profile_dir_for(url, choice)
            wait_sel = success_selectors[0] if success_selectors else None
            dev = "iPhone 13 Pro" if choice == "playwright_mobile_chrome" else ""
            rc, html, err = _run_python_patchright(
                url,
                profile_dir=pd,
                wait_selector=wait_sel,
                timeout=timeout,
                device=dev,
            )
            if rc == 0 and html:
                resp = _FakeResp(html, status=200, final_url=url)
                vr = validate(resp, success_selectors=success_selectors)
                att.status = 200
                att.body_size = len(html)
                att.verdict = vr.verdict.value
                att.reasons = list(vr.reasons)
                return att, html

            # Patchright failed — try Camoufox (Firefox, no CDP surface)
            try:
                import camoufox  # noqa: F401
                rc, html, err = _run_python_camoufox(
                    url,
                    wait_selector=wait_sel,
                    timeout=timeout,
                    headless=False,
                )
                if rc == 0 and html:
                    resp = _FakeResp(html, status=200, final_url=url)
                    vr = validate(resp, success_selectors=success_selectors)
                    att.status = 200
                    att.body_size = len(html)
                    att.verdict = vr.verdict.value
                    att.reasons = list(vr.reasons)
                    return att, html
            except ImportError:
                pass
            except Exception:
                pass
            
            # Both Python paths failed — fall through to Node template
        except ImportError:
            pass  # No patchright, fall through to Node
        except Exception:
            pass  # Any other error, fall through to Node

    # Node.js template path (fallback)
    if not _chrome_channel_available():
        att.error = "node/npx not available for local Playwright template"
        att.verdict = Verdict.UNKNOWN.value
        att.elapsed_s = round(time.time() - t0, 3)
        return att, ""

    template_map = {
        "playwright_real_chrome": "playwright_real_chrome.js",
        "playwright_mobile_chrome": "playwright_mobile_chrome.js",
    }
    template = template_map.get(choice)
    if template is None:
        att.error = f"no template for executor {choice}"
        att.verdict = Verdict.UNKNOWN.value
        att.elapsed_s = round(time.time() - t0, 3)
        return att, ""

    args: dict = {
        "url": url,
        # Per-host + per-device profile isolation. A single shared profile dir
        # (the old default) leaked cookies/storage across hosts and caused
        # profile-lock collisions when two fallbacks ran concurrently. Hashing
        # the host (not storing it) keeps the No-Site-Name Rule intact while
        # letting a host reuse its own warm storageState across calls.
        "profileDir": profile_dir or _profile_dir_for(url, choice),
        "timeout": timeout * 1000,
    }
    if choice == "playwright_mobile_chrome":
        args["device"] = "iPhone 13 Pro"
    if success_selectors:
        args["waitSelector"] = success_selectors[0]

    rc, stdout, stderr = _run_node_template(template, args, timeout=timeout + 10)
    att.elapsed_s = round(time.time() - t0, 3)

    if rc != 0 or not stdout:
        att.error = (stderr or "no stdout")[:300]
        att.verdict = Verdict.UNKNOWN.value
        return att, ""

    # stdout is a JSON envelope {html, finalUrl, status, cookies, userAgent}.
    # Fall back to treating raw stdout as HTML for forward/backward compat.
    html, final_url, status, cookies, user_agent, automation = _parse_envelope(stdout, url)

    resp = _FakeResp(html, status=status, final_url=final_url)
    vr = validate(resp, success_selectors=success_selectors)
    att.status = status
    att.body_size = len(html)
    att.verdict = vr.verdict.value
    att.reasons = list(vr.reasons) + ([f"automation:{automation}"] if automation else [])
    att.url = final_url or url

    # Cookie bridge: a browser that cleared a JS challenge yields exactly the
    # cookies + UA a plain HTTP client needs. Seed the curl_cffi pool so
    # subsequent same-host pages are collected cheaply (FlareSolverr pattern).
    if vr.verdict in (Verdict.STRONG_OK, Verdict.WEAK_OK) and cookies:
        _bridge_cookies_to_pool(url, cookies, user_agent)

    return att, html


def _parse_envelope(stdout: str, url: str):
    """Return (html, final_url, status, cookies, user_agent) from a JSON
    envelope, or treat stdout as raw HTML if it isn't JSON."""
    import json
    s = stdout.lstrip()
    if s[:1] == "{":
        try:
            env = json.loads(s)
            html = env.get("html", "") or ""
            final_url = env.get("finalUrl", "") or url
            status = int(env.get("status") or 0) or 200
            cookies = env.get("cookies") or []
            user_agent = env.get("userAgent") or None
            automation = env.get("automation") or None
            return html, final_url, status, cookies, user_agent, automation
        except Exception:
            pass
    return stdout, url, 200, [], None, None


def _bridge_cookies_to_pool(url: str, cookies: list, user_agent: Optional[str]) -> None:
    try:
        from .transport import POOL, pool_enabled, _host_of
        if not pool_enabled():
            return
        # Browser is real Chrome → seed the "chrome" curl identity for this host.
        POOL.inject_cookies(_host_of(url), "chrome", cookies, user_agent=user_agent)
    except Exception:
        pass
