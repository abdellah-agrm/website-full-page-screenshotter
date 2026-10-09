#!/usr/bin/env python3
"""
Website Full-Page Screenshotter Pro (Dark GUI & CLI)
==================================================
Captures high-resolution, full-page screenshots of single pages or entire websites.
Features a modern dark CustomTkinter GUI matching Screenshotter Pro design,
live log streaming, multi-threaded async crawling, real-time progress indicators,
and 6 live metric analytics cards.
"""

import sys
import os
import re
import time
import hashlib
import threading
import queue
import argparse
import asyncio
from urllib.parse import urljoin, urlparse, urldefrag
from collections import deque
import subprocess

import urllib.request
import urllib.parse
import urllib.error

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    _HAS_REQUESTS = False

try:
    from bs4 import BeautifulSoup
    _HAS_BS4 = True
except ImportError:
    _HAS_BS4 = False

try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False

try:
    import customtkinter as ctk
    _HAS_CTK = True
    BaseGUI = ctk.CTk
except ImportError:
    _HAS_CTK = False
    BaseGUI = object

if _HAS_CTK:
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")

HEADERS = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 SiteScreenshotPro/2.0'}
SKIP_EXT = re.compile(r'\.(pdf|jpg|jpeg|png|gif|svg|webp|zip|mp4|mp3|css|js|ico|xml|woff2?|ttf|eot)$', re.I)


def http_get(url, timeout=10):
    """Fetch URL content with requests if available or urllib fallback."""
    if _HAS_REQUESTS:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        return resp.status_code, resp.headers.get('Content-Type', ''), resp.text, resp.url
    else:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            ctype = resp.headers.get('Content-Type', '')
            charset = 'utf-8'
            if 'charset=' in ctype.lower():
                charset = ctype.lower().split('charset=')[-1].split(';')[0].strip()
            text = resp.read().decode(charset, errors='replace')
            return status, ctype, text, resp.geturl()


def extract_sitemap_locs(html_text):
    """Extract <loc> URLs from sitemap XML text."""
    if _HAS_BS4:
        try:
            soup = BeautifulSoup(html_text, 'html.parser')
            return [loc.text.strip() for loc in soup.find_all('loc')]
        except Exception:
            pass
    return [m.strip() for m in re.findall(r'<loc>(.*?)</loc>', html_text, re.I | re.S)]


def extract_html_hrefs(html_text):
    """Extract href links from HTML text."""
    if _HAS_BS4:
        try:
            soup = BeautifulSoup(html_text, 'html.parser')
            return [a['href'].strip() for a in soup.find_all('a', href=True)]
        except Exception:
            pass
    return [m.strip() for m in re.findall(r'<a\s+(?:[^>]*?\s+)?href=["\'](.*?)["\']', html_text, re.I)]


def get_base_domain(netloc):
    """Extract clean base domain name stripping www and port."""
    netloc = netloc.lower().split(':')[0]
    if netloc.startswith('www.'):
        netloc = netloc[4:]
    return netloc


def same_domain(url, root_netloc):
    """Check if target URL belongs to the root domain or subdomains."""
    root_base = get_base_domain(root_netloc)
    url_base = get_base_domain(urlparse(url).netloc)
    if not url_base or not root_base:
        return False
    return url_base == root_base or url_base.endswith('.' + root_base) or root_base.endswith('.' + url_base)


def normalize_url(url):
    """Normalize URL by stripping fragments, trailing slashes, and tracking query params."""
    if not url:
        return ""
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    
    if not parsed.scheme or not parsed.netloc:
        return url

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip('/') or '/'

    # Strip tracking query parameters
    query = parsed.query
    if query:
        filtered = [
            param for param in query.split('&') 
            if not param.startswith(('utm_', 'fbclid', 'gclid', 'ref=', 'source='))
        ]
        query = '&'.join(filtered)

    clean_url = f"{scheme}://{netloc}{path}"
    if query:
        clean_url += f"?{query}"
    return clean_url


def get_sitemap_urls(base_url, log_callback=None, cancel_check=None):
    """Extract URLs from sitemap.xml or sitemap_index.xml."""
    urls = set()
    sitemap_targets = [urljoin(base_url, '/sitemap.xml'), urljoin(base_url, '/sitemap_index.xml')]
    root_netloc = urlparse(base_url).netloc
    
    for sitemap_url in sitemap_targets:
        if cancel_check and cancel_check():
            break
        if log_callback:
            log_callback(f"Checking sitemap target: {sitemap_url}", "INFO")
        try:
            status, ctype, text, final_url = http_get(sitemap_url, timeout=10)
            if status != 200:
                continue
            
            locs = extract_sitemap_locs(text)
            nested = [l for l in locs if l.endswith('.xml')]
            
            for nested_url in nested:
                if cancel_check and cancel_check():
                    break
                try:
                    s2, c2, t2, f2 = http_get(nested_url, timeout=10)
                    for l in extract_sitemap_locs(t2):
                        norm = normalize_url(l)
                        if same_domain(norm, root_netloc):
                            urls.add(norm)
                except Exception as e:
                    if log_callback:
                        log_callback(f"Error fetching nested sitemap {nested_url}: {e}", "WARNING")
            
            for l in locs:
                if not l.endswith('.xml'):
                    norm = normalize_url(l)
                    if same_domain(norm, root_netloc):
                        urls.add(norm)
        except Exception as e:
            if log_callback:
                log_callback(f"Sitemap check failed for {sitemap_url}: {e}", "WARNING")
            continue
            
    return urls


def crawl_site(start_url, max_pages=50, delay=0.5, log_callback=None, cancel_check=None):
    """BFS crawl restricted to target domain."""
    root_netloc = urlparse(start_url).netloc
    start_norm = normalize_url(start_url)
    visited, discovered = set(), set()
    queue_urls = deque([start_norm])

    while queue_urls and len(discovered) < max_pages:
        if cancel_check and cancel_check():
            if log_callback:
                log_callback("Crawling cancelled by user.", "WARNING")
            break

        url = queue_urls.popleft()
        if url in visited:
            continue
        visited.add(url)

        if log_callback:
            log_callback(f"Crawling ({len(discovered) + 1}/{max_pages}): {url}", "INFO")

        try:
            status, ctype, text, final_url = http_get(url, timeout=10)
            if status != 200 or 'text/html' not in ctype.lower():
                continue
            
            norm_final = normalize_url(final_url)
            discovered.add(norm_final)

            hrefs = extract_html_hrefs(text)
            for href in hrefs:
                if not href or href.startswith(('mailto:', 'tel:', 'javascript:', '#')):
                    continue
                
                link = normalize_url(urljoin(norm_final, href))
                if SKIP_EXT.search(link):
                    continue

                if same_domain(link, root_netloc) and link not in visited and link not in queue_urls:
                    queue_urls.append(link)

        except Exception as e:
            if log_callback:
                log_callback(f"Request failed for {url}: {e}", "WARNING")
            continue

        time.sleep(delay)

    return discovered


