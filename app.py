import os
import re
import json
import glob
import shutil
import asyncio
import hashlib
from collections import deque
from urllib.parse import urlparse, urljoin, parse_qs, urlencode, urlunparse
from datetime import datetime
from bs4 import BeautifulSoup
from flask import Flask, render_template, request, redirect, url_for, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from playwright.async_api import async_playwright

app = Flask(__name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DOWNLOAD_FOLDER = os.path.join(BASE_DIR, 'downloads')
PREVIEW_FOLDER = os.path.join(BASE_DIR, 'static', 'previews')
AUTH_STATE_PATH = os.path.join(BASE_DIR, 'auth.json')
COOKIES_TXT_PATH = os.path.join(BASE_DIR, 'cookies.txt')

app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(BASE_DIR, 'downloader.db')}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)

class DownloadTask(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(500), nullable=False)
    folder_name = db.Column(db.String(200), nullable=False)
    mode = db.Column(db.String(50), default='single')
    preview_img = db.Column(db.String(200), nullable=True)
    status = db.Column(db.String(100), default='Pending')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

with app.app_context():
    os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)
    os.makedirs(PREVIEW_FOLDER, exist_ok=True)
    db.create_all()

def export_cookies_for_singlefile():
    if not os.path.exists(AUTH_STATE_PATH):
        return None
    try:
        with open(AUTH_STATE_PATH, 'r', encoding='utf-8') as f:
            state = json.load(f)
        cookies = state.get('cookies', [])
        if not cookies:
            return None

        lines = ["# Netscape HTTP Cookie File"]
        for c in cookies:
            domain = c.get('domain', '')
            flag = "TRUE" if domain.startswith('.') else "FALSE"
            path = c.get('path', '/')
            secure = "TRUE" if c.get('secure', False) else "FALSE"
            expires = int(c.get('expires', 0))
            if expires < 0:
                expires = 0
            lines.append(f"{domain}\t{flag}\t{path}\t{secure}\t{expires}\t{c.get('name', '')}\t{c.get('value', '')}")

        with open(COOKIES_TXT_PATH, 'w', encoding='utf-8') as f:
            f.write("\n".join(lines) + "\n")
        return COOKIES_TXT_PATH
    except Exception:
        return None

def find_singlefile_executable():
    for candidate in [
        shutil.which("single-file"),
        "/usr/bin/single-file",
        "/usr/local/bin/single-file",
        os.path.expanduser("~/.npm-global/bin/single-file"),
    ]:
        if candidate and os.path.exists(candidate):
            return candidate
    return "single-file"

def find_chromium_executable():
    if os.path.exists("/usr/local/bin/chromium"):
        return "/usr/local/bin/chromium"
    pw_paths = glob.glob('/home/*/.cache/ms-playwright/chromium-*/chrome-linux*/chrome')
    if pw_paths:
        return pw_paths[0]
    root_pw_paths = glob.glob('/root/.cache/ms-playwright/chromium-*/chrome-linux*/chrome')
    if root_pw_paths:
        return root_pw_paths[0]
    for sys_path in ['/usr/bin/chromium', '/usr/bin/chromium-browser', '/usr/bin/google-chrome']:
        if os.path.exists(sys_path):
            return sys_path
    return None

def get_root_domain(netloc):
    host = netloc.lower().split(':')[0]
    parts = host.split('.')
    if len(parts) >= 2:
        if parts[-2] in ('co', 'or', 'ac', 'go', 'web', 'my') and len(parts) >= 3:
            return '.'.join(parts[-3:])
        return '.'.join(parts[-2:])
    return host

def normalize_url(url):
    """Menyamakan bentuk URL (hilangkan tracking query, samakan www/non-www & trailing slash)"""
    parsed = urlparse(url.strip())
    netloc = parsed.netloc.lower()
    if netloc.startswith('www.'):
        netloc = netloc[4:]
    path = parsed.path.rstrip('/')

    # Filter parameter tracking agar tidak menduplikasi halaman yang sama
    ignored_params = {'utm_source', 'utm_medium', 'utm_campaign', 'utm_term', 'utm_content', 'fbclid', 'gclid', 'ref'}
    qs = parse_qs(parsed.query)
    filtered_qs = {k: v for k, v in sorted(qs.items()) if k.lower() not in ignored_params}
    query_str = urlencode(filtered_qs, doseq=True)

    return urlunparse(('https', netloc, path, '', query_str, ''))

