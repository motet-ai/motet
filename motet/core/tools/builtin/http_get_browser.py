"""
Motet - Browser HTTP GET Tool

Copyright (c) 2024-2026 Motet Contributors
Licensed under the Functional Source License, Version 1.1, or a commercial license. See LICENSE.

Author: Matt Chisholm <matt@motet.dev>
Last Modified: 2026-08-23

Description:
    Browser-based HTTP GET tool for the Motet distributed framework.
    Provides comprehensive web content retrieval using Playwright browser
    automation with JavaScript execution, screenshot capabilities, and
    dynamic content processing. Includes security controls and content extraction.
    ``main_content`` keeps a real page (80k chars) so official docs are not
    silently sliced at 10k; ``content_length`` is the pre-clip size and
    ``truncated`` is set when the extract rail hits. Observation clipping and
    artifact offload still decide what the model sees.

Dependencies:
    - json: Data serialization and processing
    - pydantic: Data validation and model definitions
    - typing: Type hints and annotations
    - Playwright browser automation (async API with run_async_safe for gevent/eventlet compatibility)

Usage:
    from motet.core.tools.builtin.http_get_browser import run_browser
    
    # Get browser content
    result = run_browser({
        "url": "https://example.com",
        "wait_for": ".content",
        "screenshot": True,
        "execute_js": "return document.title"
    })

Notes:
    - Provides browser-based HTTP GET capabilities using Playwright
    - Includes JavaScript execution and dynamic content processing
    - Supports screenshot capture and element waiting
    - Includes security controls and domain filtering
    - Supports headless and non-headless browser modes
    - Integrates with tool registry and protocol system
    - Includes comprehensive error handling and content analysis
    - ``main_content`` is bounded at MAIN_CONTENT_MAX_CHARS (80k), not 10k.
      A fat page sets truncated=true and keeps content_length of the full
      extract; tool_execution clips the live observation and offloads the rest.
"""


from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Tuple

import structlog
from pydantic import BaseModel, Field

logger = structlog.get_logger(__name__)

from ...config import Config
from ..cache_control import attach_snapshot_cache_control
from ..protocol import ok, err
from ..registry import ToolRegistry

# Enough for an official pricing/docs page. The old 10k slice threw the
# rest away before tool_execution could offload it, which is why MCP
# browser_evaluate (25–50k) looked like a better extractor. Observation
# clipping still applies; this is what we store.
MAIN_CONTENT_MAX_CHARS = 80_000


def _bound_main_content(text: str) -> Dict[str, Any]:
    """Clip extract text and report the pre-clip size.

    ``content_length`` is always the raw extract so callers can see that a
    page was larger than the rail. ``truncated`` is true only when we cut.
    """
    original = len(text or "")
    truncated = original > MAIN_CONTENT_MAX_CHARS
    body = (text or "")[:MAIN_CONTENT_MAX_CHARS] if truncated else (text or "")
    return {
        "main_content": body,
        "content_length": original,
        "truncated": truncated,
    }


def _resolve_chromium_path() -> Tuple[Optional[str], str, List[str]]:
    """
    Resolve Chromium executable for Playwright by checking env and scanning
    PLAYWRIGHT_BROWSERS_PATH. Does not install; callers get paths_tried for errors.
    Returns (executable_path or None, display_string for browser_used, paths_tried).
    """
    pw_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/tmp/playwright-browsers")
    paths_tried: List[str] = []

    def _check(path: Optional[str]) -> Optional[str]:
        if not path:
            return None
        try:
            if os.path.isfile(path):
                return os.path.realpath(path)
            return None
        except OSError:
            return None

    # 1) Env-set binary
    env_bin = os.environ.get("CHROME_BIN") or os.environ.get("CHROME_PATH")
    if env_bin:
        paths_tried.append(env_bin)
    real = _check(env_bin)
    if real:
        return (real, real, paths_tried)

    # 2) Scan PLAYWRIGHT_BROWSERS_PATH for chromium (prefer chrome over headless_shell)
    def _scan() -> Optional[str]:
        if not os.path.isdir(pw_path):
            return None
        found: List[Tuple[str, int]] = []  # (path, preference: 0=chrome, 1=headless_shell)
        for root, _dirs, files in os.walk(pw_path):
            if "chrome-linux" not in root:
                continue
            for name in ("chrome", "headless_shell"):
                if name in files:
                    p = os.path.join(root, name)
                    if os.path.isfile(p) and os.access(p, os.X_OK):
                        found.append((p, 0 if name == "chrome" else 1))
        if not found:
            return None
        found.sort(key=lambda x: x[1])
        return os.path.realpath(found[0][0])

    paths_tried.append(pw_path)
    candidate = _scan()
    if candidate:
        return (candidate, candidate, paths_tried)
    paths_tried.append("(playwright default)")
    return (None, "(playwright default)", paths_tried)


