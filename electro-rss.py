#!/usr/bin/env python3

import os
import re
import json
import shutil
import hashlib
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import email.utils

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, GdkPixbuf, GLib, Gio, Gdk

import requests
import feedparser

# ---------------------- CONFIG ----------------------
APP_ID = "electro_rss"
RSS_URLS = {
    'x264/1080p': 'https://electro-torrent.pl/rss.php?cat=770',
    'x265/2160p': 'https://electro-torrent.pl/rss.php?cat=1160',
    'x265/1080p': 'https://electro-torrent.pl/rss.php?cat=1116',
    'Seriale':    'https://electro-torrent.pl/rss.php?cat=7',
}

CACHE_BASE = os.path.join(GLib.get_user_cache_dir(), APP_ID)
THUMB_DIR  = os.path.join(CACHE_BASE, 'thumbs')
CACHE_FILE = os.path.join(CACHE_BASE, 'items.json')
STATE_FILE = os.path.join(CACHE_BASE, 'state.json')
UI_STATE   = os.path.join(CACHE_BASE, 'ui.json')
for d in (CACHE_BASE, THUMB_DIR):
    os.makedirs(d, exist_ok=True)

CACHE_MAX_BYTES = 50 * 1024 * 1024
CACHE_MAX_FILES = 50
CACHE_MAX_AGE_DAYS = 20

HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": "ElectroRSS/1.0 (+Linux; GTK3)",
    "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
})
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    HTTP.mount("http://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.2)))
    HTTP.mount("https://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.2)))
except Exception:
    pass
DEFAULT_TIMEOUT = 6

TITLE_RE   = re.compile(r'^(?P<title>.+?)\s*\((?P<year>\d{4})\)')
QUALITY_RE = re.compile(r'\b(2160p|1080p|720p)\b', re.I)
LEKTOR_RE  = re.compile(r'(Lektor\s*[^\]/&\s]+(?:\s*AI|\s*\(AI\))?)', re.I)
NAPISY_RE  = re.compile(r'(Napisy\s*[^\]/&\s]+(?:\s*AI|\s*\(AI\))?)', re.I)
DUBBING_RE = re.compile(r'(Dubbing\s*[^\]/&\s]+)', re.I)

# ---------------------- Helpers ----------------------
def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode('utf-8', 'ignore')).hexdigest()

def thumb_path(url: str) -> str:
    return os.path.join(THUMB_DIR, _sha1(url) + '.img')

def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def live_thumb_hashes(items):
    hs = set()
    for it in items or []:
        u = it.get('thumb')
        if u:
            hs.add(_sha1(u))
    return hs

def cleanup_thumbs(max_bytes=CACHE_MAX_BYTES, max_files=CACHE_MAX_FILES, max_age_days=CACHE_MAX_AGE_DAYS, keep_hashes=None):
    try:
        entries = []
        now = datetime.datetime.now().timestamp()
        for fn in os.listdir(THUMB_DIR):
            fp = os.path.join(THUMB_DIR, fn)
            if not os.path.isfile(fp):
                continue
            base = os.path.splitext(fn)[0]
            try:
                st = os.stat(fp)
            except FileNotFoundError:
                continue
            age_days = (now - st.st_mtime) / 86400.0
            if max_age_days and age_days > max_age_days and (not keep_hashes or base not in keep_hashes):
                try:
                    os.remove(fp)
                except Exception:
                    pass
                continue
            entries.append((fp, st.st_mtime, st.st_size, base))

        total_size = sum(e[2] for e in entries)
        total_files = len(entries)
        if (max_bytes and total_size > max_bytes) or (max_files and total_files > max_files):
            entries.sort(key=lambda e: e[1])
            for fp, _, sz, base in entries:
                if keep_hashes and base in keep_hashes:
                    continue
                try:
                    os.remove(fp)
                    total_size -= sz
                    total_files -= 1
                except Exception:
                    pass
                if (not max_bytes or total_size <= max_bytes) and (not max_files or total_files <= max_files):
                    break
    except Exception:
        pass

def cache_stats():
    files = 0
    size = 0
    for root, _, fnames in os.walk(CACHE_BASE):
        for fn in fnames:
            fp = os.path.join(root, fn)
            try:
                st = os.stat(fp)
                size += st.st_size
                files += 1
            except FileNotFoundError:
                pass
    return files, size

def human_bytes(n):
    for unit in ('B','KB','MB','GB','TB'):
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0

# ---------------------- Data layer ----------------------
def _fetch_feed_bytes(url, meta):
    headers = {}
    if 'etag' in meta: headers['If-None-Match'] = meta['etag']
    if 'modified' in meta: headers['If-Modified-Since'] = meta['modified']
    r = HTTP.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
    if r.status_code == 304:
        return None, meta
    r.raise_for_status()
    new_meta = {}
    if 'ETag' in r.headers: new_meta['etag'] = r.headers['ETag']
    if 'Last-Modified' in r.headers: new_meta['modified'] = r.headers['Last-Modified']
    return r.content, (new_meta or meta)

def parse_entries(cat: str, feed, cutoff: datetime.datetime):
    out = []
    for e in getattr(feed, 'entries', []) or []:
        try:
            if e.get('published_parsed'):
                pub = datetime.datetime(*e.published_parsed[:6])
            else:
                pub = email.utils.parsedate_to_datetime(e.get('published', ''))
        except Exception:
            continue
        if not pub or pub < cutoff:
            continue

        txt = e.title or ""
        m = TITLE_RE.search(txt)
        if not m or m.group('year') not in ('2025', '2024'):
            continue

        title, year = m.group('title').strip(), m.group('year')
        q  = QUALITY_RE.search(txt)
        lm = LEKTOR_RE.search(txt)
        nm = NAPISY_RE.search(txt)
        dm = DUBBING_RE.search(txt)

        thumb = ''
        try:
            mt = e.get('media_thumbnail') or e.get('media_content')
            if isinstance(mt, list) and mt:
                thumb = mt[0].get('url') or ''
        except Exception:
            pass

        itm = {
            'category': cat,
            'title':    title,
            'year':     year,
            'quality':  q.group(1) if q else '',
            'lektor':   (lm.group(1).strip('[]') if lm else ('Film Polski' if 'film polski' in txt.lower() else 'Nie')),
            'napisy':   nm.group(1).strip('[]') if nm else 'Nie',
            'dubbing':  dm.group(1).strip('[]') if dm else 'Nie',
            'thumb':    thumb,
            'link':     e.link,
            'pubDate':  pub.isoformat(),
            'season':   '',
            'episode':  ''
        }

        if cat.lower() == 'seriale':
            low = txt.lower()
            s = ep = None
            m1 = re.search(r'\bs\s*(\d{1,2})\s*e\s*(\d{1,3})\b', low)
            if m1:
                s, ep = int(m1.group(1)), int(m1.group(2))
            else:
                ms = re.search(r'\bsezon\s*(\d{1,2})\b', low) or re.search(r'\bs\s*(\d{1,2})\b', low)
                if ms:
                    s = int(ms.group(1))
                mr = re.search(r'\be\s*(\d{1,3})\s*[-–]\s*(\d{1,3})\b', low)
                if mr:
                    ep = f"{int(mr.group(1))}-{int(mr.group(2))}"
                else:
                    me = re.search(r'\be\s*(\d{1,3})\b', low)
                    if me:
                        ep = int(me.group(1))
            if s is not None: itm['season'] = str(s)
            if ep is not None: itm['episode'] = str(ep)

        out.append(itm)
    return out

def fetch_items(days=7):
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    state = load_json(STATE_FILE, {})  
    prev_items = load_json(CACHE_FILE, [])

    def _one(cat, url):
        meta = state.get(url, {})
        try:
            body, new_meta = _fetch_feed_bytes(url, meta)
        except Exception:
            items = [it for it in prev_items
                     if it.get('category') == cat and
                        datetime.datetime.fromisoformat(it['pubDate']) >= cutoff]
            return cat, items, meta
        state[url] = new_meta
        if body is None:
            items = [it for it in prev_items
                     if it.get('category') == cat and
                        datetime.datetime.fromisoformat(it['pubDate']) >= cutoff]
            return cat, items, new_meta
        feed = feedparser.parse(body)
        return cat, parse_entries(cat, feed, cutoff), new_meta

    results = []
    with ThreadPoolExecutor(max_workers=min(4, len(RSS_URLS))) as ex:
        futs = [ex.submit(_one, cat, url) for cat, url in RSS_URLS.items()]
        for f in as_completed(futs):
            try:
                _cat, items, _meta = f.result()
                results.extend(items)
            except Exception:
                pass

    save_json(STATE_FILE, state)
    results.sort(key=lambda x: x['pubDate'], reverse=True)
    return results

# ---------------------- UI ----------------------
class TorrentWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Electro-Torrent.pl RSS")
        self.set_default_size(900, 650)

        self.thumb_pool = ThreadPoolExecutor(max_workers=6)
        self.ui_lock = threading.Lock()

        self.items = load_json(CACHE_FILE, [])
        try:
            cleanup_thumbs(keep_hashes=live_thumb_hashes(self.items))
        except Exception:
            pass

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add(vbox)

        cfg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vbox.pack_start(cfg, False, False, 6)

        cfg.pack_start(Gtk.Label(label="Okres (dni):"), False, False, 0)

        self.days = Gtk.ComboBoxText()
        for d in ("3", "7", "14", "30"):
            self.days.append_text(d)
        self.days.set_active(1)
        cfg.pack_start(self.days, False, False, 0)

        self.search = Gtk.Entry()
        self.search.set_placeholder_text("Filtr tytułu…")
        self.search.connect("changed", lambda e: self.apply_simple_filter(e.get_text()))
        cfg.pack_start(self.search, True, True, 0)

        self.spinner = Gtk.Spinner()
        cfg.pack_start(self.spinner, False, False, 0)

        self.cache_label = Gtk.Label(label="")
        cfg.pack_start(self.cache_label, False, False, 0)

        btn_clean = Gtk.Button(label="Wyczyść")
        btn_clean.connect("clicked", self.on_clean)
        cfg.pack_start(btn_clean, False, False, 0)

        self.btn_refresh = Gtk.Button(label="Odśwież")
        self.btn_refresh.connect("clicked", self.on_refresh)
        cfg.pack_start(self.btn_refresh, False, False, 0)

        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        self.btn_refresh.add_accelerator("clicked", accel, Gdk.KEY_F5, 0, Gtk.AccelFlags.VISIBLE)
        self.add_events(Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("key-press-event", self._on_key)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(150)
        vbox.pack_start(self.stack, True, True, 0)

        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vbox.pack_start(nav, False, False, 6)
        btn_prev = Gtk.Button(label="« Poprzednia")
        btn_prev.connect("clicked", lambda _, d=-1: self.page(d))
        nav.pack_start(btn_prev, False, False, 0)
        self.page_label = Gtk.Label(label="0/0")
        nav.pack_start(self.page_label, True, True, 0)
        btn_next = Gtk.Button(label="Następna »")
        btn_next.connect("clicked", lambda _, d=1: self.page(d))
        nav.pack_start(btn_next, False, False, 0)

        ui = self.load_ui()
        if ui.get('days') in ("3", "7", "14", "30"):
            self.days.set_active(("3", "7", "14", "30").index(ui['days']))
        if 'size' in ui:
            try:
                w, h = ui['size']
                self.resize(int(w), int(h))
            except Exception:
                pass

        self.update_cache_label()
        self.apply_simple_filter(self.search.get_text())

    # ---------- actions ----------
    def on_clean(self, _):
        for p in (CACHE_FILE, STATE_FILE):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        try:
            if os.path.isdir(THUMB_DIR):
                shutil.rmtree(THUMB_DIR)
            os.makedirs(THUMB_DIR, exist_ok=True)
        except Exception:
            pass

        self.items = []
        self.apply_simple_filter(self.search.get_text())
        self.update_cache_label()
        self.save_ui({'days': self.days.get_active_text(), 'size': self.get_size()})

    def on_refresh(self, button):
        button.set_sensitive(False)
        self.spinner.start()
        days = int(self.days.get_active_text())
        threading.Thread(target=self._refresh_thread, args=(days,), daemon=True).start()

    def _refresh_thread(self, days):
        try:
            items = fetch_items(days)
            save_json(CACHE_FILE, items)
        except Exception:
            items = []
        GLib.idle_add(self._on_refresh_done, items)

    def _on_refresh_done(self, items):
        self.items = items
        try:
            cleanup_thumbs(keep_hashes=live_thumb_hashes(self.items))
        except Exception:
            pass
        self.apply_simple_filter(self.search.get_text())
        self.btn_refresh.set_sensitive(True)
        self.spinner.stop()
        self.update_cache_label()
        self.save_ui({'days': self.days.get_active_text(), 'size': self.get_size()})
        return False

    # ---------- UI build / filter ----------
    def apply_simple_filter(self, q):
        q = (q or "").strip().lower()
        if not q:
            self.filtered = list(self.items)
        else:
            self.filtered = [it for it in self.items if q in it.get('title', '').lower()]
        self.build_pages_from(self.filtered)

    def build_pages_from(self, src):
        for c in self.stack.get_children():
            self.stack.remove(c)
        self.pages = []

        if not src:
            lbl = Gtk.Label(label="Brak wyników (filtr/okres).")
            self.stack.add_named(lbl, "empty")
            lbl.show()
            self.pages = [lbl]
        else:
            for itm in src:
                try:
                    itm['_pub_dt'] = datetime.datetime.fromisoformat(itm['pubDate'])
                except Exception:
                    itm['_pub_dt'] = None
            for i in range(0, len(src), 20):
                page = self.make_page(src[i:i+20])
                name = f"page{i//20}"
                self.stack.add_named(page, name)
                page.show_all()
                self.pages.append(page)

        self.current_page = 0
        self.pages_count = len(self.pages)
        self.page_label.set_text(f"{self.current_page+1}/{self.pages_count}")
        self.stack.set_visible_child(self.pages[0])

    def build_pages(self):
        self.build_pages_from(self.items)

    def make_page(self, subset):
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sw.add(box)

        for itm in subset:
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

            img = Gtk.Image.new_from_icon_name("image-x-generic", Gtk.IconSize.DIALOG)
            img.set_pixel_size(96)
            h.pack_start(img, False, False, 0)

            if itm.get('thumb'):
                self.load_thumb_async(itm['thumb'], img)

            esc = GLib.markup_escape_text
            color = 'purple' if itm['category'] == 'Seriale' else 'blue'

            lines = [
                f"<b>Tytuł: {esc(itm['title'])} ({esc(itm['year'])})</b>",
                f"<span foreground='{color}'><b>Kategoria: {esc(itm['category'])}</b></span>",
                f"Jakość: {esc(itm.get('quality',''))}",
            ]
            if itm.get('season'):
                s = f"Sezon: {esc(itm['season'])}"
                if itm.get('episode'):
                    s += f"  Odcinek: {esc(itm['episode'])}"
                lines.append(s)
            dt = itm.get('_pub_dt')
            lines += [
                f"Lektor: {esc(itm.get('lektor','Nie'))}",
                f"Napisy: {esc(itm.get('napisy','Nie'))}",
                f"Dubbing: {esc(itm.get('dubbing','Nie'))}",
                f"Data: {dt.strftime('%Y-%m-%d %H:%M') if dt else esc(itm.get('pubDate',''))}",
            ]

            lbl = Gtk.Label()
            lbl.set_xalign(0)
            lbl.set_line_wrap(True)
            lbl.set_use_markup(True)
            lbl.set_markup("\n".join(lines))
            h.pack_start(lbl, True, True, 0)

            if itm.get('link'):
                btn = Gtk.Button(label="Otwórz")
                btn.connect("clicked", lambda _, url=itm['link']: self.open_url(url))
                h.pack_start(btn, False, False, 0)

            box.pack_start(h, False, False, 0)

        return sw

    def page(self, delta):
        if not getattr(self, 'pages', None):
            return
        idx = max(0, min(getattr(self, 'current_page', 0) + delta, self.pages_count - 1))
        self.current_page = idx
        self.stack.set_visible_child(self.pages[idx])
        self.page_label.set_text(f"{idx+1}/{self.pages_count}")

    def load_thumb_async(self, url: str, img_widget: Gtk.Image):
        path = thumb_path(url)

        def work():
            if not os.path.exists(path):
                try:
                    r = HTTP.get(url, timeout=DEFAULT_TIMEOUT, stream=True)
                    r.raise_for_status()
                    with open(path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                except Exception:
                    return
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 170, 230, True)
            except Exception:
                return
            GLib.idle_add(img_widget.set_from_pixbuf, pb)

        self.thumb_pool.submit(work)

    # ---------- Misc ----------
    @staticmethod
    def open_url(url):
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception:
            pass

    def update_cache_label(self):
        files, size = cache_stats()
        self.cache_label.set_text(f"Cache: {files} plików, {human_bytes(size)}")

    def load_ui(self):
        return load_json(UI_STATE, {})

    def save_ui(self, data):
        cur = self.load_ui()
        cur.update(data or {})
        save_json(UI_STATE, cur)

    def _on_key(self, _w, event):
        if event.keyval == Gdk.KEY_Left:
            self.page(-1)
        elif event.keyval == Gdk.KEY_Right:
            self.page(1)

    def do_destroy(self):
        try:
            self.save_ui({'days': self.days.get_active_text(), 'size': self.get_size()})
        except Exception:
            pass
        try:
            self.thumb_pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self.thumb_pool.shutdown(wait=False)
        except Exception:
            pass
        super().do_destroy()

# ---------------------- main ----------------------
if __name__ == "__main__":
    win = TorrentWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import json
import shutil
import hashlib
import datetime
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import email.utils

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, GdkPixbuf, GLib, Gio, Gdk

import requests
import feedparser

# ---------------------- CONFIG ----------------------
APP_ID = "electro_rss"
RSS_URLS = {
    'x264/1080p': 'https://electro-torrent.pl/rss.php?cat=770',
    'x265/2160p': 'https://electro-torrent.pl/rss.php?cat=1160',
    'x265/1080p': 'https://electro-torrent.pl/rss.php?cat=1116',
    'Seriale':    'https://electro-torrent.pl/rss.php?cat=7',
}

CACHE_BASE = os.path.join(GLib.get_user_cache_dir(), APP_ID)
THUMB_DIR  = os.path.join(CACHE_BASE, 'thumbs')
CACHE_FILE = os.path.join(CACHE_BASE, 'items.json')
STATE_FILE = os.path.join(CACHE_BASE, 'state.json')
UI_STATE   = os.path.join(CACHE_BASE, 'ui.json')
for d in (CACHE_BASE, THUMB_DIR):
    os.makedirs(d, exist_ok=True)

CACHE_MAX_BYTES = 50 * 1024 * 1024
CACHE_MAX_FILES = 50
CACHE_MAX_AGE_DAYS = 20

HTTP = requests.Session()
HTTP.headers.update({
    "User-Agent": "ElectroRSS/1.0 (+Linux; GTK3)",
    "Accept": "application/rss+xml,application/xml;q=0.9,*/*;q=0.8",
})
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    HTTP.mount("http://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.2)))
    HTTP.mount("https://", HTTPAdapter(max_retries=Retry(total=2, backoff_factor=0.2)))
except Exception:
    pass
DEFAULT_TIMEOUT = 6

TITLE_RE   = re.compile(r'^(?P<title>.+?)\s*\((?P<year>\d{4})\)')
QUALITY_RE = re.compile(r'\b(2160p|1080p|720p)\b', re.I)
LEKTOR_RE  = re.compile(r'(Lektor\s*[^\]/&\s]+(?:\s*AI|\s*\(AI\))?)', re.I)
NAPISY_RE  = re.compile(r'(Napisy\s*[^\]/&\s]+(?:\s*AI|\s*\(AI\))?)', re.I)
DUBBING_RE = re.compile(r'(Dubbing\s*[^\]/&\s]+)', re.I)

# ---------------------- Helpers ----------------------
def _sha1(s: str) -> str:
    return hashlib.sha1(s.encode('utf-8', 'ignore')).hexdigest()

def thumb_path(url: str) -> str:
    return os.path.join(THUMB_DIR, _sha1(url) + '.img')

def load_json(path, default):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)

def live_thumb_hashes(items):
    hs = set()
    for it in items or []:
        u = it.get('thumb')
        if u:
            hs.add(_sha1(u))
    return hs

def cleanup_thumbs(max_bytes=CACHE_MAX_BYTES, max_files=CACHE_MAX_FILES, max_age_days=CACHE_MAX_AGE_DAYS, keep_hashes=None):
    try:
        entries = []
        now = datetime.datetime.now().timestamp()
        for fn in os.listdir(THUMB_DIR):
            fp = os.path.join(THUMB_DIR, fn)
            if not os.path.isfile(fp):
                continue
            base = os.path.splitext(fn)[0]
            try:
                st = os.stat(fp)
            except FileNotFoundError:
                continue
            age_days = (now - st.st_mtime) / 86400.0
            if max_age_days and age_days > max_age_days and (not keep_hashes or base not in keep_hashes):
                try:
                    os.remove(fp)
                except Exception:
                    pass
                continue
            entries.append((fp, st.st_mtime, st.st_size, base))

        total_size = sum(e[2] for e in entries)
        total_files = len(entries)
        if (max_bytes and total_size > max_bytes) or (max_files and total_files > max_files):
            entries.sort(key=lambda e: e[1])
            for fp, _, sz, base in entries:
                if keep_hashes and base in keep_hashes:
                    continue
                try:
                    os.remove(fp)
                    total_size -= sz
                    total_files -= 1
                except Exception:
                    pass
                if (not max_bytes or total_size <= max_bytes) and (not max_files or total_files <= max_files):
                    break
    except Exception:
        pass

def cache_stats():
    files = 0
    size = 0
    for root, _, fnames in os.walk(CACHE_BASE):
        for fn in fnames:
            fp = os.path.join(root, fn)
            try:
                st = os.stat(fp)
                size += st.st_size
                files += 1
            except FileNotFoundError:
                pass
    return files, size

def human_bytes(n):
    for unit in ('B','KB','MB','GB','TB'):
        if n < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0

# ---------------------- Data layer ----------------------
def _fetch_feed_bytes(url, meta):
    headers = {}
    if 'etag' in meta: headers['If-None-Match'] = meta['etag']
    if 'modified' in meta: headers['If-Modified-Since'] = meta['modified']
    r = HTTP.get(url, headers=headers, timeout=DEFAULT_TIMEOUT)
    if r.status_code == 304:
        return None, meta
    r.raise_for_status()
    new_meta = {}
    if 'ETag' in r.headers: new_meta['etag'] = r.headers['ETag']
    if 'Last-Modified' in r.headers: new_meta['modified'] = r.headers['Last-Modified']
    return r.content, (new_meta or meta)

def parse_entries(cat: str, feed, cutoff: datetime.datetime):
    out = []
    for e in getattr(feed, 'entries', []) or []:
        try:
            if e.get('published_parsed'):
                pub = datetime.datetime(*e.published_parsed[:6])
            else:
                pub = email.utils.parsedate_to_datetime(e.get('published', ''))
        except Exception:
            continue
        if not pub or pub < cutoff:
            continue

        txt = e.title or ""
        m = TITLE_RE.search(txt)
        if not m or m.group('year') not in ('2025', '2024'):
            continue

        title, year = m.group('title').strip(), m.group('year')
        q  = QUALITY_RE.search(txt)
        lm = LEKTOR_RE.search(txt)
        nm = NAPISY_RE.search(txt)
        dm = DUBBING_RE.search(txt)

        thumb = ''
        try:
            mt = e.get('media_thumbnail') or e.get('media_content')
            if isinstance(mt, list) and mt:
                thumb = mt[0].get('url') or ''
        except Exception:
            pass

        itm = {
            'category': cat,
            'title':    title,
            'year':     year,
            'quality':  q.group(1) if q else '',
            'lektor':   (lm.group(1).strip('[]') if lm else ('Film Polski' if 'film polski' in txt.lower() else 'Nie')),
            'napisy':   nm.group(1).strip('[]') if nm else 'Nie',
            'dubbing':  dm.group(1).strip('[]') if dm else 'Nie',
            'thumb':    thumb,
            'link':     e.link,
            'pubDate':  pub.isoformat(),
            'season':   '',
            'episode':  ''
        }

        if cat.lower() == 'seriale':
            low = txt.lower()
            s = ep = None
            m1 = re.search(r'\bs\s*(\d{1,2})\s*e\s*(\d{1,3})\b', low)
            if m1:
                s, ep = int(m1.group(1)), int(m1.group(2))
            else:
                ms = re.search(r'\bsezon\s*(\d{1,2})\b', low) or re.search(r'\bs\s*(\d{1,2})\b', low)
                if ms:
                    s = int(ms.group(1))
                mr = re.search(r'\be\s*(\d{1,3})\s*[-–]\s*(\d{1,3})\b', low)
                if mr:
                    ep = f"{int(mr.group(1))}-{int(mr.group(2))}"
                else:
                    me = re.search(r'\be\s*(\d{1,3})\b', low)
                    if me:
                        ep = int(me.group(1))
            if s is not None: itm['season'] = str(s)
            if ep is not None: itm['episode'] = str(ep)

        out.append(itm)
    return out

def fetch_items(days=7):
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    state = load_json(STATE_FILE, {})  
    prev_items = load_json(CACHE_FILE, [])

    def _one(cat, url):
        meta = state.get(url, {})
        try:
            body, new_meta = _fetch_feed_bytes(url, meta)
        except Exception:
            items = [it for it in prev_items
                     if it.get('category') == cat and
                        datetime.datetime.fromisoformat(it['pubDate']) >= cutoff]
            return cat, items, meta
        state[url] = new_meta
        if body is None:
            items = [it for it in prev_items
                     if it.get('category') == cat and
                        datetime.datetime.fromisoformat(it['pubDate']) >= cutoff]
            return cat, items, new_meta
        feed = feedparser.parse(body)
        return cat, parse_entries(cat, feed, cutoff), new_meta

    results = []
    with ThreadPoolExecutor(max_workers=min(4, len(RSS_URLS))) as ex:
        futs = [ex.submit(_one, cat, url) for cat, url in RSS_URLS.items()]
        for f in as_completed(futs):
            try:
                _cat, items, _meta = f.result()
                results.extend(items)
            except Exception:
                pass

    save_json(STATE_FILE, state)
    results.sort(key=lambda x: x['pubDate'], reverse=True)
    return results

# ---------------------- UI ----------------------
class TorrentWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Electro-Torrent.pl RSS")
        self.set_default_size(900, 650)

        self.thumb_pool = ThreadPoolExecutor(max_workers=6)
        self.ui_lock = threading.Lock()

        self.items = load_json(CACHE_FILE, [])
        try:
            cleanup_thumbs(keep_hashes=live_thumb_hashes(self.items))
        except Exception:
            pass

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add(vbox)

        cfg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vbox.pack_start(cfg, False, False, 6)

        cfg.pack_start(Gtk.Label(label="Okres (dni):"), False, False, 0)

        self.days = Gtk.ComboBoxText()
        for d in ("3", "7", "14", "30"):
            self.days.append_text(d)
        self.days.set_active(1)
        cfg.pack_start(self.days, False, False, 0)

        self.search = Gtk.Entry()
        self.search.set_placeholder_text("Filtr tytułu…")
        self.search.connect("changed", lambda e: self.apply_simple_filter(e.get_text()))
        cfg.pack_start(self.search, True, True, 0)

        self.spinner = Gtk.Spinner()
        cfg.pack_start(self.spinner, False, False, 0)

        self.cache_label = Gtk.Label(label="")
        cfg.pack_start(self.cache_label, False, False, 0)

        btn_clean = Gtk.Button(label="Wyczyść")
        btn_clean.connect("clicked", self.on_clean)
        cfg.pack_start(btn_clean, False, False, 0)

        self.btn_refresh = Gtk.Button(label="Odśwież")
        self.btn_refresh.connect("clicked", self.on_refresh)
        cfg.pack_start(self.btn_refresh, False, False, 0)

        accel = Gtk.AccelGroup()
        self.add_accel_group(accel)
        self.btn_refresh.add_accelerator("clicked", accel, Gdk.KEY_F5, 0, Gtk.AccelFlags.VISIBLE)
        self.add_events(Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("key-press-event", self._on_key)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(150)
        vbox.pack_start(self.stack, True, True, 0)

        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vbox.pack_start(nav, False, False, 6)
        btn_prev = Gtk.Button(label="« Poprzednia")
        btn_prev.connect("clicked", lambda _, d=-1: self.page(d))
        nav.pack_start(btn_prev, False, False, 0)
        self.page_label = Gtk.Label(label="0/0")
        nav.pack_start(self.page_label, True, True, 0)
        btn_next = Gtk.Button(label="Następna »")
        btn_next.connect("clicked", lambda _, d=1: self.page(d))
        nav.pack_start(btn_next, False, False, 0)

        ui = self.load_ui()
        if ui.get('days') in ("3", "7", "14", "30"):
            self.days.set_active(("3", "7", "14", "30").index(ui['days']))
        if 'size' in ui:
            try:
                w, h = ui['size']
                self.resize(int(w), int(h))
            except Exception:
                pass

        self.update_cache_label()
        self.apply_simple_filter(self.search.get_text())

    # ---------- actions ----------
    def on_clean(self, _):
        for p in (CACHE_FILE, STATE_FILE):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass
        try:
            if os.path.isdir(THUMB_DIR):
                shutil.rmtree(THUMB_DIR)
            os.makedirs(THUMB_DIR, exist_ok=True)
        except Exception:
            pass

        self.items = []
        self.apply_simple_filter(self.search.get_text())
        self.update_cache_label()
        self.save_ui({'days': self.days.get_active_text(), 'size': self.get_size()})

    def on_refresh(self, button):
        button.set_sensitive(False)
        self.spinner.start()
        days = int(self.days.get_active_text())
        threading.Thread(target=self._refresh_thread, args=(days,), daemon=True).start()

    def _refresh_thread(self, days):
        try:
            items = fetch_items(days)
            save_json(CACHE_FILE, items)
        except Exception:
            items = []
        GLib.idle_add(self._on_refresh_done, items)

    def _on_refresh_done(self, items):
        self.items = items
        try:
            cleanup_thumbs(keep_hashes=live_thumb_hashes(self.items))
        except Exception:
            pass
        self.apply_simple_filter(self.search.get_text())
        self.btn_refresh.set_sensitive(True)
        self.spinner.stop()
        self.update_cache_label()
        self.save_ui({'days': self.days.get_active_text(), 'size': self.get_size()})
        return False

    # ---------- UI build / filter ----------
    def apply_simple_filter(self, q):
        q = (q or "").strip().lower()
        if not q:
            self.filtered = list(self.items)
        else:
            self.filtered = [it for it in self.items if q in it.get('title', '').lower()]
        self.build_pages_from(self.filtered)

    def build_pages_from(self, src):
        for c in self.stack.get_children():
            self.stack.remove(c)
        self.pages = []

        if not src:
            lbl = Gtk.Label(label="Brak wyników (filtr/okres).")
            self.stack.add_named(lbl, "empty")
            lbl.show()
            self.pages = [lbl]
        else:
            for itm in src:
                try:
                    itm['_pub_dt'] = datetime.datetime.fromisoformat(itm['pubDate'])
                except Exception:
                    itm['_pub_dt'] = None
            for i in range(0, len(src), 20):
                page = self.make_page(src[i:i+20])
                name = f"page{i//20}"
                self.stack.add_named(page, name)
                page.show_all()
                self.pages.append(page)

        self.current_page = 0
        self.pages_count = len(self.pages)
        self.page_label.set_text(f"{self.current_page+1}/{self.pages_count}")
        self.stack.set_visible_child(self.pages[0])

    def build_pages(self):
        self.build_pages_from(self.items)

    def make_page(self, subset):
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sw.add(box)

        for itm in subset:
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

            img = Gtk.Image.new_from_icon_name("image-x-generic", Gtk.IconSize.DIALOG)
            img.set_pixel_size(96)
            h.pack_start(img, False, False, 0)

            if itm.get('thumb'):
                self.load_thumb_async(itm['thumb'], img)

            esc = GLib.markup_escape_text
            color = 'purple' if itm['category'] == 'Seriale' else 'blue'

            lines = [
                f"<b>Tytuł: {esc(itm['title'])} ({esc(itm['year'])})</b>",
                f"<span foreground='{color}'><b>Kategoria: {esc(itm['category'])}</b></span>",
                f"Jakość: {esc(itm.get('quality',''))}",
            ]
            if itm.get('season'):
                s = f"Sezon: {esc(itm['season'])}"
                if itm.get('episode'):
                    s += f"  Odcinek: {esc(itm['episode'])}"
                lines.append(s)
            dt = itm.get('_pub_dt')
            lines += [
                f"Lektor: {esc(itm.get('lektor','Nie'))}",
                f"Napisy: {esc(itm.get('napisy','Nie'))}",
                f"Dubbing: {esc(itm.get('dubbing','Nie'))}",
                f"Data: {dt.strftime('%Y-%m-%d %H:%M') if dt else esc(itm.get('pubDate',''))}",
            ]

            lbl = Gtk.Label()
            lbl.set_xalign(0)
            lbl.set_line_wrap(True)
            lbl.set_use_markup(True)
            lbl.set_markup("\n".join(lines))
            h.pack_start(lbl, True, True, 0)

            if itm.get('link'):
                btn = Gtk.Button(label="Otwórz")
                btn.connect("clicked", lambda _, url=itm['link']: self.open_url(url))
                h.pack_start(btn, False, False, 0)

            box.pack_start(h, False, False, 0)

        return sw

    def page(self, delta):
        if not getattr(self, 'pages', None):
            return
        idx = max(0, min(getattr(self, 'current_page', 0) + delta, self.pages_count - 1))
        self.current_page = idx
        self.stack.set_visible_child(self.pages[idx])
        self.page_label.set_text(f"{idx+1}/{self.pages_count}")

    def load_thumb_async(self, url: str, img_widget: Gtk.Image):
        path = thumb_path(url)

        def work():
            if not os.path.exists(path):
                try:
                    r = HTTP.get(url, timeout=DEFAULT_TIMEOUT, stream=True)
                    r.raise_for_status()
                    with open(path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                except Exception:
                    return
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 170, 230, True)
            except Exception:
                return
            GLib.idle_add(img_widget.set_from_pixbuf, pb)

        self.thumb_pool.submit(work)

    # ---------- Misc ----------
    @staticmethod
    def open_url(url):
        try:
            Gio.AppInfo.launch_default_for_uri(url, None)
        except Exception:
            pass

    def update_cache_label(self):
        files, size = cache_stats()
        self.cache_label.set_text(f"Cache: {files} plików, {human_bytes(size)}")

    def load_ui(self):
        return load_json(UI_STATE, {})

    def save_ui(self, data):
        cur = self.load_ui()
        cur.update(data or {})
        save_json(UI_STATE, cur)

    def _on_key(self, _w, event):
        if event.keyval == Gdk.KEY_Left:
            self.page(-1)
        elif event.keyval == Gdk.KEY_Right:
            self.page(1)

    def do_destroy(self):
        try:
            self.save_ui({'days': self.days.get_active_text(), 'size': self.get_size()})
        except Exception:
            pass
        try:
            self.thumb_pool.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            self.thumb_pool.shutdown(wait=False)
        except Exception:
            pass
        super().do_destroy()

# ---------------------- main ----------------------
if __name__ == "__main__":
    win = TorrentWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