def url_to_html_filename(url, root_url):
    if normalize_url(url) == normalize_url(root_url):
        return "index.html"
    parsed = urlparse(url)
    host_prefix = parsed.netloc.replace('www.', '').replace('.', '_')
    path = parsed.path.strip('/')
    url_hash = hashlib.md5(normalize_url(url).encode('utf-8')).hexdigest()[:8]
    clean_slug = re.sub(r'[^a-zA-Z0-9_-]', '_', f"{host_prefix}_{path}")[:45] or "page"
    return f"{clean_slug}_{url_hash}.html"

def is_valid_domain_or_subdomain(link, root_domain):
    try:
        parsed = urlparse(link)
        if parsed.scheme not in ('http', 'https') or not parsed.netloc:
            return False
        if get_root_domain(parsed.netloc) != root_domain:
            return False
        excluded_keywords = ['logout', 'signout', 'keluar', 'cart', 'checkout', 'whatsapp', 'mailto:', 'tel:']
        excluded_exts = ('.pdf', '.zip', '.rar', '.jpg', '.jpeg', '.png', '.gif', '.svg', '.mp4', '.mp3', '.exe', '.webp', '.ico', '.css', '.js', '.json', '.xml')
        lower_link = link.lower()
        if any(k in lower_link for k in excluded_keywords) or parsed.path.lower().endswith(excluded_exts):
            return False
        return True
    except Exception:
        return False

async def auto_scroll_and_expand(page):
    """Menggulir halaman dari atas ke bawah & membuka menu dropdown agar seluruh link lazy-load muncul"""
    try:
        await page.evaluate("""async () => {
            // 1. Hapus overlay/modal yang menghalangi
            const selectors = [
                '.modal', '.modal-backdrop', '.fade.show',
                '[class*="modal"]', '[class*="backdrop"]', '[class*="popup"]', '[class*="overlay"]'
            ];
            selectors.forEach(sel => {
                document.querySelectorAll(sel).forEach(el => el.remove());
            });
            document.body.style.overflow = 'auto';
            document.body.style.position = 'static';
            document.documentElement.style.overflow = 'auto';

            // 2. Picu hover pada menu navigasi & dropdown
            document.querySelectorAll('nav li, .menu-item, .dropdown, [class*="category"], [class*="nav"] a').forEach(el => {
                el.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
                el.dispatchEvent(new MouseEvent('mouseenter', { bubbles: true }));
            });

            // 3. Auto-scroll bertahap ke bawah halaman untuk memicu lazy-load
            const totalHeight = Math.min(document.body.scrollHeight || 3000, 8000);
            const step = 600;
            for (let y = 0; y <= totalHeight; y += step) {
                window.scrollTo(0, y);
                await new Promise(r => setTimeout(r, 150));
            }
            window.scrollTo(0, 0);
        }""")
        await asyncio.sleep(0.8)
    except Exception:
        pass

async def prepare_page_for_discovery(page):
    try:
        for target_text in ["Kota Semarang", "Semarang", "Tutup", "Close", "Setuju", "Accept"]:
            btn = page.get_by_text(target_text).first
            if await btn.is_visible():
                await btn.click(timeout=1200)
                await asyncio.sleep(0.8)
                break
    except Exception:
        pass

    await page.wait_for_load_state('domcontentloaded')
    await auto_scroll_and_expand(page)