class BrowserParams(BaseModel):
    url: str
    timeout: float = Field(default=30, ge=0.1, le=120)
    wait_for: Optional[str] = Field(default=None, description="CSS selector to wait for before extracting content")
    wait_timeout: float = Field(default=10, description="Timeout for waiting for elements")
    extract_strategy: str = Field(default="auto", description="Content extraction strategy")
    include_links: bool = Field(default=False, description="Extract links from the page")
    include_images: bool = Field(default=False, description="Extract images from the page")
    screenshot: bool = Field(default=False, description="Take a screenshot (returns base64)")
    execute_js: Optional[str] = Field(
        default=None,
        description=(
            "Optional JavaScript source code string to evaluate in the page. "
            "Omit this field or use null unless custom JavaScript is required; "
            "never use true or false."
        ),
    )
    headless: bool = Field(default=True, description="Run browser in headless mode")


def run_browser(params: Dict[str, Any]) -> Dict[str, Any]:
    """Browser-based HTTP GET with JavaScript execution."""
    url = params.get("url")
    timeout = float(params.get("timeout", 30))
    wait_for = params.get("wait_for")
    wait_timeout = float(params.get("wait_timeout", 10))
    extract_strategy = params.get("extract_strategy", "auto")
    include_links = params.get("include_links", False)
    include_images = params.get("include_images", False)
    screenshot = params.get("screenshot", False)
    execute_js = params.get("execute_js")
    headless = params.get("headless", True)
    
    if not url:
        return err("url is required")
    
    # Security check
    try:
        cfg = Config()
        from ...security import is_host_allowed
        if not is_host_allowed(url, cfg.http_tool_allow_domains, cfg.http_tool_deny_domains):
            return err("domain not allowed" if cfg.http_tool_allow_domains else "domain denied")
    except Exception as e:  # fail-open: proceed if config/security unavailable
        logger.debug("security_check_skipped", url=url, error=str(e))
    
    try:
        # Try to import playwright (async API)
        from playwright.async_api import async_playwright
    except ImportError:
        return err("Playwright not available. Install with: pip install playwright && playwright install")

    # Resolve Chromium path (env then scan; no install). paths_tried is reported in errors.
    resolved_path, browser_used_display, paths_tried = _resolve_chromium_path()
    
    import time as _t
    _t0 = _t.perf_counter()
    
    # Define the browser execution function as async
    # With asyncio-gevent policy configured at worker startup,
    # this async code works seamlessly on both fork/threads and gevent pools
    async def _run_browser_async():
        async with async_playwright() as p:
            launch_options = dict(
                headless=headless,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-extensions',
                ],
            )
            if resolved_path:
                launch_options["executable_path"] = resolved_path
            browser_used = browser_used_display
            browser = await p.chromium.launch(**launch_options)
            
            try:
                # Create context with realistic settings
                context = await browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                )
                
                page = await context.new_page()
                
                # Navigate to URL
                response = await page.goto(url, timeout=timeout * 1000, wait_until='domcontentloaded')
                
                # Wait for specific element if requested
                if wait_for:
                    try:
                        await page.wait_for_selector(wait_for, timeout=wait_timeout * 1000)
                    except Exception as e:
                        # Continue even if wait fails
                        try:
                            logger.warning("browser_wait_failed", selector=wait_for, error=str(e))
                        except Exception as log_err:  # best-effort: logging must not fail
                            logger.debug("browser_wait_log_failed", error=str(log_err))
                
                # Execute custom JavaScript if provided
                js_result = None
                if execute_js:
                    try:
                        js_result = await page.evaluate(execute_js)
                    except Exception as e:
                        js_result = {"error": str(e)}
                
                # Extract content
                extracted_content = await _extract_browser_content_async(
                    page, extract_strategy, include_links, include_images
                )
                
                # Take screenshot if requested
                screenshot_data = None
                if screenshot:
                    try:
                        screenshot_bytes = await page.screenshot(type='png', full_page=True)
                        import base64
                        screenshot_data = base64.b64encode(screenshot_bytes).decode('utf-8')
                    except Exception as e:
                        screenshot_data = {"error": str(e)}
                
                _dur = round((_t.perf_counter() - _t0) * 1000, 2)
                
                # Log success
                try:
                    logger.info("http_get_browser_done",
                                url=url, status=response.status if response else 0, ms=_dur)
                except Exception as log_err:  # best-effort: logging must not fail
                    logger.debug("success_log_failed", error=str(log_err))
                
                # Build result
                result = {
                    "status": response.status if response else 200,
                    "url": page.url,  # Final URL after redirects/JS navigation
                    "title": await page.title(),
                    "browser_used": browser_used,
                    "extraction_strategy": extract_strategy,
                    **extracted_content
                }
                
                if js_result is not None:
                    result["javascript_result"] = js_result
                
                if screenshot_data:
                    result["screenshot"] = screenshot_data
                
                return attach_snapshot_cache_control("core.http_get_browser", result)
            
            finally:
                await browser.close()
    
    # Execute browser operation using centralized run_async_safe (gevent-aware)
    try:
        from ...utils.async_helpers import run_async_safe
        return run_async_safe(_run_browser_async())
            
    except Exception as e:
        _dur = round((_t.perf_counter() - _t0) * 1000, 2)
        error_msg = str(e)
        
        # Check if it's a timeout error
        is_timeout = 'timeout' in error_msg.lower() or 'TimeoutError' in str(type(e).__name__)
        
        try:
            if is_timeout:
                logger.error("http_get_browser_timeout", url=url, ms=_dur, error=error_msg)
            else:
                logger.error("http_get_browser_failed", url=url, error=error_msg, error_type=type(e).__name__, ms=_dur)
        except Exception as log_err:  # best-effort: logging must not fail
            logger.debug("error_log_failed", error=str(log_err))
        
        meta = {"browser_used": browser_used_display, "paths_tried": paths_tried}
        suffix = f" (browser_used: {browser_used_display}, paths_tried: {paths_tried})"
        if is_timeout:
            return err(f"Browser request timeout after {timeout}s: {error_msg}{suffix}", meta=meta)
        else:
            return err(f"Browser request failed: {error_msg}{suffix}", meta=meta)