async def crawl_site_playwright_spa(start_url, root_netloc, max_pages=50, log_callback=None):
    """Fall back Playwright crawler for Single Page Applications (React/Vue/Next.js)."""
    from playwright.async_api import async_playwright
    discovered = {normalize_url(start_url)}

    if log_callback:
        log_callback("Using Playwright DOM rendering to discover SPA links...", "INFO")

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                await page.goto(start_url, wait_until='commit', timeout=20000)
                try:
                    await page.wait_for_load_state('domcontentloaded', timeout=10000)
                except Exception:
                    pass
            except Exception:
                await page.goto(start_url, wait_until='domcontentloaded', timeout=20000)

            await page.wait_for_timeout(1500)

            hrefs = await page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)
            """)
            await browser.close()

            for href in hrefs:
                if len(discovered) >= max_pages:
                    break
                norm = normalize_url(href)
                if norm and same_domain(norm, root_netloc) and not SKIP_EXT.search(norm):
                    discovered.add(norm)
    except Exception as e:
        if log_callback:
            log_callback(f"Playwright SPA crawler error: {e}", "WARNING")

    return discovered


def url_to_slug(url):
    """Convert URL path to a clean page slug without random hashes."""
    parsed = urlparse(url)
    path = parsed.path.strip('/')
    
    if not path:
        return "homepage"
    
    slug = re.sub(r'[^a-zA-Z0-9_\-]', '-', path.replace('/', '-'))
    slug = re.sub(r'-+', '-', slug).strip('-')
    return slug[:120] or "page"


def get_unique_filepath(output_dir, base_slug, used_filenames=None):
    """
    Generate a unique filename in output_dir using base_slug.
    Appends -1, -2, -3 if file or filename already exists.
    """
    os.makedirs(output_dir, exist_ok=True)
    filename = f"{base_slug}.png"
    filepath = os.path.join(output_dir, filename)
    
    counter = 1
    while (used_filenames is not None and filename.lower() in used_filenames) or os.path.exists(filepath):
        filename = f"{base_slug}-{counter}.png"
        filepath = os.path.join(output_dir, filename)
        counter += 1

    if used_filenames is not None:
        used_filenames.add(filename.lower())

    return filename, filepath


def get_default_screenshots_dir():
    """
    Get the default folder to store screenshots inside the user's Windows Pictures folder.
    Ensures standalone executable saves to standard user media storage.
    """
    pictures_dir = None
    if sys.platform == "win32":
        user_profile = os.environ.get("USERPROFILE")
        if user_profile:
            candidate = os.path.join(user_profile, "Pictures")
            if os.path.exists(candidate):
                pictures_dir = candidate
        if not pictures_dir and user_profile:
            onedrive = os.environ.get("OneDrive")
            if onedrive:
                candidate = os.path.join(onedrive, "Pictures")
                if os.path.exists(candidate):
                    pictures_dir = candidate

    if not pictures_dir:
        from pathlib import Path
        home_pics = Path.home() / "Pictures"
        if home_pics.exists():
            pictures_dir = str(home_pics)
        else:
            pictures_dir = str(Path.home())

    return os.path.join(pictures_dir, "Screenshots")


async def auto_scroll_async(page, step=500, pause_ms=80, load_delay_sec=1.5, unhide_animations=False):
    """
    Scrolls down the page step-by-step to load lazy content & images.
    If unhide_animations is explicitly True, injects style overrides to reveal hidden animated elements.
    Explicitly protects navigation menus and dropdowns from opening.
    """
    try:
        if unhide_animations:
            await page.evaluate("""
                () => {
                    const style = document.createElement('style');
                    style.id = '__screenshot_unhide_style__';
                    style.textContent = `
                        *, *::before, *::after {
                            animation-duration: 0s !important;
                            animation-delay: 0s !important;
                            transition-duration: 0s !important;
                            transition-delay: 0s !important;
                        }
                        [data-aos]:not(nav):not(.menu):not(.dropdown):not([class*="menu"]):not([class*="nav"]) {
                            opacity: 1 !important;
                            transform: none !important;
                            visibility: visible !important;
                        }
                        .wow:not(nav):not(.menu), .animated:not(nav):not(.menu), .fade-in, .fade-up, .reveal, .lazyload {
                            opacity: 1 !important;
                            transform: none !important;
                            visibility: visible !important;
                        }
                    `;
                    document.head.appendChild(style);
                }
            """)

        await page.evaluate(f"""
            async () => {{
                await new Promise((resolve) => {{
                    let total = 0;
                    const distance = {step};
                    const timer = setInterval(() => {{
                        window.scrollBy(0, distance);
                        total += distance;
                        window.dispatchEvent(new Event('scroll'));
                        
                        document.querySelectorAll('img').forEach(img => {{
                            if (img.getAttribute('loading') === 'lazy') {{
                                img.setAttribute('loading', 'eager');
                            }}
                            if (img.dataset) {{
                                if (img.dataset.src && (!img.src || !img.src.includes(img.dataset.src))) img.src = img.dataset.src;
                                if (img.dataset.srcset && !img.srcset) img.srcset = img.dataset.srcset;
                                if (img.dataset.lazySrc && !img.src) img.src = img.dataset.lazySrc;
                                if (img.dataset.original && !img.src) img.src = img.dataset.original;
                            }}
                        }});

                        const scrollHeight = document.body ? document.body.scrollHeight : document.documentElement.scrollHeight;
                        if (total >= scrollHeight || total >= 35000) {{
                            clearInterval(timer);
                            resolve();
                        }}
                    }}, {pause_ms});
                }});
            }}
        """)

        if unhide_animations:
            await page.evaluate("""
                async () => {
                    const menuSelectors = 'nav, header nav, .menu, .nav-menu, .hamburger, .mobile-menu, .dropdown, [aria-expanded], [class*="menu"], [class*="dropdown"]';
                    
                    document.querySelectorAll('*').forEach(el => {
                        if (el.closest(menuSelectors)) return;

                        const style = el.style;
                        const computed = window.getComputedStyle(el);
                        
                        if (computed.opacity === '0' || style.opacity === '0') {
                            el.style.setProperty('opacity', '1', 'important');
                        }
                        if (computed.visibility === 'hidden' || style.visibility === 'hidden') {
                            el.style.setProperty('visibility', 'visible', 'important');
                        }

                        if (el.dataset) {
                            if (el.dataset.bg) el.style.backgroundImage = `url("${el.dataset.bg}")`;
                            if (el.dataset.backgroundImage) el.style.backgroundImage = `url("${el.dataset.backgroundImage}")`;
                        }
                    });
                }
            """)

        await page.evaluate("""
            async () => {
                const images = Array.from(document.images);
                await Promise.all(images.map(img => {
                    if (img.complete) return Promise.resolve();
                    return new Promise(resolve => {
                        img.onload = img.onerror = resolve;
                        setTimeout(resolve, 3500);
                    });
                }));

                if (document.fonts && document.fonts.ready) {
                    await document.fonts.ready;
                }
            }
        """)

        await page.evaluate("window.scrollTo(0, 0)")
        await page.wait_for_timeout(int(load_delay_sec * 1000))

    except Exception:
        pass


async def ensure_playwright_ready_async(log_callback=None):
    """Ensure Playwright Chromium browser is installed and ready."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        if log_callback:
            log_callback("Playwright Python package is not installed. Run: pip install playwright", "ERROR")
        return False

    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(headless=True)
            await b.close()
        return True
    except Exception as e:
        if log_callback:
            log_callback("Chromium binary missing. Installing Playwright Chromium...", "WARNING")
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", "chromium"
            )
            await proc.communicate()
            if proc.returncode == 0:
                if log_callback:
                    log_callback("Playwright Chromium installed successfully!", "SUCCESS")
                return True
            else:
                if log_callback:
                    log_callback("Failed to install Playwright Chromium browser binary.", "ERROR")
                return False
        except Exception as err:
            if log_callback:
                log_callback(f"Failed to install Playwright Chromium: {err}", "ERROR")
            return False


async def screenshot_worker_async(urls, output_dir, viewport_width=1440, wait_ms=1500, load_delay_sec=1.5, unhide_animations=False,
                                 progress_callback=None, log_callback=None, result_callback=None, cancel_check=None):
    """Execute screenshot captures using async Playwright."""
    os.makedirs(output_dir, exist_ok=True)
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        if log_callback:
            log_callback("Playwright package is missing. Run: pip install playwright", "ERROR")
        return []

    if log_callback:
        log_callback(f"Launching Playwright Chromium (Viewport: {viewport_width}px, Delay: {load_delay_sec}s, Unhide: {unhide_animations})...", "INFO")

    results = []
    total = len(urls)
    used_filenames = set()

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={'width': viewport_width, 'height': 900})

            for i, url in enumerate(sorted(urls), 1):
                if cancel_check and cancel_check():
                    if log_callback:
                        log_callback("Screenshot capture process cancelled by user.", "WARNING")
                    break

                if log_callback:
                    log_callback(f"[{i}/{total}] Navigating: {url}", "INFO")
                
                if progress_callback:
                    progress_callback(i, total, url)

                try:
                    # Fast & non-blocking navigation strategy
                    try:
                        await page.goto(url, wait_until='commit', timeout=25000)
                        try:
                            await page.wait_for_load_state('domcontentloaded', timeout=10000)
                        except Exception:
                            pass
                    except Exception:
                        await page.goto(url, wait_until='domcontentloaded', timeout=30000)

                    await page.wait_for_timeout(wait_ms)
                    await auto_scroll_async(page, load_delay_sec=load_delay_sec, unhide_animations=unhide_animations)

                    base_slug = url_to_slug(url)
                    filename, filepath = get_unique_filepath(output_dir, base_slug, used_filenames)
                    await page.screenshot(path=filepath, full_page=True)

                    results.append((url, filepath, 'ok'))
                    if log_callback:
                        log_callback(f"[{i}/{total}] Saved screenshot: {filename}", "SUCCESS")
                    if result_callback:
                        result_callback(url, filepath, 'ok')
                except Exception as e:
                    err_msg = str(e).split('\n')[0]
                    results.append((url, None, f'error: {err_msg}'))
                    if log_callback:
                        log_callback(f"[{i}/{total}] Capture failed for {url}: {err_msg}", "ERROR")
                    if result_callback:
                        result_callback(url, None, f'error: {err_msg}')

            await browser.close()
    except Exception as e:
        if log_callback:
            log_callback(f"Fatal Playwright engine error: {e}", "ERROR")

    return results