async def extract_all_links(page, enable_button_probe=True):
    """Mengekstrak seluruh tautan dari DOM (<a>, onclick, data-href, routerlink) + probing tombol SPA"""
    urls = await page.evaluate("""() => {
        const found = new Set();

        // 1. Semua elemen <a href>
        document.querySelectorAll('a[href]').forEach(a => {
            if (a.href && !a.href.startsWith('javascript:') && !a.href.startsWith('mailto:') && !a.href.startsWith('tel:')) {
                found.add(a.href);
            }
        });

        // 2. Atribut SPA & custom data attributes
        document.querySelectorAll('[data-href], [data-url], [data-link], [routerlink], [ng-reflect-router-link]').forEach(el => {
            const val = el.getAttribute('data-href') || el.getAttribute('data-url') || el.getAttribute('data-link') || el.getAttribute('routerlink') || el.getAttribute('ng-reflect-router-link');
            if (val) {
                try { found.add(new URL(val, window.location.origin).href); } catch (e) {}
            }
        });

        // 3. Pola URL di dalam atribut onclick (misal: location.href='/produk/detail')
        document.querySelectorAll('[onclick]').forEach(el => {
            const code = el.getAttribute('onclick') || '';
            const matches = code.match(/['"]((?:https?:\\/\\/|\\/)[^'"]+)['"]/g);
            if (matches) {
                matches.forEach(m => {
                    const clean = m.slice(1, -1);
                    try { found.add(new URL(clean, window.location.origin).href); } catch (e) {}
                });
            }
        });

        return Array.from(found);
    }""")

    discovered_set = set(urls)
    base_url = page.url

    # 4. Probing tombol SPA (hanya pada depth awal agar tetap cepat namun menembus gerbang SPA)
    if enable_button_probe:
        try:
            buttons = await page.locator("button, [role='button'], a.btn").all()
            for idx in range(min(len(buttons), 12)):
                try:
                    if page.url != base_url:
                        await page.goto(base_url, wait_until='domcontentloaded', timeout=25000)
                        await asyncio.sleep(1.5)
                    btn = page.locator("button, [role='button'], a.btn").nth(idx)
                    if await btn.is_visible():
                        btn_text = (await btn.inner_text()).strip().lower()
                        if any(skip in btn_text for skip in ['batal', 'cancel', 'tutup', 'close', 'logout', 'keluar', 'hapus', 'delete', 'id', 'en']):
                            continue
                        await btn.click(timeout=2500)
                        await asyncio.sleep(2.5)
                        if page.url != base_url:
                            discovered_set.add(page.url)
                            sub_urls = await page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)")
                            discovered_set.update(sub_urls)
                except Exception:
                    continue
        except Exception:
            pass

    return list(discovered_set)

async def bfs_discover_links(page, start_url, max_depth=4, max_pages=100, rate_limit_sec=1.2, task_id=None):
    """Deep BFS Link Discovery dengan prioritas menu navigasi & pengurutan kedalaman"""
    root_domain = get_root_domain(urlparse(start_url).netloc)
    clean_start = normalize_url(start_url)

    queue = deque([(start_url.split('#')[0], 0)])
    visited = {clean_start}
    discovered = [start_url.split('#')[0]]

    while queue and len(discovered) < max_pages:
        current_url, depth = queue.popleft()

        if task_id:
            with app.app_context():
                t = db.session.get(DownloadTask, task_id)
                if t:
                    t.status = f'Downloading: Deep Crawl (Depth {depth}/{max_depth} | {len(discovered)}/{max_pages} hal)...'
                    db.session.commit()

        if depth < max_depth:
            try:
                await page.goto(current_url, wait_until='domcontentloaded', timeout=35000)
                await asyncio.sleep(max(rate_limit_sec, 1.8))
                await prepare_page_for_discovery(page)

                # Aktifkan probing tombol SPA pada depth 0 & 1
                links = await extract_all_links(page, enable_button_probe=(depth <= 1))

                # Urutkan link: path yang lebih pendek (kategori/menu utama) diprioritaskan lebih dulu
                links.sort(key=lambda u: len(urlparse(u).path))

                for raw_link in links:
                    raw_clean = raw_link.split('#')[0]
                    norm_link = normalize_url(raw_clean)
                    if norm_link not in visited and is_valid_domain_or_subdomain(raw_clean, root_domain):
                        visited.add(norm_link)
                        discovered.append(raw_clean)
                        queue.append((raw_clean, depth + 1))
                        if len(discovered) >= max_pages:
                            break
            except Exception:
                continue

    return discovered

