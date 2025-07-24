#!/usr/bin/env python3
import gi
gi.require_version('Gtk', '3.0')
gi.require_version('GdkPixbuf', '2.0')
from gi.repository import Gtk, GdkPixbuf, GLib

import threading
import os, re, json, datetime, feedparser, requests, email.utils, webbrowser

# --- KONFIGURACJA RSS ---
RSS_URLS = {
    'x264/1080p': 'https://electro-torrent.pl/rss.php?cat=770',
    'x265/2160p': 'https://electro-torrent.pl/rss.php?cat=1160',
    'x265/1080p': 'https://electro-torrent.pl/rss.php?cat=1116',
    'Seriale':    'https://electro-torrent.pl/rss.php?cat=7',
}
CACHE_FILE = 'cache.json'

# Regex-y
TITLE_RE   = re.compile(r'^(?P<title>.+?)\s*\((?P<year>\d{4})\)')
QUALITY_RE = re.compile(r'\b(2160p|1080p|720p)\b', re.I)
LEKTOR_RE  = re.compile(r'(Lektor\s*[^\]/&\s]+(?:\s*AI|\s*\(AI\))?)', re.I)
NAPISY_RE  = re.compile(r'(Napisy\s*[^\]/&\s]+(?:\s*AI|\s*\(AI\))?)', re.I)
DUBBING_RE = re.compile(r'(Dubbing\s*[^\]/&\s]+)', re.I)

def fetch_items(days=7):
    cutoff = datetime.datetime.now() - datetime.timedelta(days=days)
    out = []
    for cat, url in RSS_URLS.items():
        feed = feedparser.parse(url)
        for e in feed.entries:
            try:
                pub = (datetime.datetime(*e.published_parsed[:6])
                       if e.published_parsed
                       else email.utils.parsedate_to_datetime(e.published))
            except:
                continue
            if pub < cutoff:
                continue

            txt = e.title or ""
            m = TITLE_RE.search(txt)
            if not m or m.group('year') not in ('2025','2024'):
                continue

            title, year = m.group('title').strip(), m.group('year')
            q  = QUALITY_RE.search(txt)
            lm = LEKTOR_RE.search(txt)
            nm = NAPISY_RE.search(txt)
            dm = DUBBING_RE.search(txt)

            itm = {
                'category': cat,
                'title':    title,
                'year':     year,
                'quality':  q.group(1) if q else '',
                'lektor':   (lm.group(1).strip('[]') if lm else
                             'Film Polski' if 'Film Polski' in txt else 'Nie'),
                'napisy':   nm.group(1).strip('[]') if nm else 'Nie',
                'dubbing':  dm.group(1).strip('[]') if dm else 'Nie',
                'thumb':    e.media_thumbnail[0]['url'] if 'media_thumbnail' in e else '',
                'link':     e.link,
                'pubDate':  pub,
                'season':   '',
                'episode':  ''
            }

            if cat.lower() == 'seriale':
                low = txt.lower()
                s = ep = None

                m1 = re.search(r's\s*(\d{1,2})\s*e\s*(\d{1,2})', low)
                if m1:
                    s, ep = int(m1.group(1)), int(m1.group(2))
                else:
                    ms = re.search(r'\[s\s*(\d{1,2})\]', low) or \
                         re.search(r'sezon\s*(\d{1,2})', low)
                    if ms:
                        s = int(ms.group(1))
                    mr = re.search(r'\[e\s*(\d{1,2})\s*[-–]\s*(\d{1,2})\]', low)
                    if mr:
                        ep = f"{int(mr.group(1))}-{int(mr.group(2))}"
                    else:
                        me = re.search(r'e\s*(\d{1,2})', low)
                        if me:
                            ep = int(me.group(1))

                if s is not None:
                    itm['season'] = str(s)
                if ep is not None:
                    itm['episode'] = str(ep)

            out.append(itm)

    return sorted(out, key=lambda x: x['pubDate'], reverse=True)