# ==============================================================================
# Windows 11 Fluent Design Tokens & Constants
# ==============================================================================
FLUENT_BG_BASE = "#18191D"        # Deep mica background
FLUENT_SIDEBAR_BG = "#212328"     # Navigation sidebar canvas
FLUENT_CARD_BG = "#26282E"        # Primary card surface
FLUENT_CARD_BORDER = "#353840"    # 1px border stroke for elevation
FLUENT_CARD_RECESSED = "#1A1B1F"  # Inset container / terminal surface
FLUENT_INPUT_BG = "#191A1E"       # Text input surface
FLUENT_INPUT_BORDER = "#3B3E47"   # Input outline
FLUENT_ACCENT = "#0078D4"         # Windows 11 Signature Blue
FLUENT_ACCENT_HOVER = "#1886D8"   # Hover state
FLUENT_ACCENT_LIGHT = "#60CDFF"   # Text highlight
FLUENT_TEXT_PRIMARY = "#FFFFFF"   # High contrast text
FLUENT_TEXT_SECONDARY = "#A0A5B1" # Secondary neutral text
FLUENT_TEXT_MUTED = "#6B7280"     # Disabled / subtle label text
FLUENT_SUCCESS = "#22C55E"        # Green success
FLUENT_SUCCESS_BG = "#143823"     # Subtle green glow
FLUENT_DANGER = "#EF4444"         # Red danger
FLUENT_DANGER_BG = "#3D1619"      # Subtle red glow
FLUENT_WARNING = "#F59E0B"        # Yellow warning
FLUENT_WARNING_BG = "#3B2810"     # Subtle amber glow


