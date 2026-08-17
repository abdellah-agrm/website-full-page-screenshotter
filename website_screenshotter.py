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


class WebsiteScreenshotterGUI(BaseGUI):
    """Modern Dark Mode GUI matching Screenshotter Pro design."""

    def __init__(self):
        super().__init__()

        self.title("Screenshotter Pro")
        self.geometry("1240 x 800")
        self.minsize(1080, 720)
        self.configure(fg_color="#0D111A")

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

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._create_sidebar()
        self._create_main_panel()

        # Start periodic log queue & timer polling
        self.after(100, self._process_log_queue)

    def _setup_app_icon(self):
        """Set application icon for window titlebar and taskbar."""
        script_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        ico_path = os.path.join(script_dir, "assets", "icon.ico")
        png_path = os.path.join(script_dir, "assets", "icon.png")
        root_png = os.path.join(script_dir, "website_screenshotter_icon.png")

        icon_path = None
        if os.path.exists(ico_path):
            icon_path = ico_path
        elif os.path.exists(png_path):
            icon_path = png_path
        elif os.path.exists(root_png):
            icon_path = root_png

        if icon_path:
            try:
                if icon_path.endswith(".ico"):
                    self.wm_iconbitmap(icon_path)
                else:
                    from PIL import ImageTk
                    img = Image.open(icon_path)
                    photo = ImageTk.PhotoImage(img.resize((32, 32)))
                    self.iconphoto(False, photo)
                    self._app_icon_ref = photo
            except Exception:
                pass

    def _create_sidebar(self):
        self.sidebar = ctk.CTkFrame(self, width=340, corner_radius=12, fg_color="#161B26")
        self.sidebar.grid(row=0, column=0, sticky="nsew", padx=(15, 8), pady=15)
        self.sidebar.grid_rowconfigure(14, weight=1)

        # 1. Header Logo
        header_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        header_frame.grid(row=0, column=0, padx=20, pady=(20, 15), sticky="ew")

        logo_box = ctk.CTkFrame(header_frame, width=44, height=44, corner_radius=10, fg_color="#1E2330")
        logo_box.grid(row=0, column=0, rowspan=2, padx=(0, 12), pady=0)
        logo_box.grid_propagate(False)

        script_dir = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
        png_path = os.path.join(script_dir, "assets", "icon.png")
        root_png = os.path.join(script_dir, "website_screenshotter_icon.png")
        icon_path = png_path if os.path.exists(png_path) else (root_png if os.path.exists(root_png) else None)

        if icon_path:
            try:
                icon_pil = Image.open(icon_path)
                ctk_icon = ctk.CTkImage(light_image=icon_pil, dark_image=icon_pil, size=(36, 36))
                logo_lbl = ctk.CTkLabel(logo_box, image=ctk_icon, text="")
            except Exception:
                logo_lbl = ctk.CTkLabel(logo_box, text="📸", font=ctk.CTkFont(size=22))
        else:
            logo_lbl = ctk.CTkLabel(logo_box, text="📸", font=ctk.CTkFont(size=22))

        logo_lbl.place(relx=0.5, rely=0.5, anchor="center")

        title_lbl = ctk.CTkLabel(header_frame, text="Screenshotter Pro", font=ctk.CTkFont(size=18, weight="bold"), text_color="#FFFFFF")
        title_lbl.grid(row=0, column=1, sticky="w")

        sub_lbl = ctk.CTkLabel(header_frame, text="Automated Full-Page Web Captures", font=ctk.CTkFont(size=11), text_color="#8992A6")
        sub_lbl.grid(row=1, column=1, sticky="w")

        # 2. Target URL Input
        ctk.CTkLabel(self.sidebar, text="Target URL / Domain", font=ctk.CTkFont(size=12, weight="bold"), text_color="#E2E8F0").grid(row=1, column=0, padx=20, pady=(5, 4), sticky="w")

        self.url_entry = ctk.CTkEntry(
            self.sidebar,
            placeholder_text="🌐  e.g. sharedsystem.com/fr",
            height=38,
            fg_color="#0F131E",
            border_color="#2A324B",
            text_color="#FFFFFF"
        )
        self.url_entry.grid(row=2, column=0, padx=20, pady=(0, 12), sticky="ew")

        # 3. Discovery Mode
        ctk.CTkLabel(self.sidebar, text="Discovery Mode", font=ctk.CTkFont(size=12, weight="bold"), text_color="#E2E8F0").grid(row=3, column=0, padx=20, pady=(5, 4), sticky="w")

        self.mode_var = ctk.StringVar(value="BFS Crawl")
        self.mode_seg = ctk.CTkSegmentedButton(
            self.sidebar,
            values=["BFS Crawl", "Sitemap XML", "Single Page"],
            variable=self.mode_var,
            selected_color="#5865F2",
            selected_hover_color="#4752C4",
            unselected_color="#0F131E",
            unselected_hover_color="#21283B",
            text_color="#FFFFFF"
        )
        self.mode_seg.grid(row=4, column=0, padx=20, pady=(0, 12), sticky="ew")

        # 4. Max Pages Slider
        max_header = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        max_header.grid(row=5, column=0, padx=20, pady=(0, 2), sticky="ew")
        max_header.columnconfigure(1, weight=1)

        ctk.CTkLabel(max_header, text="Max Pages", font=ctk.CTkFont(size=12, weight="bold"), text_color="#E2E8F0").grid(row=0, column=0, sticky="w")
        self.max_pages_val_label = ctk.CTkLabel(max_header, text="11", font=ctk.CTkFont(size=12, weight="bold"), text_color="#5865F2")
        self.max_pages_val_label.grid(row=0, column=1, sticky="e")

        self.max_pages_slider = ctk.CTkSlider(
            self.sidebar,
            from_=1, to=150, number_of_steps=149,
            button_color="#5865F2", button_hover_color="#4752C4",
            progress_color="#5865F2", fg_color="#0F131E",
            command=self._on_pages_slider_change
        )
        self.max_pages_slider.set(11)
        self.max_pages_slider.grid(row=6, column=0, padx=20, pady=(0, 12), sticky="ew")

        # 5. Load Delay Slider
        delay_header = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        delay_header.grid(row=7, column=0, padx=20, pady=(0, 2), sticky="ew")
        delay_header.columnconfigure(1, weight=1)

        ctk.CTkLabel(delay_header, text="Load Delay (sec)", font=ctk.CTkFont(size=12, weight="bold"), text_color="#E2E8F0").grid(row=0, column=0, sticky="w")
        self.delay_val_label = ctk.CTkLabel(delay_header, text="4.0s", font=ctk.CTkFont(size=12, weight="bold"), text_color="#5865F2")
        self.delay_val_label.grid(row=0, column=1, sticky="e")

        self.delay_slider = ctk.CTkSlider(
            self.sidebar,
            from_=0.5, to=5.0, number_of_steps=45,
            button_color="#5865F2", button_hover_color="#4752C4",
            progress_color="#5865F2", fg_color="#0F131E",
            command=self._on_delay_slider_change
        )
        self.delay_slider.set(4.0)
        self.delay_slider.grid(row=8, column=0, padx=20, pady=(0, 12), sticky="ew")

        # 6. Viewport Resolution
        ctk.CTkLabel(self.sidebar, text="Viewport Resolution", font=ctk.CTkFont(size=12, weight="bold"), text_color="#E2E8F0").grid(row=9, column=0, padx=20, pady=(5, 4), sticky="w")

        self.viewport_var = ctk.StringVar(value="Full HD (1920px)")
        self.viewport_combo = ctk.CTkOptionMenu(
            self.sidebar,
            values=["Desktop 4K (3840px)", "Full HD (1920px)", "Desktop (1440px)", "Tablet (768px)", "Mobile (375px)"],
            variable=self.viewport_var,
            height=36,
            fg_color="#0F131E",
            button_color="#21283B",
            button_hover_color="#2A324B",
            text_color="#FFFFFF"
        )
        self.viewport_combo.grid(row=10, column=0, padx=20, pady=(0, 12), sticky="ew")

        # 7. Output Directory
        ctk.CTkLabel(self.sidebar, text="Output Directory", font=ctk.CTkFont(size=12, weight="bold"), text_color="#E2E8F0").grid(row=11, column=0, padx=20, pady=(5, 4), sticky="w")

        folder_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        folder_frame.grid(row=12, column=0, padx=20, pady=(0, 10), sticky="ew")
        folder_frame.columnconfigure(0, weight=1)

        self.output_entry = ctk.CTkEntry(folder_frame, height=36, fg_color="#0F131E", border_color="#2A324B", text_color="#FFFFFF")
        self.output_entry.insert(0, os.path.abspath("screenshots"))
        self.output_entry.grid(row=0, column=0, padx=(0, 6), sticky="ew")

        browse_btn = ctk.CTkButton(folder_frame, text="📁", width=38, height=36, fg_color="#21283B", hover_color="#2A324B", command=self._browse_folder)
        browse_btn.grid(row=0, column=1, sticky="e")

        # 8. Unhide Checkbox
        self.unhide_var = ctk.BooleanVar(value=False)
        self.unhide_cb = ctk.CTkCheckBox(
            self.sidebar,
            text="Force Un-hide Animations (Off-screen)",
            variable=self.unhide_var,
            font=ctk.CTkFont(size=11),
            checkbox_width=18, checkbox_height=18,
            border_width=2, fg_color="#5865F2", hover_color="#4752C4", text_color="#A6ADC8"
        )
        self.unhide_cb.grid(row=13, column=0, padx=20, pady=(0, 15), sticky="w")

        # 9. Action Buttons Section
        self.start_btn = ctk.CTkButton(
            self.sidebar,
            text="🚀  Start Screenshots",
            height=42,
            font=ctk.CTkFont(size=14, weight="bold"),
            fg_color="#6366F1", text_color="#FFFFFF", hover_color="#4F46E5",
            corner_radius=8,
            command=self._start_process
        )
        self.start_btn.grid(row=15, column=0, padx=20, pady=(10, 8), sticky="ew")

        self.stop_btn = ctk.CTkButton(
            self.sidebar,
            text="⛔  Stop Session",
            height=38,
            font=ctk.CTkFont(size=13, weight="bold"),
            fg_color="#E15B64", text_color="#FFFFFF", hover_color="#CB4B54",
            corner_radius=8, state="disabled",
            command=self._stop_process
        )
        self.stop_btn.grid(row=16, column=0, padx=20, pady=(0, 8), sticky="ew")

        open_folder_btn = ctk.CTkButton(
            self.sidebar,
            text="📂  Open Output Folder",
            height=36,
            font=ctk.CTkFont(size=12),
            fg_color="#21283B", text_color="#E2E8F0", hover_color="#2A324B",
            corner_radius=8,
            command=self._open_output_folder
        )
        open_folder_btn.grid(row=17, column=0, padx=20, pady=(0, 15), sticky="ew")

        # 10. Sidebar Footer
        footer_lbl = ctk.CTkLabel(self.sidebar, text="Screenshotter Pro    v2.0.0", font=ctk.CTkFont(size=11), text_color="#454F68")
        footer_lbl.grid(row=18, column=0, pady=(0, 15))

    def _create_main_panel(self):
        self.main_frame = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=(0, 15), pady=15)
        self.main_frame.grid_columnconfigure(0, weight=1)
        self.main_frame.grid_rowconfigure(2, weight=1)

        # Top Header Bar Card
        top_card = ctk.CTkFrame(self.main_frame, fg_color="#161B26", corner_radius=12, border_width=1, border_color="#232A3B")
        top_card.grid(row=0, column=0, padx=0, pady=(0, 10), sticky="ew")
        top_card.grid_columnconfigure(1, weight=1)

        # Green Check Icon Box
        self.status_icon_box = ctk.CTkFrame(top_card, width=46, height=46, corner_radius=23, fg_color="#143828")
        self.status_icon_box.grid(row=0, column=0, rowspan=2, padx=(15, 12), pady=12)
        self.status_icon_box.grid_propagate(False)

        self.status_icon_lbl = ctk.CTkLabel(self.status_icon_box, text="✓", font=ctk.CTkFont(size=22, weight="bold"), text_color="#22C55E")
        self.status_icon_lbl.place(relx=0.5, rely=0.5, anchor="center")

        # Title & Subtitle
        self.status_title = ctk.CTkLabel(top_card, text="Status: Ready", font=ctk.CTkFont(size=16, weight="bold"), text_color="#CDD6F4")
        self.status_title.grid(row=0, column=1, padx=0, pady=(12, 1), sticky="w")

        self.status_sub = ctk.CTkLabel(top_card, text="Enter a website URL and click 'Start Screenshots' to begin.", font=ctk.CTkFont(size=12), text_color="#8992A6")
        self.status_sub.grid(row=1, column=1, padx=0, pady=(0, 12), sticky="w")

        # Top Right Badges
        top_right_frame = ctk.CTkFrame(top_card, fg_color="transparent")
        top_right_frame.grid(row=0, column=2, rowspan=2, padx=15, pady=12, sticky="e")

        self.saved_badge_lbl = ctk.CTkLabel(
            top_right_frame, text="0\nSaved", font=ctk.CTkFont(size=11, weight="bold"),
            fg_color="#0F131E", corner_radius=8, width=54, height=40, text_color="#89B4FA"
        )
        self.saved_badge_lbl.grid(row=0, column=0, padx=(0, 8))

        top_folder_btn = ctk.CTkButton(
            top_right_frame, text="📥", width=40, height=40, corner_radius=8,
            fg_color="#3B82F6", hover_color="#2563EB", command=self._open_output_folder
        )
        top_folder_btn.grid(row=0, column=1)

        # Progress Bar Frame
        prog_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        prog_frame.grid(row=1, column=0, padx=0, pady=(0, 12), sticky="ew")
        prog_frame.columnconfigure(0, weight=1)

        self.progress_bar = ctk.CTkProgressBar(
            prog_frame, height=10, corner_radius=5,
            progress_color="#5865F2", fg_color="#161B26"
        )
        self.progress_bar.set(0.0)
        self.progress_bar.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        self.progress_pct_lbl = ctk.CTkLabel(prog_frame, text="0%", font=ctk.CTkFont(size=12, weight="bold"), text_color="#89B4FA")
        self.progress_pct_lbl.grid(row=0, column=1, sticky="e")

        # Main Segmented Tabview Container
        self.tabview = ctk.CTkTabview(
            self.main_frame,
            fg_color="#161B26",
            corner_radius=12,
            segmented_button_selected_color="#5865F2",
            segmented_button_selected_hover_color="#4752C4",
            segmented_button_unselected_color="#0F131E",
            segmented_button_unselected_hover_color="#21283B"
        )
        self.tabview.grid(row=2, column=0, padx=0, pady=(0, 12), sticky="nsew")

        self.tab_logs = self.tabview.add(">_  Activity Console Log")
        self.tab_gallery = self.tabview.add("🖼️  Screenshot Gallery")

        self._setup_logs_tab()
        self._setup_gallery_tab()

        # Bottom 6 Analytics Metric Cards Row
        self._create_metric_cards_row()

    def _create_metric_cards_row(self):
        cards_frame = ctk.CTkFrame(self.main_frame, fg_color="transparent")
        cards_frame.grid(row=3, column=0, padx=0, pady=0, sticky="ew")
        cards_frame.grid_columnconfigure((0, 1, 2, 3, 4, 5), weight=1)

        def make_card(col, icon, bg_color, fg_color, init_val, title):
            card = ctk.CTkFrame(cards_frame, fg_color="#161B26", corner_radius=10, border_width=1, border_color="#232A3B")
            card.grid(row=0, column=col, padx=(0 if col == 0 else 4, 0 if col == 5 else 4), pady=0, sticky="nsew")
            card.grid_columnconfigure(1, weight=1)

            ibox = ctk.CTkFrame(card, width=36, height=36, corner_radius=18, fg_color=bg_color)
            ibox.grid(row=0, column=0, rowspan=2, padx=(8, 8), pady=8)
            ibox.grid_propagate(False)

            ilbl = ctk.CTkLabel(ibox, text=icon, font=ctk.CTkFont(size=13, weight="bold"), text_color=fg_color)
            ilbl.place(relx=0.5, rely=0.5, anchor="center")

            vlbl = ctk.CTkLabel(card, text=init_val, font=ctk.CTkFont(size=14, weight="bold"), text_color="#CDD6F4")
            vlbl.grid(row=0, column=1, padx=(0, 6), pady=(8, 0), sticky="w")

            tlbl = ctk.CTkLabel(card, text=title, font=ctk.CTkFont(size=10), text_color="#8992A6")
            tlbl.grid(row=1, column=1, padx=(0, 6), pady=(0, 8), sticky="w")

            return vlbl

        self.lbl_discovered = make_card(0, "🌐", "#1E293B", "#3B82F6", "0", "Pages Discovered")
        self.lbl_saved = make_card(1, "📷", "#2E1065", "#A855F7", "0", "Screenshots Saved")
        self.lbl_success = make_card(2, "✓", "#064E3B", "#22C55E", "0", "Successful")
        self.lbl_failed = make_card(3, "✕", "#7F1D1D", "#EF4444", "0", "Failed")
        self.lbl_warnings = make_card(4, "⚠️", "#78350F", "#F59E0B", "0", "Warnings")
        self.lbl_duration = make_card(5, "⏱️", "#134E4A", "#06B6D4", "00:00", "Duration")

    def _setup_logs_tab(self):
        self.tab_logs.grid_columnconfigure(0, weight=1)
        self.tab_logs.grid_rowconfigure(0, weight=1)

        self.log_textbox = ctk.CTkTextbox(
            self.tab_logs,
            font=ctk.CTkFont(family="Consolas", size=12),
            fg_color="#0B0E14",
            text_color="#CDD6F4",
            wrap="none",
            corner_radius=8
        )
        self.log_textbox.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        
        self._append_log("System initialized. Ready for domain capture.", "INFO")

    def _setup_gallery_tab(self):
        self.tab_gallery.grid_columnconfigure(0, weight=1)
        self.tab_gallery.grid_rowconfigure(0, weight=1)

        self.gallery_scroll = ctk.CTkScrollableFrame(
            self.tab_gallery,
            fg_color="#0B0E14",
            corner_radius=8
        )
        self.gallery_scroll.grid(row=0, column=0, sticky="nsew", padx=5, pady=5)
        self.gallery_scroll.grid_columnconfigure((0, 1, 2), weight=1)

        self.empty_gallery_lbl = ctk.CTkLabel(
            self.gallery_scroll,
            text="No screenshots captured yet.\nCaptured images will appear here in real-time.",
            font=ctk.CTkFont(size=14),
            text_color="#8992A6"
        )
        self.empty_gallery_lbl.grid(row=0, column=0, columnspan=3, pady=60)

    def _on_pages_slider_change(self, value):
        self.max_pages_val_label.configure(text=str(int(value)))

    def _on_delay_slider_change(self, value):
        self.delay_val_label.configure(text=f"{value:.1f}s")

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
                self.saved_badge_lbl.configure(text=f"{self.metric_saved}\nSaved")

                self._add_gallery_item(url, filepath, status)
            elif msg_type == "DONE":
                self._on_process_complete()

        # Update timer if running
        if self.is_running and self.start_time:
            elapsed = int(time.time() - self.start_time)
            mins, secs = divmod(elapsed, 60)
            self.lbl_duration.configure(text=f"{mins:02d}:{secs:02d}")

        self.after(100, self._process_log_queue)

    def _add_gallery_item(self, url, filepath, status):
        if hasattr(self, 'empty_gallery_lbl') and self.empty_gallery_lbl.winfo_exists():
            self.empty_gallery_lbl.destroy()

        self.captured_items.append((url, filepath, status))

        index = len(self.captured_items) - 1
        row = index // 3
        col = index % 3

        card = ctk.CTkFrame(self.gallery_scroll, fg_color="#161B26", corner_radius=8, border_width=1, border_color="#232A3B")
        card.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")

        if status == 'ok' and filepath and os.path.exists(filepath):
            try:
                img = Image.open(filepath)
                img.thumbnail((260, 160))
                ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=(img.width, img.height))
                img_lbl = ctk.CTkLabel(card, image=ctk_img, text="")
                img_lbl.pack(padx=8, pady=(8, 4))
            except Exception:
                err_lbl = ctk.CTkLabel(card, text="[Image Preview Error]", text_color="#E15B64")
                err_lbl.pack(padx=8, pady=30)
        else:
            err_lbl = ctk.CTkLabel(card, text="❌ Failed Capture", text_color="#E15B64", font=ctk.CTkFont(weight="bold"))
            err_lbl.pack(padx=8, pady=30)

        parsed = urlparse(url)
        short_url = parsed.path or parsed.netloc
        if len(short_url) > 30:
            short_url = short_url[:27] + "..."
        url_lbl = ctk.CTkLabel(card, text=short_url, font=ctk.CTkFont(size=11, weight="bold"), text_color="#CDD6F4")
        url_lbl.pack(padx=8, pady=(2, 2))

        btn_frame = ctk.CTkFrame(card, fg_color="transparent")
        btn_frame.pack(padx=8, pady=(2, 8), fill="x")

        if status == 'ok' and filepath:
            open_img_btn = ctk.CTkButton(
                btn_frame, 
                text="View", 
                height=26, 
                font=ctk.CTkFont(size=11),
                fg_color="#5865F2", 
                text_color="#FFFFFF",
                hover_color="#4752C4",
                command=lambda p=filepath: self._open_file(p)
            )
            open_img_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))

        copy_btn = ctk.CTkButton(
            btn_frame, 
            text="Copy URL", 
            height=26, 
            font=ctk.CTkFont(size=11),
            fg_color="#21283B",
            hover_color="#2A324B",
            command=lambda u=url: self.clipboard_clear() or self.clipboard_append(u)
        )
        copy_btn.pack(side="right", expand=True, fill="x", padx=(4, 0))

    def _open_file(self, filepath):
        if os.path.exists(filepath):
            if sys.platform == "win32":
                os.startfile(filepath)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", filepath])
            else:
                subprocess.Popen(["xdg-open", filepath])

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
        self.saved_badge_lbl.configure(text="0\nSaved")

        self.status_icon_box.configure(fg_color="#1F293D")
        self.status_icon_lbl.configure(text="🔄", text_color="#89B4FA")
        
        self.start_btn.configure(state="disabled", fg_color="#313244")
        self.stop_btn.configure(state="normal", fg_color="#E15B64")
        self.progress_bar.set(0.0)
        self.progress_pct_lbl.configure(text="0%")

        # Clear previous gallery
        for child in self.gallery_scroll.winfo_children():
            child.destroy()
        self.captured_items.clear()

        # Extract viewport size
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
        self._append_log(f"Starting process for domain: {target_url} (Mode: {mode}, Max: {max_pages}, Delay: {load_delay}s, Unhide: {unhide_anim})", "INFO")

        # Launch background thread
        threading.Thread(
            target=self._run_backend_async,
            args=(target_url, mode, max_pages, vp_width, load_delay, unhide_anim, output_dir),
            daemon=True
        ).start()

    def _stop_process(self):
        if self.is_running:
            self.cancel_requested = True
            self._append_log("Stop signal sent! Wrapping up active capture...", "WARNING")
            self.stop_btn.configure(state="disabled")

    def _on_process_complete(self):
        self.is_running = False
        self.start_btn.configure(state="normal", fg_color="#6366F1")
        self.stop_btn.configure(state="disabled")
        
        self.status_icon_box.configure(fg_color="#143828")
        self.status_icon_lbl.configure(text="✓", text_color="#22C55E")
        self.status_title.configure(text="Status: Completed")
        self.status_sub.configure(text=f"Done! {self.metric_saved} screenshot(s) saved.")
        self.progress_bar.set(1.0)
        self.progress_pct_lbl.configure(text="100%")
        self._append_log(f"Task finished! All screenshots saved in '{self.output_entry.get()}'", "SUCCESS")

    def _run_backend_async(self, start_url, mode, max_pages, viewport_width, load_delay, unhide_anim, output_dir):
        asyncio.run(self._async_backend_task(start_url, mode, max_pages, viewport_width, load_delay, unhide_anim, output_dir))

    async def _async_backend_task(self, start_url, mode, max_pages, viewport_width, load_delay, unhide_anim, output_dir):
        def log_cb(msg, level="INFO"):
            self.log_queue.put(("LOG", (msg, level)))

        def cancel_check():
            return self.cancel_requested

        log_cb("Verifying Playwright browser availability...", "INFO")
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
            log_cb("Searching site sitemap.xml...", "INFO")
            urls = get_sitemap_urls(start_url, log_cb, cancel_check)
            if not urls:
                log_cb("No sitemap URLs found. Falling back to internal BFS crawl...", "WARNING")
                urls = crawl_site(start_url, max_pages=max_pages, log_callback=log_cb, cancel_check=cancel_check)
        else: # BFS Crawl
            urls = crawl_site(start_url, max_pages=max_pages, log_callback=log_cb, cancel_check=cancel_check)

        if len(urls) < 2 and mode != "Single Page" and not cancel_check():
            log_cb("Very few links discovered via static HTML. Engaging Playwright SPA rendering...", "INFO")
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
    parser.add_argument('--output-dir', default='screenshots', help='Output folder')
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