def load_cache():
    if not os.path.exists(CACHE_FILE):
        return []
    try:
        with open(CACHE_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for d in data:
            d['pubDate'] = datetime.datetime.fromisoformat(d['pubDate'])
        return data
    except:
        return []

def save_cache(items):
    to_save = []
    for itm in items:
        d = itm.copy()
        d['pubDate'] = d['pubDate'].isoformat()
        to_save.append(d)
    with open(CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(to_save, f, ensure_ascii=False, indent=2)

class TorrentWindow(Gtk.Window):
    def __init__(self):
        super().__init__(title="Electro-Torrent.pl RSS - Najnowsze filmy i seriale")
        self.set_default_size(800,600)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.add(vbox)

        # górny panel: okres + spinner + przyciski
        cfg = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vbox.pack_start(cfg, False, False, 0)

        cfg.pack_start(Gtk.Label(label="Okres (dni):"), False, False, 0)
        self.days = Gtk.ComboBoxText()
        for d in ("7","14"):
            self.days.append_text(d)
        self.days.set_active(0)
        cfg.pack_start(self.days, False, False, 0)

        # spinner ładowania
        self.spinner = Gtk.Spinner()
        cfg.pack_start(self.spinner, False, False, 0)

        btn_clean = Gtk.Button(label="Wyczyść")
        btn_clean.connect("clicked", self.on_clean)
        cfg.pack_start(btn_clean, False, False, 0)

        btn_refresh = Gtk.Button(label="Odśwież")
        btn_refresh.connect("clicked", self.on_refresh)
        cfg.pack_start(btn_refresh, False, False, 0)

        # stos stron
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(200)
        vbox.pack_start(self.stack, True, True, 0)

        # nawigacja + wskaźnik
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        vbox.pack_start(nav, False, False, 0)
        btn_prev = Gtk.Button(label="« Poprzednia")
        btn_prev.connect("clicked", lambda w: self.page(-1))
        nav.pack_start(btn_prev, False, False, 0)
        self.page_label = Gtk.Label(label="0/0")
        nav.pack_start(self.page_label, True, True, 0)
        btn_next = Gtk.Button(label="Następna »")
        btn_next.connect("clicked", lambda w: self.page(+1))
        nav.pack_start(btn_next, False, False, 0)

        # wczytaj cache
        self.items = load_cache() if os.path.exists(CACHE_FILE) else []
        self.build_pages()

    def on_clean(self, w):
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
        self.items = []
        self.build_pages()

    def on_refresh(self, w):
        # blokuj przycisk i start spinnera
        w.set_sensitive(False)
        self.spinner.start()
        days = int(self.days.get_active_text())
        threading.Thread(
            target=self._refresh_thread,
            args=(days, w),
            daemon=True
        ).start()

    def _refresh_thread(self, days, button):
        if os.path.exists(CACHE_FILE):
            os.remove(CACHE_FILE)
        items = fetch_items(days)
        save_cache(items)
        GLib.idle_add(self._on_refresh_done, items, button)

    def _on_refresh_done(self, items, button):
        self.items = items
        self.build_pages()
        button.set_sensitive(True)
        self.spinner.stop()
        return False

    def build_pages(self):
        for c in self.stack.get_children():
            self.stack.remove(c)
        self.pages = []

        if not self.items:
            lbl = Gtk.Label(label=f"Brak wyników w ostatnich {self.days.get_active_text()} dniach, kliknij przycisk Odśwież.")
            self.stack.add_named(lbl, "empty"); lbl.show()
            self.pages.append(lbl)
        else:
            for i in range(0, len(self.items), 20):
                p = self.make_page(self.items[i:i+20])
                name = f"page{i//20}"
                self.stack.add_named(p, name)
                p.show_all()
                self.pages.append(p)

        self.current_page = 0
        self.pages_count  = len(self.pages)
        self.page_label.set_text(f"{self.current_page+1}/{self.pages_count}")
        self.stack.set_visible_child(self.pages[0])

    def make_page(self, subset):
        sw = Gtk.ScrolledWindow()
        v  = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sw.add(v)

        for itm in subset:
            h = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

            # miniaturka
            if itm['thumb']:
                try:
                    data = requests.get(itm['thumb'], timeout=3).content
                    loader = GdkPixbuf.PixbufLoader()
                    loader.write(data); loader.close()
                    pb = loader.get_pixbuf().scale_simple(170,230,GdkPixbuf.InterpType.BILINEAR)
                    img = Gtk.Image.new_from_pixbuf(pb)
                    h.pack_start(img, False, False, 0)
                except:
                    pass

            esc = GLib.markup_escape_text
            title = esc(itm['title']); year = esc(itm['year'])
            quality= esc(itm['quality']); lekt = esc(itm['lektor'])
            napis = esc(itm['napisy']); dubb = esc(itm['dubbing'])
            cat = 'Seriale' if itm['category']=='Seriale' else 'Filmy'
            color = 'purple' if cat=='Seriale' else 'blue'

            lines = [
                f"<b>Tytuł: {title} ({year})</b>",
                f"<span foreground='{color}'><b>Kategoria: {cat}</b></span>",
                f"Jakość: {quality}",
            ]
            if itm['season']:
                s = f"Sezon: {esc(itm['season'])}"
                if itm['episode']:
                    s += f"  Odcinek: {esc(itm['episode'])}"
                lines.append(s)
            lines += [
                f"Lektor: {lekt}",
                f"Napisy: {napis}",
                f"Dubbing: {dubb}",
                f"Data: {itm['pubDate'].strftime('%Y-%m-%d %H:%M')}"
            ]

            lbl = Gtk.Label()
            lbl.set_xalign(0)
            lbl.set_line_wrap(True)
            lbl.set_use_markup(True)
            lbl.set_markup("\n".join(lines))
            h.pack_start(lbl, True, True, 0)

            if itm.get('link'):
                btn = Gtk.Button(label="Otwórz")
                btn.connect("clicked", lambda w, url=itm['link']: webbrowser.open(url))
                h.pack_start(btn, False, False, 0)

            v.pack_start(h, False, False, 0)

        return sw

    def page(self, delta):
        if not self.pages:
            return
        cur = self.stack.get_visible_child()
        try:
            idx = self.pages.index(cur) + delta
        except ValueError:
            idx = 0
        idx = max(0, min(idx, self.pages_count-1))
        self.current_page = idx
        self.stack.set_visible_child(self.pages[idx])
        self.page_label.set_text(f"{self.current_page+1}/{self.pages_count}")

if __name__ == "__main__":
    win = TorrentWindow()
    win.connect("destroy", Gtk.main_quit)
    win.show_all()
    Gtk.main()