class WebsiteScreenshotterGUI(BaseGUI):
    """Windows 11 Fluent Design Desktop GUI for Website Screenshotter Pro."""

    def __init__(self):
        super().__init__()

        self.title("Screenshotter Pro - Fluent Studio")
        self.geometry("1280x820")
        self.minsize(1100, 720)
        self.configure(fg_color=FLUENT_BG_BASE)

        self._setup_app_icon()

        # Application State & Metrics
        self.is_running = False
        self.cancel_requested = False
        self.log_queue = queue.Queue()
        self.captured_items = []
        self.start_time = None

        self.metric_discovered = 0
        self.metric_saved = 0
        self.metric_success = 0
        self.metric_failed = 0
        self.metric_warnings = 0

        # Nav & Views Registry
        self.views = {}
        self.nav_buttons = {}

        # Grid setup: Column 0 = Sidebar, Column 1 = Main Canvas
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._create_sidebar()
        self._create_main_panel()

        # Start periodic log queue & timer polling
        self.after(100, self._process_log_queue)

    def _setup_app_icon(self):
        """Set application icon for window titlebar and taskbar."""
        script_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        imgs_ico = os.path.join(script_dir, "imgs", "website_screenshotter_icon.ico")
        root_ico = os.path.join(script_dir, "website_screenshotter_icon.ico")
        assets_ico = os.path.join(script_dir, "assets", "icon.ico")
        imgs_png = os.path.join(script_dir, "imgs", "website_screenshotter_icon.png")

        ico_path = None
        for path in [imgs_ico, root_ico, assets_ico]:
            if os.path.exists(path):
                ico_path = path
                break

        # Fallback: Auto-convert PNG to ICO if ICO does not exist yet
        if not ico_path and os.path.exists(imgs_png) and _HAS_PIL:
            try:
                img = Image.open(imgs_png)
                img.save(imgs_ico, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
                ico_path = imgs_ico
            except Exception:
                pass

        if ico_path:
            try:
                self.iconbitmap(ico_path)
            except Exception:
                try:
                    self.wm_iconbitmap(ico_path)
                except Exception:
                    pass

    # ==========================================================================
    # Fluent Sidebar Navigation
    # ==========================================================================
    def _create_sidebar(self):
        self.sidebar = ctk.CTkFrame(
            self,
            width=240,
            corner_radius=0,
            fg_color=FLUENT_SIDEBAR_BG,
            border_width=1,
            border_color=FLUENT_CARD_BORDER
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)
        self.sidebar.grid_rowconfigure(3, weight=1)

        # 1. Header & App Branding
        brand_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand_frame.grid(row=0, column=0, padx=16, pady=(18, 16), sticky="ew")

        logo_box = ctk.CTkFrame(
            brand_frame,
            width=42,
            height=42,
            corner_radius=10,
            fg_color=FLUENT_CARD_BG,
            border_width=1,
            border_color=FLUENT_CARD_BORDER
        )
        logo_box.grid(row=0, column=0, rowspan=2, padx=(0, 10), pady=0)
        logo_box.grid_propagate(False)

        script_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        png_path = os.path.join(script_dir, "assets", "icon.png")
        root_png = os.path.join(script_dir, "website_screenshotter_icon.png")
        imgs_png = os.path.join(script_dir, "imgs", "website_screenshotter_icon.png")
        icon_path = png_path if os.path.exists(png_path) else (root_png if os.path.exists(root_png) else (imgs_png if os.path.exists(imgs_png) else None))

        if icon_path and _HAS_PIL:
            try:
                icon_pil = Image.open(icon_path)
                ctk_icon = ctk.CTkImage(light_image=icon_pil, dark_image=icon_pil, size=(30, 30))
                logo_lbl = ctk.CTkLabel(logo_box, image=ctk_icon, text="")
            except Exception:
                logo_lbl = ctk.CTkLabel(logo_box, text="📸", font=ctk.CTkFont(size=20))
        else:
            logo_lbl = ctk.CTkLabel(logo_box, text="📸", font=ctk.CTkFont(size=20))
        logo_lbl.place(relx=0.5, rely=0.5, anchor="center")

        title_lbl = ctk.CTkLabel(
            brand_frame,
            text="Screenshotter Pro",
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            text_color=FLUENT_TEXT_PRIMARY
        )
        title_lbl.grid(row=0, column=1, sticky="w")

        sub_lbl = ctk.CTkLabel(
            brand_frame,
            text="Fluent Studio Edition",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=FLUENT_TEXT_SECONDARY
        )
        sub_lbl.grid(row=1, column=1, sticky="w")

        # 2. Navigation Category Header
        nav_header = ctk.CTkLabel(
            self.sidebar,
            text="NAVIGATION",
            font=ctk.CTkFont(family="Segoe UI", size=9, weight="bold"),
            text_color=FLUENT_TEXT_MUTED
        )
        nav_header.grid(row=1, column=0, padx=20, pady=(4, 6), sticky="w")

        # 3. Nav Buttons Container
        nav_container = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        nav_container.grid(row=2, column=0, padx=10, pady=0, sticky="ew")
        nav_container.grid_columnconfigure(0, weight=1)

        self._add_nav_item(nav_container, 0, "dashboard", "🏠  Capture Studio", is_default=True)
        self._add_nav_item(nav_container, 1, "gallery", "🖼️  Screenshot Gallery")
        self._add_nav_item(nav_container, 2, "console", "📋  Activity Logs")
        self._add_nav_item(nav_container, 3, "settings", "⚙️  Studio Settings")

        # 4. Bottom System Status & Quick Actions
        bottom_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        bottom_frame.grid(row=4, column=0, padx=14, pady=(10, 16), sticky="ew")
        bottom_frame.grid_columnconfigure(0, weight=1)

        # Engine Status Pill
        engine_card = ctk.CTkFrame(
            bottom_frame,
            fg_color=FLUENT_CARD_BG,
            corner_radius=8,
            border_width=1,
            border_color=FLUENT_CARD_BORDER
        )
        engine_card.grid(row=0, column=0, pady=(0, 10), sticky="ew")
        engine_card.grid_columnconfigure(1, weight=1)

        dot = ctk.CTkLabel(
            engine_card,
            text="●",
            font=ctk.CTkFont(size=13),
            text_color=FLUENT_SUCCESS
        )
        dot.grid(row=0, column=0, padx=(10, 4), pady=8)

        engine_lbl = ctk.CTkLabel(
            engine_card,
            text="Playwright Engine",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=FLUENT_TEXT_PRIMARY
        )
        engine_lbl.grid(row=0, column=1, sticky="w")

        engine_sub = ctk.CTkLabel(
            engine_card,
            text="Ready",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=FLUENT_SUCCESS
        )
        engine_sub.grid(row=0, column=2, padx=(0, 10), sticky="e")

        # Open Folder Quick Button
        folder_quick_btn = ctk.CTkButton(
            bottom_frame,
            text="📂  Open Output Folder",
            height=34,
            corner_radius=6,
            fg_color=FLUENT_CARD_BG,
            hover_color=FLUENT_CARD_BORDER,
            text_color=FLUENT_TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._open_output_folder
        )
        folder_quick_btn.grid(row=1, column=0, pady=(0, 10), sticky="ew")

        # Version Tag
        ver_lbl = ctk.CTkLabel(
            bottom_frame,
            text="v2.1.0 • Windows 11 Fluent",
            font=ctk.CTkFont(family="Segoe UI", size=10),
            text_color=FLUENT_TEXT_MUTED
        )
        ver_lbl.grid(row=2, column=0)

    def _add_nav_item(self, parent, row_idx, key, label_text, is_default=False):
        """Creates a modern Windows 11 navigation pill with left active bar."""
        item_frame = ctk.CTkFrame(parent, fg_color="transparent", corner_radius=6)
        item_frame.grid(row=row_idx, column=0, pady=3, sticky="ew")
        item_frame.grid_columnconfigure(1, weight=1)

        # 3px blue vertical pill indicator
        indicator = ctk.CTkFrame(
            item_frame,
            width=3,
            height=20,
            corner_radius=2,
            fg_color=FLUENT_ACCENT if is_default else "transparent"
        )
        indicator.grid(row=0, column=0, padx=(2, 6), pady=6)
        indicator.grid_propagate(False)

        btn = ctk.CTkButton(
            item_frame,
            text=label_text,
            anchor="w",
            height=34,
            corner_radius=6,
            fg_color=FLUENT_CARD_BG if is_default else "transparent",
            hover_color="#2D3037",
            text_color=FLUENT_TEXT_PRIMARY if is_default else FLUENT_TEXT_SECONDARY,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold" if is_default else "normal"),
            command=lambda k=key: self._switch_nav(k)
        )
        btn.grid(row=0, column=1, sticky="ew", padx=(0, 4))

        self.nav_buttons[key] = {
            "frame": item_frame,
            "indicator": indicator,
            "button": btn
        }

    def _switch_nav(self, target_key):
        """Switch active view and update nav button indicator."""
        for key, frame in self.views.items():
            frame.pack_forget()

        if target_key in self.views:
            self.views[target_key].pack(fill="both", expand=True)

        for key, data in self.nav_buttons.items():
            active = (key == target_key)
            data["indicator"].configure(fg_color=FLUENT_ACCENT if active else "transparent")
            data["button"].configure(
                fg_color=FLUENT_CARD_BG if active else "transparent",
                text_color=FLUENT_TEXT_PRIMARY if active else FLUENT_TEXT_SECONDARY,
                font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold" if active else "normal")
            )

    # ==========================================================================
    # Main Canvas & Views
    # ==========================================================================
    def _create_main_panel(self):
        self.main_container = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self.main_container.grid(row=0, column=1, sticky="nsew", padx=16, pady=16)
        self.main_container.grid_columnconfigure(0, weight=1)
        self.main_container.grid_rowconfigure(2, weight=1)

        # 1. Prominent Status Hero Card (Full Width at Top)
        self._create_hero_card()

        # 2. Sleek Progress Bar
        self._create_progress_bar()

        # 3. View Switcher Area
        self.view_host = ctk.CTkFrame(self.main_container, fg_color="transparent")
        self.view_host.grid(row=2, column=0, sticky="nsew", pady=(8, 0))

        # Build Views
        self._build_dashboard_view()
        self._build_gallery_view()
        self._build_console_view()
        self._build_settings_view()

        # Set default view
        self._switch_nav("dashboard")

    def _create_hero_card(self):
        """Full-width Windows 11 Acrylic Hero Status Card."""
        self.hero_card = ctk.CTkFrame(
            self.main_container,
            fg_color=FLUENT_CARD_BG,
            corner_radius=12,
            border_width=1,
            border_color=FLUENT_CARD_BORDER
        )
        self.hero_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.hero_card.grid_columnconfigure(1, weight=1)

        # Glowing Circular Status Orb (56x56)
        self.status_icon_box = ctk.CTkFrame(
            self.hero_card,
            width=54,
            height=54,
            corner_radius=27,
            fg_color=FLUENT_SUCCESS_BG,
            border_width=1,
            border_color=FLUENT_SUCCESS
        )
        self.status_icon_box.grid(row=0, column=0, rowspan=2, padx=(18, 14), pady=14)
        self.status_icon_box.grid_propagate(False)

        self.status_icon_lbl = ctk.CTkLabel(
            self.status_icon_box,
            text="✓",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=FLUENT_SUCCESS
        )
        self.status_icon_lbl.place(relx=0.5, rely=0.5, anchor="center")

        # Titles
        self.status_title = ctk.CTkLabel(
            self.hero_card,
            text="Ready to Capture",
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color=FLUENT_TEXT_PRIMARY
        )
        self.status_title.grid(row=0, column=1, sticky="w", pady=(14, 2))

        self.status_sub = ctk.CTkLabel(
            self.hero_card,
            text="Configure website parameters below and click 'Start Capture' to begin.",
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=FLUENT_TEXT_SECONDARY
        )
        self.status_sub.grid(row=1, column=1, sticky="w", pady=(0, 14))

        # Hero Action Controls (Right Side)
        actions_frame = ctk.CTkFrame(self.hero_card, fg_color="transparent")
        actions_frame.grid(row=0, column=2, rowspan=2, padx=16, pady=12, sticky="e")

        self.start_btn = ctk.CTkButton(
            actions_frame,
            text="🚀  Start Capture",
            height=40,
            corner_radius=8,
            fg_color=FLUENT_ACCENT,
            hover_color=FLUENT_ACCENT_HOVER,
            text_color=FLUENT_TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            command=self._start_process
        )
        self.start_btn.grid(row=0, column=0, padx=(0, 8))

        self.stop_btn = ctk.CTkButton(
            actions_frame,
            text="⛔  Stop",
            height=40,
            width=80,
            corner_radius=8,
            fg_color=FLUENT_DANGER,
            hover_color="#DC2626",
            text_color=FLUENT_TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            state="disabled",
            command=self._stop_process
        )
        self.stop_btn.grid(row=0, column=1, padx=(0, 8))

        hero_folder_btn = ctk.CTkButton(
            actions_frame,
            text="📂",
            width=40,
            height=40,
            corner_radius=8,
            fg_color="#333742",
            hover_color=FLUENT_CARD_BORDER,
            text_color=FLUENT_TEXT_PRIMARY,
            font=ctk.CTkFont(size=14),
            command=self._open_output_folder
        )
        hero_folder_btn.grid(row=0, column=2)

    def _create_progress_bar(self):
        """Sleek 6px Fluent Progress Bar."""
        prog_frame = ctk.CTkFrame(self.main_container, fg_color="transparent")
        prog_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        prog_frame.columnconfigure(0, weight=1)

        self.progress_bar = ctk.CTkProgressBar(
            prog_frame,
            height=6,
            corner_radius=3,
            progress_color=FLUENT_ACCENT,
            fg_color=FLUENT_CARD_RECESSED
        )
        self.progress_bar.set(0.0)
        self.progress_bar.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.progress_pct_lbl = ctk.CTkLabel(
            prog_frame,
            text="0%",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=FLUENT_ACCENT_LIGHT
        )
        self.progress_pct_lbl.grid(row=0, column=1, sticky="e")

    # ==========================================================================
    # View 1: Dashboard View (Main Studio)
    # ==========================================================================
    def _build_dashboard_view(self):
        view = ctk.CTkFrame(self.view_host, fg_color="transparent")
        view.grid_columnconfigure(0, weight=1)
        view.grid_rowconfigure(2, weight=1)

        # 1. Configuration Panel Card
        config_card = ctk.CTkFrame(
            view,
            fg_color=FLUENT_CARD_BG,
            corner_radius=12,
            border_width=1,
            border_color=FLUENT_CARD_BORDER
        )
        config_card.grid(row=0, column=0, sticky="ew", pady=(0, 10), padx=0)
        config_card.grid_columnconfigure((0, 1, 2), weight=1)

        # Row 0: Target URL Input with Paste Quick-Action
        url_header = ctk.CTkFrame(config_card, fg_color="transparent")
        url_header.grid(row=0, column=0, columnspan=3, padx=16, pady=(12, 4), sticky="ew")
        url_header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            url_header,
            text="🌐  Target Website URL / Domain",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=FLUENT_TEXT_PRIMARY
        ).grid(row=0, column=0, sticky="w")

        paste_btn = ctk.CTkButton(
            url_header,
            text="📋 Paste",
            width=65,
            height=24,
            corner_radius=4,
            fg_color="#333742",
            hover_color=FLUENT_CARD_BORDER,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._paste_url_from_clipboard
        )
        paste_btn.grid(row=0, column=1, sticky="e")

        self.url_entry = ctk.CTkEntry(
            config_card,
            placeholder_text="e.g. sharedsystem.com/fr  or  https://stripe.com",
            height=38,
            corner_radius=8,
            fg_color=FLUENT_INPUT_BG,
            border_color=FLUENT_INPUT_BORDER,
            text_color=FLUENT_TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=12)
        )
        self.url_entry.grid(row=1, column=0, columnspan=3, padx=16, pady=(0, 12), sticky="ew")

        # Row 2: 3-Column Settings (Mode, Viewport, Output)
        # Col 0: Mode
        col0 = ctk.CTkFrame(config_card, fg_color="transparent")
        col0.grid(row=2, column=0, padx=(16, 8), pady=(0, 8), sticky="nsew")
        col0.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            col0,
            text="Discovery Mode",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=FLUENT_TEXT_SECONDARY
        ).grid(row=0, column=0, sticky="w", pady=(0, 3))

        self.mode_var = ctk.StringVar(value="BFS Crawl")
        self.mode_seg = ctk.CTkSegmentedButton(
            col0,
            values=["BFS Crawl", "Sitemap XML", "Single Page"],
            variable=self.mode_var,
            selected_color=FLUENT_ACCENT,
            selected_hover_color=FLUENT_ACCENT_HOVER,
            unselected_color=FLUENT_INPUT_BG,
            unselected_hover_color="#2A2C33",
            text_color=FLUENT_TEXT_PRIMARY,
            corner_radius=6,
            height=32
        )
        self.mode_seg.grid(row=1, column=0, sticky="ew")

        # Col 1: Viewport
        col1 = ctk.CTkFrame(config_card, fg_color="transparent")
        col1.grid(row=2, column=1, padx=8, pady=(0, 8), sticky="nsew")
        col1.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            col1,
            text="Viewport Resolution",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=FLUENT_TEXT_SECONDARY
        ).grid(row=0, column=0, sticky="w", pady=(0, 3))

        self.viewport_var = ctk.StringVar(value="Full HD (1920px)")
        self.viewport_combo = ctk.CTkOptionMenu(
            col1,
            values=["Desktop 4K (3840px)", "Full HD (1920px)", "Desktop (1440px)", "Tablet (768px)", "Mobile (375px)"],
            variable=self.viewport_var,
            height=32,
            corner_radius=6,
            fg_color=FLUENT_INPUT_BG,
            button_color="#2A2C33",
            button_hover_color=FLUENT_CARD_BORDER,
            text_color=FLUENT_TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=11)
        )
        self.viewport_combo.grid(row=1, column=0, sticky="ew")

        # Col 2: Output Folder
        col2 = ctk.CTkFrame(config_card, fg_color="transparent")
        col2.grid(row=2, column=2, padx=(8, 16), pady=(0, 8), sticky="nsew")
        col2.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            col2,
            text="Output Directory",
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            text_color=FLUENT_TEXT_SECONDARY
        ).grid(row=0, column=0, sticky="w", pady=(0, 3))

        out_box = ctk.CTkFrame(col2, fg_color="transparent")
        out_box.grid(row=1, column=0, sticky="ew")
        out_box.grid_columnconfigure(0, weight=1)

        self.output_entry = ctk.CTkEntry(
            out_box,
            height=32,
            corner_radius=6,
            fg_color=FLUENT_INPUT_BG,
            border_color=FLUENT_INPUT_BORDER,
            text_color=FLUENT_TEXT_PRIMARY,
            font=ctk.CTkFont(family="Segoe UI", size=11)
        )
        default_dir = get_default_screenshots_dir()
        self.output_entry.insert(0, default_dir)
        self.output_entry.grid(row=0, column=0, padx=(0, 4), sticky="ew")

        browse_btn = ctk.CTkButton(
            out_box,
            text="📁",
            width=32,
            height=32,
            corner_radius=6,
            fg_color="#333742",
            hover_color=FLUENT_CARD_BORDER,
            command=self._browse_folder
        )
        browse_btn.grid(row=0, column=1)

        # Row 3: Sliders and Checkbox
        row3 = ctk.CTkFrame(config_card, fg_color="transparent")
        row3.grid(row=3, column=0, columnspan=3, padx=16, pady=(4, 14), sticky="ew")
        row3.grid_columnconfigure((0, 1), weight=1)

        # Max Pages Slider
        slider_card1 = ctk.CTkFrame(row3, fg_color=FLUENT_CARD_RECESSED, corner_radius=8, border_width=1, border_color=FLUENT_CARD_BORDER)
        slider_card1.grid(row=0, column=0, padx=(0, 8), sticky="ew")
        slider_card1.grid_columnconfigure(0, weight=1)

        s1_head = ctk.CTkFrame(slider_card1, fg_color="transparent")
        s1_head.grid(row=0, column=0, padx=10, pady=(6, 2), sticky="ew")
        s1_head.columnconfigure(1, weight=1)
        ctk.CTkLabel(s1_head, text="Max Crawl Pages", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=FLUENT_TEXT_PRIMARY).grid(row=0, column=0, sticky="w")
        self.max_pages_val_label = ctk.CTkLabel(s1_head, text="11 pages", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=FLUENT_ACCENT_LIGHT)
        self.max_pages_val_label.grid(row=0, column=1, sticky="e")

        self.max_pages_slider = ctk.CTkSlider(
            slider_card1,
            from_=1, to=150, number_of_steps=149,
            button_color=FLUENT_ACCENT, button_hover_color=FLUENT_ACCENT_HOVER,
            progress_color=FLUENT_ACCENT, fg_color="#2A2C33",
            command=self._on_pages_slider_change
        )
        self.max_pages_slider.set(11)
        self.max_pages_slider.grid(row=1, column=0, padx=10, pady=(0, 8), sticky="ew")

        # Load Delay Slider
        slider_card2 = ctk.CTkFrame(row3, fg_color=FLUENT_CARD_RECESSED, corner_radius=8, border_width=1, border_color=FLUENT_CARD_BORDER)
        slider_card2.grid(row=0, column=1, padx=(8, 8), sticky="ew")
        slider_card2.grid_columnconfigure(0, weight=1)

        s2_head = ctk.CTkFrame(slider_card2, fg_color="transparent")
        s2_head.grid(row=0, column=0, padx=10, pady=(6, 2), sticky="ew")
        s2_head.columnconfigure(1, weight=1)
        ctk.CTkLabel(s2_head, text="Load Delay (sec)", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=FLUENT_TEXT_PRIMARY).grid(row=0, column=0, sticky="w")
        self.delay_val_label = ctk.CTkLabel(s2_head, text="4.0s", font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"), text_color=FLUENT_ACCENT_LIGHT)
        self.delay_val_label.grid(row=0, column=1, sticky="e")

        self.delay_slider = ctk.CTkSlider(
            slider_card2,
            from_=0.5, to=5.0, number_of_steps=45,
            button_color=FLUENT_ACCENT, button_hover_color=FLUENT_ACCENT_HOVER,
            progress_color=FLUENT_ACCENT, fg_color="#2A2C33",
            command=self._on_delay_slider_change
        )
        self.delay_slider.set(4.0)
        self.delay_slider.grid(row=1, column=0, padx=10, pady=(0, 8), sticky="ew")

        # Checkbox
        self.unhide_var = ctk.BooleanVar(value=False)
        self.unhide_cb = ctk.CTkCheckBox(
            row3,
            text="Force Un-hide Animations",
            variable=self.unhide_var,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            checkbox_width=18, checkbox_height=18,
            corner_radius=4,
            border_width=2,
            fg_color=FLUENT_ACCENT, hover_color=FLUENT_ACCENT_HOVER,
            text_color=FLUENT_TEXT_SECONDARY
        )
        self.unhide_cb.grid(row=0, column=2, padx=(8, 0), sticky="w")

        # 2. 6 Elevated Analytics Metric Cards Row
        self._build_metric_cards_row(view)

        # 3. Bottom Tabs (Activity Log & Live Gallery)
        self._build_bottom_tabs(view)

        self.views["dashboard"] = view

    def _build_metric_cards_row(self, parent):
        cards_frame = ctk.CTkFrame(parent, fg_color="transparent")
        cards_frame.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        cards_frame.grid_columnconfigure((0, 1, 2, 3, 4, 5), weight=1)

        def make_fluent_metric(col, icon, orb_bg, icon_fg, init_val, title):
            card = ctk.CTkFrame(
                cards_frame,
                fg_color=FLUENT_CARD_BG,
                corner_radius=10,
                border_width=1,
                border_color=FLUENT_CARD_BORDER
            )
            card.grid(row=0, column=col, padx=(0 if col == 0 else 4, 0 if col == 5 else 4), sticky="nsew")
            card.grid_columnconfigure(1, weight=1)

            orb = ctk.CTkFrame(card, width=36, height=36, corner_radius=18, fg_color=orb_bg)
            orb.grid(row=0, column=0, rowspan=2, padx=(10, 8), pady=8)
            orb.grid_propagate(False)

            ilbl = ctk.CTkLabel(orb, text=icon, font=ctk.CTkFont(size=14, weight="bold"), text_color=icon_fg)
            ilbl.place(relx=0.5, rely=0.5, anchor="center")

            vlbl = ctk.CTkLabel(
                card,
                text=init_val,
                font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
                text_color=FLUENT_TEXT_PRIMARY
            )
            vlbl.grid(row=0, column=1, padx=(0, 6), pady=(8, 0), sticky="w")

            tlbl = ctk.CTkLabel(
                card,
                text=title,
                font=ctk.CTkFont(family="Segoe UI", size=10),
                text_color=FLUENT_TEXT_SECONDARY
            )
            tlbl.grid(row=1, column=1, padx=(0, 6), pady=(0, 8), sticky="w")

            return vlbl

        self.lbl_discovered = make_fluent_metric(0, "🌐", "#172A3A", "#38BDF8", "0", "Discovered")
        self.lbl_saved = make_fluent_metric(1, "📷", "#291838", "#C084FC", "0", "Saved")
        self.lbl_success = make_fluent_metric(2, "✓", "#123321", "#4ADE80", "0", "Success")
        self.lbl_failed = make_fluent_metric(3, "✕", "#3A1719", "#F87171", "0", "Failed")
        self.lbl_warnings = make_fluent_metric(4, "⚠️", "#382912", "#FBBF24", "0", "Warnings")
        self.lbl_duration = make_fluent_metric(5, "⏱️", "#133136", "#2DD4BF", "00:00", "Duration")
        self.saved_badge_lbl = self.lbl_saved

    def _build_bottom_tabs(self, parent):
        self.tabview = ctk.CTkTabview(
            parent,
            fg_color=FLUENT_CARD_BG,
            corner_radius=12,
            border_width=1,
            border_color=FLUENT_CARD_BORDER,
            segmented_button_selected_color=FLUENT_ACCENT,
            segmented_button_selected_hover_color=FLUENT_ACCENT_HOVER,
            segmented_button_unselected_color=FLUENT_CARD_RECESSED,
            segmented_button_unselected_hover_color="#2A2C33"
        )
        self.tabview.grid(row=2, column=0, sticky="nsew")

        self.tab_logs = self.tabview.add(">_  Activity Console Log")
        self.tab_gallery = self.tabview.add("🖼️  Live Gallery Preview")

        self._setup_logs_tab()
        self._setup_gallery_tab()

    def _setup_logs_tab(self):
        self.tab_logs.grid_columnconfigure(0, weight=1)
        self.tab_logs.grid_rowconfigure(0, weight=1)

        self.log_textbox = ctk.CTkTextbox(
            self.tab_logs,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=FLUENT_CARD_RECESSED,
            text_color="#D8DEE9",
            wrap="none",
            corner_radius=8
        )
        self.log_textbox.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self._append_log("System initialized. Fluent Studio ready for domain captures.", "INFO")

    def _setup_gallery_tab(self):
        self.tab_gallery.grid_columnconfigure(0, weight=1)
        self.tab_gallery.grid_rowconfigure(0, weight=1)

        self.gallery_scroll = ctk.CTkScrollableFrame(
            self.tab_gallery,
            fg_color=FLUENT_CARD_RECESSED,
            corner_radius=8
        )
        self.gallery_scroll.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.gallery_scroll.grid_columnconfigure((0, 1, 2), weight=1)

        self.empty_gallery_lbl = ctk.CTkLabel(
            self.gallery_scroll,
            text="No screenshots captured yet.\nCaptured images will appear here in real-time.",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=FLUENT_TEXT_SECONDARY
        )
        self.empty_gallery_lbl.grid(row=0, column=0, columnspan=3, pady=50)

    # ==========================================================================
    # View 2: Full Gallery View
    # ==========================================================================
    def _build_gallery_view(self):
        view = ctk.CTkFrame(self.view_host, fg_color="transparent")
        view.grid_columnconfigure(0, weight=1)
        view.grid_rowconfigure(1, weight=1)

        # Gallery Top Header Card
        top_bar = ctk.CTkFrame(
            view,
            fg_color=FLUENT_CARD_BG,
            corner_radius=10,
            border_width=1,
            border_color=FLUENT_CARD_BORDER
        )
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        top_bar.grid_columnconfigure(0, weight=1)

        title_box = ctk.CTkFrame(top_bar, fg_color="transparent")
        title_box.grid(row=0, column=0, padx=16, pady=10, sticky="w")

        ctk.CTkLabel(
            title_box,
            text="🖼️  Screenshot Gallery Archive",
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            text_color=FLUENT_TEXT_PRIMARY
        ).grid(row=0, column=0, sticky="w")

        self.full_gallery_sub = ctk.CTkLabel(
            title_box,
            text="Real-time captured high-resolution full-page screenshots.",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=FLUENT_TEXT_SECONDARY
        )
        self.full_gallery_sub.grid(row=1, column=0, sticky="w")

        btn_box = ctk.CTkFrame(top_bar, fg_color="transparent")
        btn_box.grid(row=0, column=1, padx=16, pady=10, sticky="e")

        open_dir_btn = ctk.CTkButton(
            btn_box,
            text="📂 Open Output Folder",
            height=32,
            corner_radius=6,
            fg_color=FLUENT_ACCENT,
            hover_color=FLUENT_ACCENT_HOVER,
            font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
            command=self._open_output_folder
        )
        open_dir_btn.grid(row=0, column=0, padx=(0, 8))

        # Full Gallery Scroll
        self.full_gallery_scroll = ctk.CTkScrollableFrame(
            view,
            fg_color=FLUENT_CARD_BG,
            corner_radius=10,
            border_width=1,
            border_color=FLUENT_CARD_BORDER
        )
        self.full_gallery_scroll.grid(row=1, column=0, sticky="nsew")
        self.full_gallery_scroll.grid_columnconfigure((0, 1, 2), weight=1)

        self.full_empty_lbl = ctk.CTkLabel(
            self.full_gallery_scroll,
            text="No screenshots captured yet.\nStart a capture session from Capture Studio to populate the gallery.",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=FLUENT_TEXT_SECONDARY
        )
        self.full_empty_lbl.grid(row=0, column=0, columnspan=3, pady=100)

        self.views["gallery"] = view

    # ==========================================================================
    # View 3: Full Console & Activity Logs View
    # ==========================================================================
    def _build_console_view(self):
        view = ctk.CTkFrame(self.view_host, fg_color="transparent")
        view.grid_columnconfigure(0, weight=1)
        view.grid_rowconfigure(1, weight=1)

        top_bar = ctk.CTkFrame(
            view,
            fg_color=FLUENT_CARD_BG,
            corner_radius=10,
            border_width=1,
            border_color=FLUENT_CARD_BORDER
        )
        top_bar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        top_bar.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            top_bar,
            text="📋  Full System Activity Console",
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            text_color=FLUENT_TEXT_PRIMARY
        ).grid(row=0, column=0, padx=16, pady=12, sticky="w")

        clear_btn = ctk.CTkButton(
            top_bar,
            text="🗑️ Clear Logs",
            width=100,
            height=30,
            corner_radius=6,
            fg_color="#333742",
            hover_color=FLUENT_CARD_BORDER,
            font=ctk.CTkFont(family="Segoe UI", size=11),
            command=self._clear_logs
        )
        clear_btn.grid(row=0, column=1, padx=16, pady=12, sticky="e")

        self.full_log_textbox = ctk.CTkTextbox(
            view,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color=FLUENT_CARD_RECESSED,
            text_color="#D8DEE9",
            wrap="none",
            corner_radius=10,
            border_width=1,
            border_color=FLUENT_CARD_BORDER
        )
        self.full_log_textbox.grid(row=1, column=0, sticky="nsew")

        self.views["console"] = view

    # ==========================================================================
    # View 4: Presets & Settings View
    # ==========================================================================
    def _build_settings_view(self):
        view = ctk.CTkFrame(self.view_host, fg_color="transparent")
        view.grid_columnconfigure(0, weight=1)

        card = ctk.CTkFrame(
            view,
            fg_color=FLUENT_CARD_BG,
            corner_radius=12,
            border_width=1,
            border_color=FLUENT_CARD_BORDER
        )
        card.grid(row=0, column=0, sticky="ew", pady=(0, 12), padx=0)
        card.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(
            card,
            text="⚙️  Capture Studio Preferences",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=FLUENT_TEXT_PRIMARY
        ).grid(row=0, column=0, columnspan=2, padx=20, pady=(16, 12), sticky="w")

        # Row 1: Quick Target Presets
        ctk.CTkLabel(
            card,
            text="Quick Presets:",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=FLUENT_TEXT_SECONDARY
        ).grid(row=1, column=0, padx=20, pady=8, sticky="w")

        preset_box = ctk.CTkFrame(card, fg_color="transparent")
        preset_box.grid(row=1, column=1, padx=20, pady=8, sticky="w")

        for preset_url in ["stripe.com", "github.com", "apple.com", "news.ycombinator.com"]:
            btn = ctk.CTkButton(
                preset_box,
                text=preset_url,
                width=80,
                height=28,
                corner_radius=6,
                fg_color="#2D3037",
                hover_color=FLUENT_ACCENT,
                font=ctk.CTkFont(family="Segoe UI", size=11),
                command=lambda u=preset_url: self._apply_url_preset(u)
            )
            btn.pack(side="left", padx=4)

        # Row 2: User Agent Description
        ctk.CTkLabel(
            card,
            text="Browser Engine:",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=FLUENT_TEXT_SECONDARY
        ).grid(row=2, column=0, padx=20, pady=8, sticky="w")

        ctk.CTkLabel(
            card,
            text="Playwright Headless Chromium (Blink) with DOM Content Load detection",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=FLUENT_TEXT_PRIMARY
        ).grid(row=2, column=1, padx=20, pady=8, sticky="w")

        # Row 3: Full Page Lazy Scroll
        ctk.CTkLabel(
            card,
            text="Scroll Engine:",
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold"),
            text_color=FLUENT_TEXT_SECONDARY
        ).grid(row=3, column=0, padx=20, pady=(8, 20), sticky="w")

        ctk.CTkLabel(
            card,
            text="Smooth step-by-step viewport trigger with dynamic image & font ready checks",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=FLUENT_TEXT_PRIMARY
        ).grid(row=3, column=1, padx=20, pady=(8, 20), sticky="w")

        self.views["settings"] = view

    # ==========================================================================
    # Helper & Event Handlers
    # ==========================================================================
    def _apply_url_preset(self, url):
        self.url_entry.delete(0, "end")
        self.url_entry.insert(0, url)
        self._switch_nav("dashboard")

    def _paste_url_from_clipboard(self):
        try:
            clipboard_text = self.clipboard_get().strip()
            if clipboard_text:
                self.url_entry.delete(0, "end")
                self.url_entry.insert(0, clipboard_text)
        except Exception:
            pass

    def _clear_logs(self):
        self.log_textbox.delete("1.0", "end")
        if hasattr(self, "full_log_textbox"):
            self.full_log_textbox.delete("1.0", "end")

    def _on_pages_slider_change(self, value):
        self.max_pages_val_label.configure(text=f"{int(value)} pages")

    def _on_delay_slider_change(self, value):
        self.delay_val_label.configure(text=f"{value:.1f}s delay")

    def _browse_folder(self):
        folder = ctk.filedialog.askdirectory(initialdir=self.output_entry.get())
        if folder:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, os.path.abspath(folder))

    def _open_output_folder(self):
        folder = self.output_entry.get()
        os.makedirs(folder, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])

    def _open_file(self, filepath):
        if os.path.exists(filepath):
            if sys.platform == "win32":
                os.startfile(filepath)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", filepath])
            else:
                subprocess.Popen(["xdg-open", filepath])

    def _append_log(self, text, level="INFO"):
        timestamp = time.strftime("%H:%M:%S")
        prefix = {
            "INFO": "🔵 INFO ",
            "SUCCESS": "🟢 SUCCESS",
            "WARNING": "🟡 WARN ",
            "ERROR": "🔴 ERROR"
        }.get(level, "[LOG]  ")

        if level == "WARNING":
            self.metric_warnings += 1
            self.lbl_warnings.configure(text=str(self.metric_warnings))

        entry = f" [{timestamp}]  {prefix}  {text}\n"
        self.log_textbox.insert("end", entry)
        self.log_textbox.see("end")

        if hasattr(self, "full_log_textbox"):
            self.full_log_textbox.insert("end", entry)
            self.full_log_textbox.see("end")

    def _process_log_queue(self):
        while not self.log_queue.empty():
            msg_type, payload = self.log_queue.get()
            if msg_type == "LOG":
                text, level = payload
                self._append_log(text, level)
            elif msg_type == "STATUS":
                title, sub = payload
                self.status_title.configure(text=title)
                self.status_sub.configure(text=sub)
            elif msg_type == "DISCOVERED":
                count = payload
                self.metric_discovered = count
                self.lbl_discovered.configure(text=str(count))
            elif msg_type == "PROGRESS":
                cur, total, current_url = payload
                frac = cur / max(total, 1)
                pct = int(frac * 100)
                self.progress_bar.set(frac)
                self.progress_pct_lbl.configure(text=f"{pct}%")
                self.status_sub.configure(text=f"[{cur}/{total}] Capturing: {current_url}")
            elif msg_type == "RESULT":
                url, filepath, status = payload
                if status == 'ok':
                    self.metric_success += 1
                    self.metric_saved += 1
                else:
                    self.metric_failed += 1

                self.lbl_saved.configure(text=str(self.metric_saved))
                self.lbl_success.configure(text=str(self.metric_success))
                self.lbl_failed.configure(text=str(self.metric_failed))

                self._add_gallery_item(url, filepath, status)
            elif msg_type == "DONE":
                self._on_process_complete()

        # Update duration timer
        if self.is_running and self.start_time:
            elapsed = int(time.time() - self.start_time)
            mins, secs = divmod(elapsed, 60)
            self.lbl_duration.configure(text=f"{mins:02d}:{secs:02d}")

        self.after(100, self._process_log_queue)

    def _add_gallery_item(self, url, filepath, status):
        # Clear empty placeholders
        if hasattr(self, 'empty_gallery_lbl') and self.empty_gallery_lbl.winfo_exists():
            self.empty_gallery_lbl.destroy()
        if hasattr(self, 'full_empty_lbl') and self.full_empty_lbl.winfo_exists():
            self.full_empty_lbl.destroy()

        self.captured_items.append((url, filepath, status))

        index = len(self.captured_items) - 1
        row = index // 3
        col = index % 3

        # Add to both mini gallery and full gallery view
        for target_scroll in [self.gallery_scroll, getattr(self, 'full_gallery_scroll', None)]:
            if not target_scroll:
                continue

            card = ctk.CTkFrame(
                target_scroll,
                fg_color=FLUENT_CARD_BG,
                corner_radius=8,
                border_width=1,
                border_color=FLUENT_CARD_BORDER
            )
            card.grid(row=row, column=col, padx=6, pady=6, sticky="nsew")

            if status == 'ok' and filepath and os.path.exists(filepath):
                try:
                    img = Image.open(filepath)
                    img.thumbnail((260, 160))
                    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(img.width, img.height))
                    img_lbl = ctk.CTkLabel(card, image=ctk_img, text="")
                    img_lbl.pack(padx=6, pady=(6, 4))
                except Exception:
                    err_lbl = ctk.CTkLabel(card, text="[Image Preview Unavailable]", text_color=FLUENT_DANGER)
                    err_lbl.pack(padx=6, pady=25)
            else:
                err_lbl = ctk.CTkLabel(card, text="❌ Capture Failed", text_color=FLUENT_DANGER, font=ctk.CTkFont(weight="bold"))
                err_lbl.pack(padx=6, pady=25)

            parsed = urlparse(url)
            short_url = parsed.path or parsed.netloc
            if len(short_url) > 30:
                short_url = short_url[:27] + "..."
            url_lbl = ctk.CTkLabel(
                card,
                text=short_url,
                font=ctk.CTkFont(family="Segoe UI", size=11, weight="bold"),
                text_color=FLUENT_TEXT_PRIMARY
            )
            url_lbl.pack(padx=6, pady=(2, 2))

            btn_frame = ctk.CTkFrame(card, fg_color="transparent")
            btn_frame.pack(padx=6, pady=(2, 6), fill="x")

            if status == 'ok' and filepath:
                open_img_btn = ctk.CTkButton(
                    btn_frame,
                    text="View",
                    height=24,
                    corner_radius=4,
                    font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
                    fg_color=FLUENT_ACCENT,
                    hover_color=FLUENT_ACCENT_HOVER,
                    command=lambda p=filepath: self._open_file(p)
                )
                open_img_btn.pack(side="left", expand=True, fill="x", padx=(0, 3))

            copy_btn = ctk.CTkButton(
                btn_frame,
                text="Copy URL",
                height=24,
                corner_radius=4,
                font=ctk.CTkFont(family="Segoe UI", size=10),
                fg_color="#333742",
                hover_color=FLUENT_CARD_BORDER,
                command=lambda u=url: self.clipboard_clear() or self.clipboard_append(u)
            )
            copy_btn.pack(side="right", expand=True, fill="x", padx=(3, 0))

    # ==========================================================================
    # Process Execution Controls
    # ==========================================================================
    def _start_process(self):
        raw_url = self.url_entry.get().strip()
        if not raw_url:
            self._append_log("Please enter a valid website URL or domain.", "ERROR")
            return

        target_url = raw_url if raw_url.startswith(("http://", "https://")) else f"https://{raw_url}"

        self.is_running = True
        self.cancel_requested = False
        self.start_time = time.time()

        self.metric_discovered = 0
        self.metric_saved = 0
        self.metric_success = 0
        self.metric_failed = 0
        self.metric_warnings = 0

        self.lbl_discovered.configure(text="0")
        self.lbl_saved.configure(text="0")
        self.lbl_success.configure(text="0")
        self.lbl_failed.configure(text="0")
        self.lbl_warnings.configure(text="0")
        self.lbl_duration.configure(text="00:00")

        # Pulse / Active state for Hero Status Orb
        self.status_icon_box.configure(fg_color="#0F3858", border_color=FLUENT_ACCENT)
        self.status_icon_lbl.configure(text="🔄", text_color=FLUENT_ACCENT_LIGHT)

        self.start_btn.configure(state="disabled", fg_color="#2D3037")
        self.stop_btn.configure(state="normal", fg_color=FLUENT_DANGER)
        self.progress_bar.set(0.0)
        self.progress_pct_lbl.configure(text="0%")

        # Clear previous gallery items
        for child in self.gallery_scroll.winfo_children():
            child.destroy()
        if hasattr(self, 'full_gallery_scroll'):
            for child in self.full_gallery_scroll.winfo_children():
                child.destroy()
        self.captured_items.clear()

        # Viewport resolution
        vp_str = self.viewport_var.get()
        vp_width = 1440
        if "3840px" in vp_str: vp_width = 3840
        elif "1920px" in vp_str: vp_width = 1920
        elif "768px" in vp_str: vp_width = 768
        elif "375px" in vp_str: vp_width = 375

        max_pages = int(self.max_pages_slider.get())
        load_delay = float(self.delay_slider.get())
        unhide_anim = self.unhide_var.get()
        mode = self.mode_var.get()
        output_dir = self.output_entry.get()

        self.log_queue.put(("STATUS", ("Status: Crawling Site...", f"Discovering pages on {target_url}")))
        self._append_log(f"Starting session for: {target_url} (Mode: {mode}, Limit: {max_pages}, Delay: {load_delay}s, Unhide: {unhide_anim})", "INFO")

        # Launch backend worker thread
        threading.Thread(
            target=self._run_backend_async,
            args=(target_url, mode, max_pages, vp_width, load_delay, unhide_anim, output_dir),
            daemon=True
        ).start()

    def _stop_process(self):
        if self.is_running:
            self.cancel_requested = True
            self._append_log("Stop requested. Halting capture worker...", "WARNING")
            self.status_icon_box.configure(fg_color=FLUENT_WARNING_BG, border_color=FLUENT_WARNING)
            self.status_icon_lbl.configure(text="⚠️", text_color=FLUENT_WARNING)
            self.status_title.configure(text="Status: Stopping...")
            self.stop_btn.configure(state="disabled")

    def _on_process_complete(self):
        self.is_running = False
        self.start_btn.configure(state="normal", fg_color=FLUENT_ACCENT)
        self.stop_btn.configure(state="disabled")

        self.status_icon_box.configure(fg_color=FLUENT_SUCCESS_BG, border_color=FLUENT_SUCCESS)
        self.status_icon_lbl.configure(text="✓", text_color=FLUENT_SUCCESS)
        self.status_title.configure(text="Status: Completed")
        self.status_sub.configure(text=f"Task complete! {self.metric_saved} full-page screenshot(s) saved.")
        self.progress_bar.set(1.0)
        self.progress_pct_lbl.configure(text="100%")
        self._append_log(f"All operations finished! Files saved in: {self.output_entry.get()}", "SUCCESS")

    def _run_backend_async(self, start_url, mode, max_pages, viewport_width, load_delay, unhide_anim, output_dir):
        asyncio.run(self._async_backend_task(start_url, mode, max_pages, viewport_width, load_delay, unhide_anim, output_dir))

    async def _async_backend_task(self, start_url, mode, max_pages, viewport_width, load_delay, unhide_anim, output_dir):
        def log_cb(msg, level="INFO"):
            self.log_queue.put(("LOG", (msg, level)))

        def cancel_check():
            return self.cancel_requested

        log_cb("Verifying Playwright browser engine availability...", "INFO")
        ready = await ensure_playwright_ready_async(log_cb)
        if not ready:
            log_cb("Playwright browser setup failed. Aborting task.", "ERROR")
            self.log_queue.put(("DONE", None))
            return

        urls = set()
        root_netloc = urlparse(start_url).netloc

        if mode == "Single Page":
            urls = {normalize_url(start_url)}
        elif mode == "Sitemap XML":
            log_cb("Scanning site sitemap.xml...", "INFO")
            urls = get_sitemap_urls(start_url, log_cb, cancel_check)
            if not urls:
                log_cb("No sitemap URLs discovered. Falling back to internal BFS crawl...", "WARNING")
                urls = crawl_site(start_url, max_pages=max_pages, log_callback=log_cb, cancel_check=cancel_check)
        else: # BFS Crawl
            urls = crawl_site(start_url, max_pages=max_pages, log_callback=log_cb, cancel_check=cancel_check)

        if len(urls) < 2 and mode != "Single Page" and not cancel_check():
            log_cb("Few static links discovered. Engaging Playwright SPA rendering...", "INFO")
            spa_urls = await crawl_site_playwright_spa(start_url, root_netloc, max_pages=max_pages, log_callback=log_cb)
            urls.update(spa_urls)

        urls = set(list(urls)[:max_pages])
        self.log_queue.put(("DISCOVERED", len(urls)))
        log_cb(f"Discovered {len(urls)} unique page(s) to capture.", "SUCCESS")

        if not urls or cancel_check():
            self.log_queue.put(("DONE", None))
            return

        self.log_queue.put(("STATUS", ("Status: Capturing Screenshots...", f"Taking screenshots of {len(urls)} unique pages...")))

        def progress_cb(cur, total, url):
            self.log_queue.put(("PROGRESS", (cur, total, url)))

        def result_cb(url, filepath, status):
            self.log_queue.put(("RESULT", (url, filepath, status)))

        await screenshot_worker_async(
            urls=urls,
            output_dir=output_dir,
            viewport_width=viewport_width,
            load_delay_sec=load_delay,
            unhide_animations=unhide_anim,
            progress_callback=progress_cb,
            log_callback=log_cb,
            result_callback=result_cb,
            cancel_check=cancel_check
        )

        self.log_queue.put(("DONE", None))