async def _extract_browser_content_async(
    page, 
    extract_strategy: str, 
    include_links: bool, 
    include_images: bool
) -> Dict[str, Any]:
    """Extract content from browser page (async version for Playwright async API)."""
    
    if extract_strategy == "raw":
        html = await page.content()
        return {"raw_html": html}
    
    # Get text content
    try:
        # Try to get main content areas
        main_content = ""
        
        # Try common content selectors
        content_selectors = [
            'main', 'article', '[role="main"]', '.content', '#content', 
            '.main-content', '.post-content', '.entry-content', '.article-content'
        ]
        
        for selector in content_selectors:
            try:
                element = await page.query_selector(selector)
                if element:
                    main_content = await element.inner_text()
                    break
            except Exception as e:  # fallback: try next selector
                logger.debug("selector_failed", selector=selector, error=str(e))
                continue
        
        # Fallback to body content
        if not main_content:
            try:
                body = await page.query_selector('body')
                if body:
                    main_content = await body.inner_text()
            except Exception as e:  # fallback: use empty if body fails
                logger.debug("body_extract_failed", error=str(e))
                main_content = ""
        
        result = {
            "content_type": "browser_extracted",
            **_bound_main_content(main_content),
        }
        
        # Extract meta information
        try:
            meta_description = await page.get_attribute('meta[name="description"]', 'content')
            if meta_description:
                result["description"] = meta_description
        except Exception as e:  # optional: meta description enrichment
            logger.debug("meta_description_failed", error=str(e))
        
        # Extract links if requested
        if include_links:
            try:
                links = await page.evaluate("""
                    () => {
                        const seen = new Set();
                        const content = [];
                        const other = [];
                        const CS = 'main, article, [role="main"], .content, #content, .story, .article, .post, .entry, .headline, .news, .stories, .feed';
                        document.querySelectorAll('a[href]').forEach(link => {
                            const url = link.href;
                            let text = (link.textContent || '').trim().replace(/\\s+/g, ' ');
                            if (!url || !text || seen.has(url)) return;
                            if (url.startsWith('javascript:') || url === '#') return;
                            if (/<[^>]+>/.test(text)) text = text.replace(/<[^>]+>/g, '').trim();
                            if (text.length < 5 || text.length > 500) return;
                            seen.add(url);
                            const entry = {url, text};
                            (link.closest(CS) ? content : other).push(entry);
                        });
                        content.sort((a, b) => b.text.length - a.text.length);
                        other.sort((a, b) => b.text.length - a.text.length);
                        return [...content, ...other].slice(0, 200);
                    }
                """)
                result["links"] = links
            except Exception as e:  # fallback: links optional
                logger.debug("links_extract_failed", error=str(e))
                result["links"] = []
        
        # Extract images if requested
        if include_images:
            try:
                images = await page.evaluate("""
                    () => {
                        const imgElements = Array.from(document.querySelectorAll('img[src]'));
                        return imgElements.slice(0, 10).map(img => ({
                            url: img.src,
                            alt: img.alt || ''
                        }));
                    }
                """)
                result["images"] = images
            except Exception as e:  # fallback: images optional
                logger.debug("images_extract_failed", error=str(e))
                result["images"] = []
        
        # Extract iframe content if present
        try:
            iframes = await page.query_selector_all('iframe')
            if iframes:
                iframe_content = []
                for i, iframe in enumerate(iframes[:3]):  # Limit to 3 iframes
                    try:
                        src = await iframe.get_attribute('src')
                        if src and not src.startswith('data:'):
                            iframe_content.append({
                                "index": i,
                                "src": src,
                                "note": "iframe_detected"
                            })
                    except Exception as e:  # skip iframe if attribute access fails
                        logger.debug("iframe_src_failed", index=i, error=str(e))
                        continue
                
                if iframe_content:
                    result["iframes"] = iframe_content
        except Exception as e:  # optional: iframe enrichment
            logger.debug("iframes_extract_failed", error=str(e))
        
        # Detect if page uses JavaScript frameworks
        try:
            frameworks = await page.evaluate("""
                () => {
                    const detected = [];
                    if (window.React) detected.push('React');
                    if (window.Vue) detected.push('Vue');
                    if (window.angular) detected.push('Angular');
                    if (window.jQuery) detected.push('jQuery');
                    if (document.querySelector('[data-reactroot]')) detected.push('React (detected)');
                    if (document.querySelector('[data-v-]')) detected.push('Vue (detected)');
                    return detected;
                }
            """)
            if frameworks:
                result["detected_frameworks"] = frameworks
        except Exception as e:  # optional: framework detection enrichment
            logger.debug("frameworks_detect_failed", error=str(e))
        
        return result
        
    except Exception as e:
        return {
            "content_type": "error",
            "error": str(e),
            "main_content": ""
        }