async def run_singlefile_cli(target_url, output_html_path, chrome_path, cookies_file=None):
    sf_bin = find_singlefile_executable()
    cmd = [
        sf_bin,
        target_url,
        output_html_path,
        "--browser-headless=true",
        "--browser-wait-until=commit",
        "--browser-wait-delay=3500",
        "--load-deferred-images=true",
        "--load-deferred-images-max-idle-time=1500",
        '--browser-args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]'
    ]
    if chrome_path:
        cmd.append(f"--browser-executable-path={chrome_path}")
    if cookies_file and os.path.exists(cookies_file):
        cmd.append(f"--browser-cookies-file={cookies_file}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if not os.path.exists(output_html_path):
        print(f"[SingleFile STDOUT] {stdout.decode('utf-8', errors='ignore')[:300]}")
        print(f"[SingleFile STDERR] {stderr.decode('utf-8', errors='ignore')[:300]}")
        return False
    return True

def rewrite_html_links(html_path, current_url, norm_url_map):
    """Mengubah seluruh tautan lokal agar klik antar-halaman terasa seperti situs asli"""
    if not os.path.exists(html_path):
        return
    with open(html_path, "r", encoding="utf-8", errors="ignore") as f:
        soup = BeautifulSoup(f.read(), "html.parser")

    # Bersihkan overlay/popup penghalang
    for el in soup.select('.modal, .modal-backdrop, .fade.show, [class*="modal"], [class*="backdrop"], [class*="popup"], [class*="overlay"]'):
        el.decompose()

    if soup.body:
        soup.body["style"] = "overflow: auto !important; position: static !important;"

    # Hapus <base href="..."> jika disisipkan SingleFile agar path relatif lokal tidak rusak
    for base_tag in soup.find_all("base"):
        base_tag.decompose()

    # Tulis ulang seluruh tag <a href="...">
    for a in soup.find_all("a", href=True):
        raw_href = a["href"].strip()
        if raw_href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue

        abs_url = urljoin(current_url, raw_href)
        fragment = f"#{urlparse(abs_url).fragment}" if urlparse(abs_url).fragment else ""
        clean_abs = abs_url.split("#")[0]
        norm_href = normalize_url(clean_abs)

        if norm_href in norm_url_map:
            a["href"] = f"{norm_url_map[norm_href]}{fragment}"
            if a.has_attr("target"):
                del a["target"]
        else:
            a["href"] = "javascript:void(0)"
            a["title"] = f"Halaman tidak termasuk dalam batas arsip ({clean_abs})"

    # Tulis ulang atribut onclick yang mengarah ke URL lokal
    for el in soup.find_all(attrs={"onclick": True}):
        onclick_val = el["onclick"]
        matches = re.findall(r"""['"]((?:https?://|/)[^'"]+)['"]""", onclick_val)
        for m in matches:
            abs_u = urljoin(current_url, m).split("#")[0]
            norm_u = normalize_url(abs_u)
            if norm_u in norm_url_map:
                onclick_val = onclick_val.replace(m, norm_url_map[norm_u])
        el["onclick"] = onclick_val

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(str(soup))

async def download_site_process(task_id, target_url, output_dir, preview_filename, mode, max_depth=4, max_pages=100, rate_limit=1.2):
    with app.app_context():
        task = db.session.get(DownloadTask, task_id)
        if not task:
            return

        has_auth = os.path.exists(AUTH_STATE_PATH)
        task.status = f"Downloading: Menyiapkan Engine ({'Auth Session Aktif' if has_auth else 'Guest'})..."
        db.session.commit()

        try:
            os.makedirs(output_dir, exist_ok=True)
            clean_root = target_url.split('#')[0]
            chrome_path = find_chromium_executable()
            cookies_file = export_cookies_for_singlefile()

            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context_kwargs = {
                    'viewport': {'width': 1920, 'height': 1080},
                    'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
                }
                if has_auth:
                    context_kwargs['storage_state'] = AUTH_STATE_PATH

                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

                await page.goto(clean_root, wait_until='domcontentloaded', timeout=60000)
                await asyncio.sleep(2.5)
                await prepare_page_for_discovery(page)

                preview_path = os.path.join(PREVIEW_FOLDER, preview_filename)
                await page.screenshot(path=preview_path, full_page=False)

                if mode == 'single':
                    await browser.close()
                    with app.app_context():
                        t = db.session.get(DownloadTask, task_id)
                        if t:
                            t.status = 'Downloading: SingleFile HTML Render...'
                            db.session.commit()

                    out_html = os.path.join(output_dir, "index.html")
                    success = await run_singlefile_cli(clean_root, out_html, chrome_path, cookies_file)
                    if success:
                        rewrite_html_links(out_html, clean_root, {normalize_url(clean_root): "index.html"})
                else:
                    pages_to_capture = await bfs_discover_links(
                        page, clean_root, max_depth=max_depth, max_pages=max_pages, rate_limit_sec=rate_limit, task_id=task_id
                    )
                    await browser.close()

                    norm_url_map = {normalize_url(u): url_to_html_filename(u, clean_root) for u in pages_to_capture}
                    total_pages = len(pages_to_capture)

                    for idx, current_url in enumerate(pages_to_capture, 1):
                        with app.app_context():
                            t = db.session.get(DownloadTask, task_id)
                            if t:
                                t.status = f'Downloading: Merender Halaman ({idx}/{total_pages})...'
                                db.session.commit()

                        filename = norm_url_map[normalize_url(current_url)]
                        out_html = os.path.join(output_dir, filename)

                        success = await run_singlefile_cli(current_url, out_html, chrome_path, cookies_file)
                        if success:
                            rewrite_html_links(out_html, current_url, norm_url_map)

                        await asyncio.sleep(rate_limit)

                saved_files = [f for f in os.listdir(output_dir) if f.endswith('.html')]
                if not saved_files:
                    task.status = 'Failed: SingleFile gagal membuat HTML (cek terminal)'
                else:
                    task.status = f'Completed ({len(saved_files)} Halaman)'
                    task.preview_img = preview_filename
                db.session.commit()

        except Exception as e:
            task.status = f'Failed: {str(e)[:45]}'
            db.session.commit()

def start_async_download(task_id, target_url, output_dir, preview_filename, mode, max_depth, max_pages, rate_limit):
    asyncio.run(download_site_process(task_id, target_url, output_dir, preview_filename, mode, max_depth, max_pages, rate_limit))

@app.route('/', methods=['GET', 'POST'])
def index():
    if request.method == 'POST':
        target_url = request.form.get('url')
        download_mode = request.form.get('download_mode', 'single')
        max_depth = int(request.form.get('max_depth', 4))
        max_pages = int(request.form.get('max_pages', 100))
        rate_limit = float(request.form.get('rate_limit', 1.2))

        if target_url:
            if not target_url.startswith('http://') and not target_url.startswith('https://'):
                target_url = 'https://' + target_url

            clean_name = target_url.split('://')[-1].replace('/', '_').replace(':', '_')
            time_stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            folder_name = f"{time_stamp}_{clean_name}"
            preview_filename = f"preview_{folder_name}.png"
            target_dir = os.path.join(DOWNLOAD_FOLDER, folder_name)

            new_task = DownloadTask(url=target_url, folder_name=folder_name, mode=download_mode, status='Pending')
            db.session.add(new_task)
            db.session.commit()

            import threading
            thread = threading.Thread(
                target=start_async_download,
                args=(new_task.id, target_url, target_dir, preview_filename, download_mode, max_depth, max_pages, rate_limit)
            )
            thread.start()

            return redirect(url_for('index'))

    tasks = DownloadTask.query.order_by(DownloadTask.created_at.desc()).all()
    return render_template('index.html', tasks=tasks)

@app.route('/view/<path:folder_name>/')
@app.route('/view/<path:folder_name>/<path:filename>')
def view_site(folder_name, filename='index.html'):
    site_path = os.path.join(DOWNLOAD_FOLDER, folder_name)
    if os.path.exists(os.path.join(site_path, filename)):
        return send_from_directory(site_path, filename)
    if filename == 'index.html' and os.path.exists(os.path.join(site_path, 'index.mht')):
        return send_from_directory(site_path, 'index.mht')
    return f"Halaman '{filename}' belum tersimpan dalam paket offline.", 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=1000, debug=False)