def main():
    parser = argparse.ArgumentParser(description="Website Full-Page Screenshotter Pro")
    parser.add_argument('--cli', action='store_true', help='Run in CLI mode instead of GUI')
    parser.add_argument('domain', nargs='?', help='Domain or URL for CLI mode')
    parser.add_argument('--max-pages', type=int, default=50, help='Max pages to screenshot')
    parser.add_argument('--delay', type=float, default=1.5, help='Page load delay in seconds')
    parser.add_argument('--unhide-animations', action='store_true', help='Force un-hide off-screen animated elements')
    parser.add_argument('--output-dir', default=get_default_screenshots_dir(), help='Output folder (default: Pictures/Screenshots)')
    args = parser.parse_args()

    if (args.cli or args.domain) and args.domain:
        start_url = args.domain if args.domain.startswith('http') else f'https://{args.domain}'
        print(f"Starting CLI capture for: {start_url}")
        log_cb = lambda msg, level="INFO": print(f"[{level}] {msg}")
        asyncio.run(ensure_playwright_ready_async(log_cb))
        urls = crawl_site(start_url, max_pages=args.max_pages, log_callback=log_cb)
        asyncio.run(screenshot_worker_async(urls, args.output_dir, load_delay_sec=args.delay, unhide_animations=args.unhide_animations, log_callback=log_cb))
    else:
        if not _HAS_CTK:
            print("[ERROR] CustomTkinter or PIL is missing. Please install dependencies:\n  pip install customtkinter pillow playwright requests bs4\nOr run CLI mode:\n  python website_screenshotter.py --cli example.com")
            return
        app = WebsiteScreenshotterGUI()
        app.mainloop()


if __name__ == '__main__':
    main()