def _extract_browser_content(
    page, 
    extract_strategy: str, 
    include_links: bool, 
    include_images: bool
) -> Dict[str, Any]:
    """Extract content from browser page."""
    
    if extract_strategy == "raw":
        html = page.content()
        return {"raw_html": html}
    
    # Get text content
    try:
        # Try to get main content areas
        main_content = ""
        
        # Try common content selectors
        content_selectors = [
            'main', 'article', '[role="main"]', '.content', '#content', 
            '.main-content', '.post-content', '.entry-content', '.article-content'
        ]
        
        for selector in content_selectors:
            try:
                element = page.query_selector(selector)
                if element:
                    main_content = element.inner_text()
                    break
            except Exception as e:  # fallback: try next selector
                logger.debug("selector_failed", selector=selector, error=str(e))
                continue
        
        # Fallback to body content
        if not main_content:
            try:
                body = page.query_selector('body')
                if body:
                    main_content = body.inner_text()
            except Exception as e:  # fallback: use empty if body fails
                logger.debug("body_extract_failed", error=str(e))
                main_content = ""
        
        result = {
            "content_type": "browser_extracted",
            **_bound_main_content(main_content),
        }
        
        # Extract meta information
        try:
            meta_description = page.get_attribute('meta[name="description"]', 'content')
            if meta_description:
                result["description"] = meta_description
        except Exception as e:  # optional: meta description enrichment
            logger.debug("meta_description_failed", error=str(e))
        
        # Extract links if requested
        if include_links:
            try:
                links = page.evaluate("""
                    () => {
                        const seen = new Set();
                        const content = [];
                        const other = [];
                        const CS = 'main, article, [role="main"], .content, #content, .story, .article, .post, .entry, .headline, .news, .stories, .feed';
                        document.querySelectorAll('a[href]').forEach(link => {
                            const url = link.href;
                            let text = (link.textContent || '').trim().replace(/\\s+/g, ' ');
                            if (!url || !text || seen.has(url)) return;
                            if (url.startsWith('javascript:') || url === '#') return;
                            if (/<[^>]+>/.test(text)) text = text.replace(/<[^>]+>/g, '').trim();
                            if (text.length < 5 || text.length > 500) return;
                            seen.add(url);
                            const entry = {url, text};
                            (link.closest(CS) ? content : other).push(entry);
                        });
                        content.sort((a, b) => b.text.length - a.text.length);
                        other.sort((a, b) => b.text.length - a.text.length);
                        return [...content, ...other].slice(0, 200);
                    }
                """)
                result["links"] = links
            except Exception as e:  # fallback: links optional
                logger.debug("links_extract_failed", error=str(e))
                result["links"] = []
        
        # Extract images if requested
        if include_images:
            try:
                images = page.evaluate("""
                    () => {
                        const imgElements = Array.from(document.querySelectorAll('img[src]'));
                        return imgElements.slice(0, 10).map(img => ({
                            url: img.src,
                            alt: img.alt || ''
                        }));
                    }
                """)
                result["images"] = images
            except Exception as e:  # fallback: images optional
                logger.debug("images_extract_failed", error=str(e))
                result["images"] = []
        
        # Extract iframe content if present
        try:
            iframes = page.query_selector_all('iframe')
            if iframes:
                iframe_content = []
                for i, iframe in enumerate(iframes[:3]):  # Limit to 3 iframes
                    try:
                        src = iframe.get_attribute('src')
                        if src and not src.startswith('data:'):
                            iframe_content.append({
                                "index": i,
                                "src": src,
                                "note": "iframe_detected"
                            })
                    except Exception as e:  # skip iframe if attribute access fails
                        logger.debug("iframe_src_failed", index=i, error=str(e))
                        continue
                
                if iframe_content:
                    result["iframes"] = iframe_content
        except Exception as e:  # optional: iframe enrichment
            logger.debug("iframes_extract_failed", error=str(e))
        
        # Detect if page uses JavaScript frameworks
        try:
            frameworks = page.evaluate("""
                () => {
                    const detected = [];
                    if (window.React) detected.push('React');
                    if (window.Vue) detected.push('Vue');
                    if (window.angular) detected.push('Angular');
                    if (window.jQuery) detected.push('jQuery');
                    if (document.querySelector('[data-reactroot]')) detected.push('React (detected)');
                    if (document.querySelector('[data-v-]')) detected.push('Vue (detected)');
                    return detected;
                }
            """)
            if frameworks:
                result["frameworks"] = frameworks
        except Exception as e:  # optional: framework detection enrichment
            logger.debug("frameworks_detect_failed", error=str(e))
        
        return result
        
    except Exception as e:
        return {
            "content_type": "browser_error",
            "error": str(e)
        }


def _parse_browser(ln: str, trig: str) -> Dict[str, Any]:
    """Parse browser tool parameters."""
    if trig in ("http:", "https:"):
        return {"url": ln}
    
    rest = ln[len(trig):].strip()
    params: Dict[str, Any] = {"url": rest}
    
    # Look for browser-specific hints
    if "screenshot" in ln.lower():
        params["screenshot"] = True
    if "wait" in ln.lower():
        params["wait_for"] = "body"  # Default wait
    if "js" in ln.lower() or "javascript" in ln.lower():
        params["extract_strategy"] = "javascript"
    
    return params


def _fmt_browser(res: Dict[str, Any]) -> str:
    """Format browser tool results."""
    status = res.get("status", "unknown")
    
    if "error" in res:
        return f"http_get_browser(status={status}, error)"
    
    frameworks = res.get("frameworks", [])
    framework_info = f", frameworks={','.join(frameworks)}" if frameworks else ""
    
    return f"http_get_browser(status={status}{framework_info})"


def register_browser(registry: ToolRegistry) -> None:
    """Register the browser-based HTTP GET tool."""
    # Import here to avoid circular dependencies
    try:
        from ..context_manager import ContextRequirement, ContextStrategy
        
        context_req = ContextRequirement(
            max_tokens=25000,           # Large context for browser content
            preferred_tokens=12000,     # Preferred size
            overflow_strategy=ContextStrategy.PRIORITIZE,
            priority_fields=["main_content", "title", "description", "status", "error", "javascript_result"],
            content_types=["text", "html", "javascript"]
        )
    except ImportError:
        context_req = None
    
    registry.register(
        name="core.http_get_browser",
        description="Fetch and read content from any website URL using a real browser. This is the RECOMMENDED tool for most web pages. Supports JavaScript-heavy sites, dynamic content, SPAs, bot-protected sites (Cloudflare, etc.), and complex web applications. Automatically handles compression (Brotli, gzip), executes JavaScript, and avoids bot detection. Use this for: modern websites, news sites, blogs, e-commerce, social media, or any site that may use bot protection. For simple REST/JSON APIs where speed matters and no JavaScript is required, prefer core.http_get instead.",
        func=run_browser,
        tool_schema=BrowserParams,
        triggers=["http_get_browser:", "browser:", "spa:", "js:", "http:", "https:"],
        priority=3,  # Higher priority (lower number) - preferred for web content
        estimate_tokens=lambda _: 300,
        parse_params=_parse_browser,
        observation_formatter=_fmt_browser,
        breaker_failure_threshold=2,  # Lower threshold due to resource intensity
        breaker_reset_timeout_seconds=60.0,
        max_retries=1,  # Lower retries due to cost
        retry_backoff_seconds=2.0,
        category="http",
        data_types=["spa", "javascript", "dynamic", "iframe", "browser", "screenshot"],
        keywords=["browser", "javascript", "spa", "react", "vue", "angular", "iframe", "dynamic", "screenshot", "read", "fetch", "get", "url", "website", "webpage", "news", "content"],
        context_requirement=context_req,
        default_timeout_seconds=60.0,  # Longer timeout for browser operations
        cost_class="high",  # Mark as expensive
    )


__all__ = ["register_browser", "run_browser"]
