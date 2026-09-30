#!/usr/bin/env python3
"""On-page SEO-аудит: обход сайта, разбор HTML, снимок и diff прогонов.

Использование:
  python scripts/onpage_audit.py https://zavodsvay.ru --limit 200
  python scripts/onpage_audit.py https://site.ru --limit 200 --diff
  python scripts/onpage_audit.py https://site.ru --urls data/urls.txt --psi
  python scripts/onpage_audit.py --compare audits/onpage-A.json audits/onpage-B.json

URL-источники (по умолчанию): Sitemap из robots.txt → sitemap index → дочерние
sitemaps. Фолбэк, если sitemaps нет: BFS-обход с главной по внутренним ссылкам.

Zero dependencies: только стандартная библиотека.

Пишет audits/<host>/onpage-YYYY-MM-DD.json:
  site, robots, sitemaps, pages[] (метрики + issues), site_issues[], summary
Снимки разложены по хостам — сеть из нескольких доменов не перетирает друг друга.
"""
import argparse
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.abspath(__file__))
AUDIT_ROOT = os.path.join(ROOT, 'audits')

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')
TIMEOUT = 20
WORKERS = 4
DELAY = 0.7  # techdebt: фиксированная пауза, Crawl-delay из robots.txt не читается

PSI_API = 'https://www.googleapis.com/pagespeedonline/v5/runPagespeed'

TITLE_MAX = 70
DESC_MAX = 180
THIN_WORDS = 300
YANDEX_URL_MAX = 75

CWV_GOOD = {'lcp_ms': 2500, 'inp_ms': 200, 'fid_ms': 100, 'cls': 0.1}
CWV_NI = {'lcp_ms': 4000, 'inp_ms': 500, 'fid_ms': 300, 'cls': 0.25}

DEAD_SCHEMA = {'FAQPage', 'HowTo', 'SpecialAnnouncement', 'ClaimReview',
               'EstimatedSalary', 'LearningVideo', 'CourseInfo', 'EstimatedCost'}

SEO_SUFFIX = ('.pdf', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.svg', '.zip',
              '.doc', '.docx', '.xls', '.xlsx', '.mp4', '.xml', '.json')

# alt="" у счётчиков — норма, у контентных картинок alt обязателен
TRACKERS = ('mc.yandex.ru', 'google-analytics', 'googletagmanager', 'mc.webmaster',
            'top-fwz1.com', 'vk.com/rtrg', 'chatra.io')


def is_tracker(src: str) -> bool:
    return any(t in src for t in TRACKERS)


def norm_key(url: str) -> str:
    """Канонический ключ URL для сравнения: без схемы, host lowercase, без хвостового /."""
    p = urllib.parse.urlsplit(url)
    path = p.path or '/'
    if len(path) > 1:
        path = path.rstrip('/')
    return p.netloc.lower() + path


def count_words(text: str) -> int:
    return sum(1 for tok in text.split() if any(c.isalpha() for c in tok))


def same_site(url: str, host: str) -> bool:
    return urllib.parse.urlsplit(url).netloc.lower() == host


# --- fetch -----------------------------------------------------------------

def fetch(url: str, want_html: bool = True) -> dict:
    req = urllib.request.Request(url, headers={
        'User-Agent': UA,
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Accept-Encoding': 'gzip',
        'Accept-Language': 'ru-RU,ru;q=0.9',
    })
    out = {'url': url, 'final_url': url, 'status': 0, 'content_type': '',
           'bytes': 0, 'body': None, 'error': None, 'elapsed_ms': 0}
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read()
            if r.headers.get('Content-Encoding') == 'gzip':
                raw = gzip.decompress(raw)
            out['status'] = r.status
            out['final_url'] = r.geturl()
            out['content_type'] = (r.headers.get('Content-Type') or '').split(';')[0].strip()
            out['bytes'] = len(raw)
            if want_html:
                charset = r.headers.get_content_charset() or ''
                if not charset:
                    m = re.search(rb'charset=["\']?([\w-]+)', raw[:2048], re.I)
                    charset = m.group(1).decode('ascii', 'ignore') if m else ''
                out['body'] = raw.decode(charset or 'utf-8', 'replace')
    except urllib.error.HTTPError as e:
        out['status'] = e.code
        out['error'] = f'HTTP {e.code} {e.reason}'
    except Exception as e:
        out['error'] = f'{type(e).__name__}: {e}'
    out['elapsed_ms'] = int((time.perf_counter() - started) * 1000)
    return out


# --- robots / sitemap ------------------------------------------------------

def parse_robots(site: str) -> dict:
    res = fetch(site.rstrip('/') + '/robots.txt')
    info = {'status': res['status'], 'exists': res['status'] == 200,
            'sitemaps': [], 'disallow_all': False, 'crawl_delay': None,
            'raw_bytes': res['bytes']}
    if not info['exists'] or not res['body']:
        return info
    for line in res['body'].splitlines():
        line = line.strip()
        low = line.lower()
        if low.startswith('sitemap:'):
            url = line.split(':', 1)[1].strip()
            if url:
                info['sitemaps'].append(url)
        elif low.startswith('disallow:') and line.split(':', 1)[1].strip() == '/':
            info['disallow_all'] = True
        elif low.startswith('crawl-delay:'):
            try:
                info['crawl_delay'] = float(line.split(':', 1)[1].strip())
            except ValueError:
                pass
    return info


def sitemap_urls(url: str, depth: int = 0) -> tuple:
    """Возвращает (urls, errors). Раскрывает sitemap index (лимит 3 уровня)."""
    if depth > 3:
        return [], [f'{url}: вложенность sitemap > 3']
    res = fetch(url)
    if res['status'] != 200:
        return [], [f'{url}: HTTP {res["status"]}']
    try:
        root = ET.fromstring((res['body'] or '').encode('utf-8', 'replace'))
    except ET.ParseError as e:
        return [], [f'{url}: невалидный XML ({e})']

    tag = root.tag.split('}')[-1]
    urls, errors = [], []
    for el in root:
        child = el.tag.split('}')[-1]
        loc = next((c.text.strip() for c in el if c.tag.split('}')[-1] == 'loc' and c.text), '')
        if not loc:
            continue
        if tag == 'urlset' and child == 'url':
            urls.append(loc)
        elif tag == 'sitemapindex' and child == 'sitemap':
            sub, sub_err = sitemap_urls(loc, depth + 1)
            urls.extend(sub)
            errors.extend(sub_err)
    return urls, errors


def collect_sitemaps(site: str, robots: dict) -> dict:
    candidates = list(robots.get('sitemaps') or [])
    if not candidates:
        candidates = [site.rstrip('/') + '/sitemap.xml']
    urls, errors, files = [], [], []
    for sm in candidates[:10]:  # techdebt: лимит 10 sitemap-файлов на прогон
        sub, sub_err = sitemap_urls(sm)
        errors.extend(sub_err)
        files.append({'sitemap': sm, 'urls': len(sub), 'error': sub_err or None})
        urls.extend(sub)
    seen, unique = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)
    return {'files': files, 'errors': errors, 'urls': unique}


def bfs_urls(site: str, limit: int) -> list:
    """Фолбэк-обход, если sitemap недоступен."""
    host = urllib.parse.urlsplit(site).netloc.lower()
    seen, queue, out = {norm_key(site)}, [site], []
    while queue and len(out) < limit:
        cur = queue.pop(0)
        res = fetch(cur)
        if res['status'] != 200 or not res['body']:
            continue
        out.append(cur)
        p = PageParser()
        p.feed(res['body'])
        for link in p.a_links:
            href = urllib.parse.urljoin(res['final_url'], link['href'])
            sp = urllib.parse.urlsplit(href)
            if sp.scheme not in ('http', 'https') or sp.netloc.lower() != host:
                continue
            if sp.path.lower().endswith(SEO_SUFFIX):
                continue
            key = norm_key(href)
            if key not in seen:
                seen.add(key)
                queue.append(href)
    return out


# --- HTML parser -----------------------------------------------------------

class PageParser(HTMLParser):
    SKIP = ('script', 'style', 'noscript', 'template', 'svg')

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = None
        self.meta = {}
        self.link_tags = []
        self.headings = []
        self.a_links = []
        self.images = []
        self.scripts = []
        self.styles = []
        self.jsonld = []
        self.text_parts = []
        self.lang = None
        self.refresh = None
        self._in_title = False
        self._heading = None
        self._a = None
        self._ld = None
        self._skip = []

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or '') for k, v in attrs}
        if tag == 'html':
            self.lang = a.get('lang') or None
        elif tag == 'title':
            self._in_title = True
        elif tag == 'meta':
            if a.get('charset'):
                self.meta.setdefault('charset', a['charset'])
            key = (a.get('name') or a.get('property') or a.get('http-equiv') or '').lower()
            if a.get('http-equiv', '').lower() == 'refresh':
                self.refresh = a.get('content')
            if key and key not in self.meta:
                self.meta[key] = a.get('content', '')
        elif tag == 'link':
            self.link_tags.append({'rel': a.get('rel', '').lower(),
                                   'href': a.get('href', ''),
                                   'hreflang': a.get('hreflang', ''),
                                   'type': a.get('type', '').lower()})
        elif tag == 'a':
            self._a = {'href': a.get('href', ''), 'rel': a.get('rel', '').lower(),
                       'text': '', 'img_alt': '', 'aria': a.get('aria-label', '')}
        elif tag in ('img', 'image'):
            src = a.get('src') or a.get('href') or ''
            self.images.append({'src': src, 'alt': a.get('alt'), 'tracker': is_tracker(src)})
            if self._a is not None:
                self._a['img_alt'] = a.get('alt') or ''
        elif tag == 'script':
            if a.get('type', '').lower() == 'application/ld+json':
                self._ld = []
            else:
                self.scripts.append({'src': a.get('src', ''), 'async': 'async' in a.get('async', ''),
                                     'defer': 'defer' in a.get('defer', '')})
            self._skip.append(tag)
        elif tag == 'style':
            self.styles.append({'src': a.get('src', ''), 'inline': True})
            self._skip.append(tag)
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6'):
            self._heading = {'level': int(tag[1]), 'text': ''}
        elif tag in self.SKIP:
            self._skip.append(tag)

    def handle_startendtag(self, tag, attrs):
        if tag in ('img', 'link', 'meta'):
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag == 'title':
            self._in_title = False
        elif tag == 'a':
            if self._a is not None:
                self._a['text'] = self._a['text'].strip()
                self.a_links.append(self._a)
                self._a = None
        elif tag == 'script' and self._ld is not None:
            self.jsonld.append(''.join(self._ld))
            self._ld = None
        elif tag in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6') and self._heading is not None:
            self._heading['text'] = self._heading['text'].strip()
            self.headings.append(self._heading)
            self._heading = None
        if tag in self._skip:
            self._skip.pop()

    def handle_data(self, data):
        if self._ld is not None:
            self._ld.append(data)
        elif self._in_title:
            self.title = (self.title or '') + data
        elif self._heading is not None:
            self._heading['text'] += data
        elif self._a is not None:
            self._a['text'] += data
        elif not self._skip:
            self.text_parts.append(data)

    def visible_text(self) -> str:
        return ' '.join(self.text_parts)


def ld_types(node, out: set) -> set:
    if isinstance(node, list):
        for item in node:
            ld_types(item, out)
    elif isinstance(node, dict):
        t = node.get('@type')
        if isinstance(t, str):
            out.add(t)
        elif isinstance(t, list):
            out.update(t)
        for v in node.values():
            if isinstance(v, (dict, list)):
                ld_types(v, out)
    return out


# --- page analysis ---------------------------------------------------------

def analyze(res: dict, host: str, thin_words: int = THIN_WORDS) -> dict:
    url, final = res['url'], res['final_url']
    page = {
        'url': url, 'final_url': final, 'status': res['status'],
        'redirected': False, 'redirect_kind': None,
        'content_type': res['content_type'], 'bytes': res['bytes'],
        'elapsed_ms': res['elapsed_ms'], 'error': res['error'],
        'title': None, 'title_len': 0, 'description': None, 'description_len': 0,
        'canonical': None, 'canonical_matches': None, 'meta_robots': None, 'indexable': True,
        'lang': None, 'viewport': None, 'charset': None, 'http_refresh': None,
        'h1': None, 'h1_count': 0, 'heading_counts': {'h1': 0, 'h2': 0, 'h3': 0},
        'heading_order_ok': True, 'word_count': 0, 'text_rate': 0.0,
        'schema_types': [], 'schema_blocks': 0, 'schema_error': False,
        'og': {}, 'twitter': {}, 'hreflang': [],
        'links_internal': 0, 'links_external': 0, 'links_nofollow': 0,
        'links_empty_anchor': 0, 'links_icon_only': 0, 'internal_targets': [],
        'images': 0, 'images_no_alt': 0, 'images_empty_alt': 0,
        'scripts': 0, 'scripts_inline': 0, 'scripts_external': 0, 'styles': 0, 'styles_inline': 0,
        'mixed_content': 0, 'seo_url': True, 'url_len': 0,
        'cwv': None, 'issues': [],
    }
    issues = page['issues']

    def add(level, code, msg, fix=''):
        issues.append({'level': level, 'code': code, 'msg': msg, 'fix': fix})

    if res['error']:
        add('error', 'FETCH_FAILED', f"страница не отдана: {res['error']}",
            'проверить доступность и редиректы')
        return page
    if res['status'] >= 400:
        add('error', 'HTTP_STATUS', f'HTTP {res["status"]}',
            'страница недоступна — проверить в Вебмастере и внутренних ссылках')
        return page
    if res['content_type'] and 'html' not in res['content_type']:
        return page

    p = PageParser()
    p.feed(res['body'] or '')
    vis = p.visible_text()
    text_len = len(vis)

    src_sp, fin_sp = urllib.parse.urlsplit(url), urllib.parse.urlsplit(final)
    if norm_key(url) != norm_key(final):
        page['redirect_kind'] = 'path'
    elif src_sp.scheme != fin_sp.scheme:
        page['redirect_kind'] = 'scheme'
    page['redirected'] = page['redirect_kind'] is not None

    page['title'] = (p.title or '').strip() or None
    page['title_len'] = len(page['title'] or '')
    page['description'] = (p.meta.get('description') or '').strip() or None
    page['description_len'] = len(page['description'] or '')
    page['lang'] = p.lang
    page['viewport'] = p.meta.get('viewport')
    page['meta_robots'] = p.meta.get('robots') or None
    page['indexable'] = 'noindex' not in (page['meta_robots'] or '').lower()
    page['http_refresh'] = p.refresh
    page['charset'] = p.meta.get('charset') or page['charset']
    page['word_count'] = count_words(vis)
    page['text_rate'] = round(text_len / max(len(res['body']), 1), 3)

    for lt in p.link_tags:
        if 'canonical' in lt['rel']:
            page['canonical'] = urllib.parse.urljoin(final, lt['href'])
        if 'alternate' in lt['rel'] and lt['hreflang']:
            page['hreflang'].append({'lang': lt['hreflang'],
                                     'href': urllib.parse.urljoin(final, lt['href'])})
    if page['canonical']:
        page['canonical_matches'] = norm_key(page['canonical']) == norm_key(final)

    for h in p.headings:
        if h['level'] <= 3:
            page['heading_counts'][f'h{h["level"]}'] += 1
        if h['level'] == 1:
            page['h1_count'] += 1
            if not page['h1']:
                page['h1'] = h['text']
    levels = [h['level'] for h in p.headings]
    page['heading_order_ok'] = all(b <= a + 1 for a, b in zip(levels, levels[1:]))

    types = set()
    page['schema_blocks'] = len(p.jsonld)
    for block in p.jsonld:
        try:
            ld_types(json.loads(block), types)
        except json.JSONDecodeError:
            page['schema_error'] = True
    page['schema_types'] = sorted(types)

    for k, v in p.meta.items():
        if k.startswith('og:'):
            page['og'][k] = v
        elif k.startswith('twitter:'):
            page['twitter'][k] = v

    for link in p.a_links:
        if not link['href'] or link['href'].startswith(('#', 'mailto:', 'tel:', 'javascript:')):
            continue
        href = urllib.parse.urljoin(final, link['href'])
        if not link['text'] and not link.get('img_alt') and not link.get('aria'):
            page['links_empty_anchor'] += 1
        elif not link['text'] and not link.get('aria'):
            page['links_icon_only'] += 1
        if 'nofollow' in link['rel']:
            page['links_nofollow'] += 1
        if same_site(href, host):
            page['links_internal'] += 1
            page['internal_targets'].append(href)
        else:
            page['links_external'] += 1

    content_imgs = [i for i in p.images if not i['tracker']]
    page['images'] = len(content_imgs)
    page['images_no_alt'] = sum(1 for i in content_imgs if i['alt'] is None)
    page['images_empty_alt'] = sum(1 for i in content_imgs if i['alt'] == '')
    page['scripts'] = len(p.scripts)
    page['scripts_inline'] = sum(1 for s in p.scripts if not s['src'])
    page['scripts_external'] = page['scripts'] - page['scripts_inline']
    page['styles'] = len(p.styles)
    page['styles_inline'] = sum(1 for s in p.styles if not s['src'])

    if final.startswith('https://'):
        subs = [i['src'] for i in p.images] + [s['src'] for s in p.scripts] + \
               [s['src'] for s in p.styles]
        page['mixed_content'] = sum(1 for s in subs if s.startswith('http://'))

    path = urllib.parse.urlsplit(final).path
    page['url_len'] = len(path)
    page['seo_url'] = path.isascii() and '_' not in path and len(path) <= YANDEX_URL_MAX

    if not page['title']:
        add('error', 'NO_TITLE', 'нет <title>', 'добавить title с ключевым запросом в начало')
    elif page['title_len'] > TITLE_MAX:
        add('warn', 'TITLE_LONG', f'title {page["title_len"]} симв. (>{TITLE_MAX}) — риск обрезки',
            'сократить до 50-70 символов, ключевой запрос в начало')
    if not page['description']:
        add('warn', 'NO_DESCRIPTION', 'нет meta description',
            'добавить description 100-180 символов с призывом и ключевым запросом')
    elif page['description_len'] > DESC_MAX:
        add('warn', 'DESC_LONG', f'description {page["description_len"]} симв. — будет обрезан',
            'сократить до 150-180 символов')
    if not page['h1_count']:
        add('error', 'NO_H1', 'нет <h1>', 'добавить один H1 с основным запросом страницы')
    elif page['h1_count'] > 1:
        add('warn', 'MULTI_H1', f'H1 на странице {page["h1_count"]}',
            'оставить один H1, остальные понизить до H2')
    if not page['heading_order_ok']:
        add('warn', 'HEADING_ORDER', 'пропущен уровень заголовка (H1 → H3)',
            'выстроить иерархию H1 → H2 → H3 без пропусков')
    if not page['canonical']:
        add('warn', 'NO_CANONICAL', ' нет canonical', 'добавить link rel=canonical на саму себя')
    elif not page['canonical_matches']:
        add('warn', 'CANONICAL_OTHER', f'canonical указывает на другой URL: {page["canonical"]}',
            'проверить намерение — дубли могут уйти из индекса')
    if not page['indexable']:
        add('error', 'NOINDEX', f'meta robots запрещает индексацию: {page["meta_robots"]}',
            'снять noindex, если страница должна быть в выдаче')
    if not page['lang']:
        add('info', 'NO_LANG', 'нет атрибута lang на <html>', 'добавить lang="ru"')
    if not page['viewport']:
        add('warn', 'NO_VIEWPORT', ' нет viewport — мобильная выдача страдает',
            'добавить meta viewport width=device-width')
    if not page['og']:
        add('info', 'NO_OG', 'нет Open Graph — на ранжирование не влияет, нужен для превью в соцсетях и мессенджерах',
            'добавить og:title/og:description/og:image при репостах')
    if page['http_refresh']:
        add('warn', 'HTTP_REFRESH', f'http-equiv refresh: {page["http_refresh"]}',
            'заменить редирект на серверный 301/302')
    if page['mixed_content']:
        add('error', 'MIXED_CONTENT', f'{page["mixed_content"]} http-ресурсов на https-странице',
            'перевести ресурсы на https — иначе браузер их заблокирует')
    if not page['seo_url']:
        add('warn', 'SEO_URL', f'неоптимальный URL: {path}',
            'транслит, без подчёркиваний, короче 75 символов')
    if page['word_count'] and page['word_count'] < thin_words:
        add('warn', 'THIN_CONTENT', f'{page["word_count"]} слов — меньше порога {thin_words}',
            f'расширить контент или объединить со страницей-приёмником; если это карточки '
            f'однотипного каталога, порог неприменим — см. --thin-words')
    if not page['schema_types'] and not page['schema_error']:
        add('info', 'NO_SCHEMA', 'нет JSON-LD разметки',
            'добавить Organization/LocalBusiness + BreadcrumbList + WebSite')
    dead = sorted(set(page['schema_types']) & DEAD_SCHEMA)
    if dead:
        add('info', 'DEAD_SCHEMA', f'типы без rich results: {", ".join(dead)}',
            'валидны для семантики, но сниплет не расширят ни Яндекс, ни Google — оставить или удалить')
    if page['schema_error']:
        add('error', 'SCHEMA_INVALID', 'JSON-LD не парсится — разметка сломана',
            'проверить синтаксис JSON в script ld+json')
    if page['images_no_alt']:
        add('warn', 'IMG_NO_ALT', f'{page["images_no_alt"]} из {page["images"]} картинок без alt',
            'заполнить alt — по нему ищут картинки')
    if not page['links_internal']:
        add('warn', 'NO_INTERNAL_LINKS', 'нет исходящих внутренних ссылок',
            'добавить ссылки на связанные страницы из меню/блока')
    if page['links_empty_anchor'] > 2:
        add('warn', 'EMPTY_ANCHOR', f'{page["links_empty_anchor"]} ссылок без анкора',
            'добавить описательные анкоры или aria-label')
    if page['links_icon_only'] > 5:
        add('info', 'ICON_ONLY_LINKS', f'{page["links_icon_only"]} ссылок-иконок без aria-label',
            'для доступности и скринридеров добавить aria-label')
    if page['scripts_external'] >= 15:
        add('warn', 'MANY_SCRIPTS', f'{page["scripts_external"]} внешних скриптов',
            'свести к 5-7, инлайнить мелкие, отложить некритичные')
    if page['redirect_kind'] == 'scheme':
        add('warn', 'REDIRECT_SCHEME', f'адрес в обходе {src_sp.scheme}, отдаётся {fin_sp.scheme}',
            'выдать конечный адрес в sitemap, меню и ссылках — иначе лишний редирект на каждый запрос')
    elif page['redirected']:
        add('info', 'REDIRECT', f'редирект на {page["final_url"]}',
            'убедиться, что редирект одношаговый и 301, а не цепочка')
    return page


# --- PageSpeed Insights (field data CrUX) ----------------------------------

def psi(url: str, strategy: str, key: str) -> dict:
    params = {'url': url, 'strategy': strategy, 'category': 'performance'}
    if key:
        params['key'] = key
    req = urllib.request.Request(PSI_API + '?' + urllib.parse.urlencode(params),
                                 headers={'User-Agent': UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return {'error': f'HTTP {e.code}'}
    except Exception as e:
        return {'error': f'{type(e).__name__}: {e}'}

    out = {'strategy': strategy, 'field': False}
    exp = data.get('loadingExperience') or {}
    metrics = exp.get('metrics') or {}

    def p75(key):
        v = metrics.get(key)
        return v.get('percentile') if isinstance(v, dict) else None

    out['overall_category'] = exp.get('overall_category')
    if metrics:
        out['field'] = True
        out['lcp_ms'] = p75('LARGEST_CONTENTFUL_PAINT_MS')
        out['inp_ms'] = p75('INTERACTION_TO_NEXT_PAINT')
        out['fid_ms'] = p75('FIRST_INPUT_DELAY_MS')
        out['cls'] = p75('CUMULATIVE_LAYOUT_SHIFT_SCORE')
    else:
        lab = ((data.get('lighthouseResult') or {}).get('audits') or {})
        lcp = lab.get('largest-contentful-paint') or {}
        if lcp.get('numericValue'):
            out['lcp_ms_lab'] = int(lcp['numericValue'])
    out['cwv'] = {}
    for metric, good in CWV_GOOD.items():
        val = out.get(metric)
        if val is None:
            continue
        out['cwv'][metric] = ('good' if val <= good else
                              'needs_improvement' if val <= CWV_NI[metric] else 'poor')
    return out


# --- site level ------------------------------------------------------------

def site_report(pages: list, robots: dict, sitemaps: dict, site: str) -> tuple:
    ok = [p for p in pages if p['status'] == 200 and p['content_type'] and
          'html' in p['content_type']]
    issues = []

    def add(level, code, msg, fix=''):
        issues.append({'level': level, 'code': code, 'msg': msg, 'fix': fix})

    if not robots.get('exists'):
        add('error', 'NO_ROBOTS', 'robots.txt не отдаётся (404)',
            'создать robots.txt с явным Sitemap и правилами для служебных разделов')
    else:
        if robots.get('disallow_all'):
            add('error', 'ROBOTS_DISALLOW_ALL', 'Disallow: / — сайт закрыт от роботов',
                'проверить, что это не забытая заглушка после переезда')
        if not robots.get('sitemaps'):
            add('warn', 'ROBOTS_NO_SITEMAP', 'нет директивы Sitemap в robots.txt',
                'добавить Sitemap: https://domain/sitemap.xml')
    for err in sitemaps.get('errors') or []:
        add('error', 'SITEMAP_ERROR', err, 'исправить sitemap или обновить в Вебмастере')
    if not sitemaps.get('urls'):
        add('warn', 'NO_SITEMAP_URLS', 'sitemap не отдал ни одного URL',
            'проверить доступность sitemap.xml и формат lastmod')
    for f in sitemaps.get('files') or []:
        if f['urls'] > 50000:
            add('info', 'SITEMAP_SPLIT', f"{f['sitemap']}: {f['urls']} URL",
                'разбить на sitemap index — лимит 50 000 URL на файл')

    errors = [p for p in pages if p['status'] >= 400]
    if errors:
        add('error', 'BROKEN_PAGES', f'{len(errors)} URL отдают 4xx/5xx',
            'закрыть 404/редиректом, убрать из sitemap и меню')
    redirects = [p for p in pages if p['redirected'] and p['status'] == 200]
    if len(redirects) > max(2, len(pages) * 0.1):
        add('warn', 'MANY_REDIRECTS', f'{len(redirects)} URL редиректят',
            'прямые ссылки в меню и sitemap должны вести на конечный адрес')

    dups = {}
    for field, label in (('title', 'title'), ('description', 'description'), ('h1', 'H1')):
        seen = {}
        for p in ok:
            val = (p.get(field) or '').strip().lower()
            if val:
                seen.setdefault(val, []).append(p['url'])
        rep = {v[0]: v[1:] for v in seen.values() if len(v) > 1}
        if rep:
            dups[field] = {k: v for k, v in list(rep.items())[:20]}
            add('warn', f'DUP_{field.upper()}', f'дубли {label}: {len(rep)} групп',
                'уникализировать — иначе Яндекс выберет одну страницу группы')

    sm_keys = {norm_key(u) for u in sitemaps.get('urls') or []}
    not_in_sitemap = [p['url'] for p in ok if norm_key(p['url']) not in sm_keys]

    incoming = {}
    for p in ok:
        for t in p.get('internal_targets') or []:
            incoming.setdefault(norm_key(t), set()).add(norm_key(p['url']))
    root = norm_key(site)
    orphans = [p for p in ok
               if norm_key(p['url']) != root and not incoming.get(norm_key(p['url']))]

    if sm_keys and not_in_sitemap:
        add('info', 'NOT_IN_SITEMAP', f'{len(not_in_sitemap)} страниц не в sitemap',
            'страницы вне sitemap индексируются хуже')
    if orphans:
        add('warn', 'ORPHAN_PAGES', f'{len(orphans)} страниц без входящих внутренних ссылок',
            'добавить ссылки из меню/хлебных крошек/статей')

    alts = [p for p in ok if p['hreflang']]
    if alts:
        add('warn', 'HREFLANG_INCOMPLETE', f'hreflang есть на {len(alts)} страницах, '
            f'покрывает {len({norm_key(a["href"]) for p in alts for a in p["hreflang"]})} URL',
            'языковые версии должны быть взаимно ссылающимися, иначе робот не свяжет их в группу')
        if not any(a['lang'] == 'x-default' for p in alts for a in p['hreflang']):
            add('info', 'HREFLANG_NO_XDEFAULT', 'нет x-default среди языковых версий',
                'x-default указывает на страницу выбора языка для неподходящей локали')

    https = sum(1 for p in pages if p['url'].startswith('https://'))
    words = sorted(p['word_count'] for p in ok if p['word_count'])
    median_w = words[len(words) // 2] if words else 0
    thin_vs_median = sum(1 for w in words if w < median_w * 0.5) if median_w else 0
    by_level = {}
    for p in pages:
        for i in p['issues']:
            by_level[i['level']] = by_level.get(i['level'], 0) + 1

    top_codes = {}
    for p in pages:
        for i in p['issues']:
            top_codes[i['code']] = top_codes.get(i['code'], 0) + 1

    summary = {
        'urls_crawled': len(pages),
        'ok': len(ok),
        'broken': len(errors),
        'https_share': round(https / len(pages), 3) if pages else None,
        'avg_words': round(sum(words) / len(words)) if words else None,
        'median_words': words[len(words) // 2] if words else None,
        'words_p10': words[int(len(words) * 0.1)] if words else None,
        'words_p90': words[int(len(words) * 0.9)] if words else None,
        'thin_vs_median': thin_vs_median,
        'with_canonical': sum(1 for p in ok if p['canonical']),
        'indexable': sum(1 for p in ok if p['indexable']),
        'with_schema': sum(1 for p in ok if p['schema_types']),
        'sitemap_urls': len(sitemaps.get('urls') or []),
        'not_in_sitemap': len(not_in_sitemap) if sm_keys else None,
        'orphans': len(orphans),
        'issues_by_level': by_level,
        'issues_by_code': dict(sorted(top_codes.items(), key=lambda x: -x[1])),
    }
    return issues, summary, dups


# --- diff ------------------------------------------------------------------

WATCH = ('status', 'title_len', 'description_len', 'word_count', 'indexable',
         'canonical_matches', 'h1_count', 'images_no_alt', 'links_internal',
         'scripts', 'mixed_content', 'redirected', 'cwv')


def compare(old: dict, new: dict) -> dict:
    def index(snap):
        return {norm_key(p['url']): p for p in snap.get('pages', [])}

    unreliable = []
    for label, snap in (('предыдущий', old), ('текущий', new)):
        if snap.get('summary', {}).get('truncated'):
            unreliable.append(f'{label} прогон был усечён '
                              f'({snap["summary"].get("ok")} из {snap["summary"].get("discovered")})')

    o, n = index(old), index(new)
    added, removed, changed, issues = sorted(set(n) - set(o)), sorted(set(o) - set(n)), [], []
    for key in sorted(set(o) & set(n)):
        for field in WATCH:
            a, b = o[key].get(field), n[key].get(field)
            if a != b:
                changed.append({'url': n[key]['url'], 'field': field,
                                'from': a, 'to': b})
    o_codes = old.get('summary', {}).get('issues_by_code', {})
    n_codes = new.get('summary', {}).get('issues_by_code', {})
    for code in sorted(set(o_codes) | set(n_codes)):
        before, after = o_codes.get(code, 0), n_codes.get(code, 0)
        if before != after:
            issues.append({'code': code, 'from': before, 'to': after,
                           'delta': after - before})
    keys = ('urls_crawled', 'ok', 'broken', 'avg_words', 'median_words', 'with_canonical',
            'indexable', 'with_schema', 'not_in_sitemap', 'orphans')
    return {
        'generated': new.get('generated'),
        'previous': old.get('generated'),
        'reliable': not unreliable,
        'unreliable_reason': '; '.join(unreliable) if unreliable else None,
        'added': [n[k]['url'] for k in added],
        'removed': [o[k]['url'] for k in removed],
        'metric_changes': changed[:200],
        'metric_changes_total': len(changed),
        'issue_counts': issues,
        'summary_delta': {k: {'from': old.get('summary', {}).get(k),
                              'to': new.get('summary', {}).get(k)}
                          for k in keys},
    }


SNAP_RE = re.compile(r'^onpage-(\d{4}-\d{2}-\d{2})(?:-(\d{6}))?\.json$')


def load_snapshots(audit_dir: str, skip: str = '') -> tuple:
    """Последний снимок по дате/времени из имени. skip — имя текущего прогона."""
    if not os.path.isdir(audit_dir):
        return None, None
    dated = []
    for name in os.listdir(audit_dir):
        m = SNAP_RE.match(name)
        if m and name != skip:
            dated.append((m.group(1), m.group(2) or '', name))
    if not dated:
        return None, None
    name = max(dated)[2]
    with open(os.path.join(audit_dir, name), encoding='utf-8') as f:
        return json.load(f), name


def archive_existing(path: str) -> str:
    """Прогон того же дня не должен затирать предыдущий — уводим его в архив."""
    if not os.path.exists(path):
        return path
    m = SNAP_RE.match(os.path.basename(path))
    stamp = time.strftime('%H%M%S')
    archived = path.replace(f'onpage-{m.group(1)}.json', f'onpage-{m.group(1)}-{stamp}.json')
    os.replace(path, archived)
    return archived


# --- run -------------------------------------------------------------------

def audit_page(url: str, host: str, use_psi: bool, psi_key: str, strategy: str,
               thin_words: int = THIN_WORDS) -> dict:
    page = analyze(fetch(url), host, thin_words)
    if use_psi and page['status'] == 200 and norm_key(url) == norm_key(page['final_url']):
        page['cwv'] = psi(page['final_url'], strategy, psi_key)
    time.sleep(DELAY)
    return page


def ensure_reachable(site: str) -> tuple:
    """Если HTTPS не отвечает — откатиться на HTTP и сообщить, что TLS недоступен.

    Отдельный дефект, а не повод отказаться от аудита: без HTTPS браузер помечает
    сайт «небезопасным», а поисковики не дают полного доверия странице.
    """
    res = fetch(site)
    if not res['error'] and res['status'] < 500:
        return site, None
    if site.startswith('https://'):
        plain = 'http://' + site[len('https://'):]
        res2 = fetch(plain)
        if not res2['error'] and res2['status'] < 500:
            return plain, ('NO_HTTPS', 'error', 'HTTPS не отвечает, сайт доступен только по HTTP',
                           'включить TLS — без него браузер помечает сайт «небезопасным», '
                           'а Яндекс и Google не дают странице полного доверия')
    return site, None


def run_site(site: str, args) -> dict | None:
    """Полный цикл по одному сайту. Возвращает краткую сводку или None при неудаче."""
    if '://' not in site:
        site = 'https://' + site
    site = site.rstrip('/')
    host = urllib.parse.urlsplit(site).netloc.lower()
    audit_dir = os.path.join(AUDIT_ROOT, host.replace(':', '_'))
    print(f'\n{"=" * 60}\nсайт: {site}')

    site, tls_issue = ensure_reachable(site)
    if tls_issue:
        print(f'  {site}')
        print('  ВНИМАНИЕ: HTTPS не отвечает — продолжаю по HTTP, это отдельный дефект')

    robots = parse_robots(site)
    print(f"robots.txt: HTTP {robots['status']}, sitemaps в директиве: {len(robots['sitemaps'])}")

    source = 'urls-file'
    sitemaps = {'files': [], 'errors': [], 'urls': []}
    if args.urls:
        urls = [l.strip() for l in open(args.urls, encoding='utf-8') if l.strip()]
    else:
        sitemaps = collect_sitemaps(site, robots)
        source = 'sitemap' if sitemaps['urls'] else 'bfs'
        urls = sitemaps['urls']
        if not urls:
            print('sitemap пуст — фолбэк-обход с главной')
            urls = bfs_urls(site, args.limit or 200)
    discovered = len(urls)
    if args.limit:
        urls = urls[:args.limit]
    else:
        urls = [u for u in urls if not u.lower().split('?')[0].endswith(SEO_SUFFIX)]
    truncated = len(urls) < discovered
    print(f'URL к проверке: {len(urls)} из {discovered} (источник: {source})')
    if truncated:
        print(f'ВНИМАНИЕ: усечение — проверено {len(urls)} из {discovered}, '
              f'остальные {discovered - len(urls)} не смотрены. Сними --limit или укажи больше.')
    elif discovered > 500:
        print(f'  прогон большой: ~{discovered / 4 * 1.0 / 60:.0f} мин при 4 потоках. '
              'Для чужого крупного сайта лучше ограничить --limit.')
    if not urls:
        print('пропускаю сайт: не нашлось ни одного URL для проверки')
        return None

    psi_key = os.environ.get('PSI_API_KEY', '').strip()
    started = time.time()
    pages = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(audit_page, u, host, args.psi, psi_key, args.psi_strategy,
                               args.thin_words)
                   for u in urls]
        for i, fut in enumerate(futures, 1):
            pages.append(fut.result())
            if i % 25 == 0 or i == len(futures):
                print(f'  {i}/{len(futures)} ({time.time() - started:.0f}s)')

    site_issues, summary, dups = site_report(pages, robots, sitemaps, site)
    if tls_issue:
        site_issues.insert(0, {'level': tls_issue[1], 'code': tls_issue[0],
                               'msg': tls_issue[2], 'fix': tls_issue[3]})
    summary['discovered'] = discovered
    summary['truncated'] = truncated
    if truncated:
        summary['sitemap_urls'] = discovered
    snap = {
        'generated': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'site': site, 'url_source': source, 'robots': robots, 'sitemaps': sitemaps,
        'pages': pages, 'duplicate_groups': dups,
        'site_issues': site_issues, 'summary': summary,
    }

    os.makedirs(audit_dir, exist_ok=True)
    out = args.out or os.path.join(audit_dir, f'onpage-{date.today():%Y-%m-%d}.json')
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    if not args.out:
        archived = archive_existing(out)
        if archived != out:
            print(f'предыдущий снимок сохранён как {os.path.basename(archived)}')
    with open(out, 'w', encoding='utf-8') as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    print(f'\nснимок: {out}')

    if args.psi:
        note = 'нет field data CrUX (мало трафика) — только lab'
        errors = {}
        for p in pages:
            cwv = p.get('cwv') or {}
            if cwv.get('error'):
                msg = cwv['error']
            elif cwv and not cwv.get('field'):
                msg = note
            else:
                continue
            errors[msg] = errors.get(msg, 0) + 1
        for msg, n in errors.items():
            print(f'PSI: {n} URL — {msg}')
        if errors and not os.environ.get('PSI_API_KEY'):
            print('  подсказка: задай PSI_API_KEY, без ключа Google отдаёт 429')

    print(f"\nстраниц: {summary['ok']} ok / {summary['broken']} битых, "
          f"https: {summary['https_share']}, слов (медиана): {summary['median_words']}")
    print(f"canonical: {summary['with_canonical']}, в индексе: {summary['indexable']}, "
          f"со schema: {summary['with_schema']}, сирот: {summary['orphans']}")
    print('топ проблем по сайту:')
    for code, n in list(summary['issues_by_code'].items())[:12]:
        print(f'  {n:>4}  {code}')
    if site_issues:
        print('проблемы уровня сайта:')
        for i in site_issues:
            print(f"  [{i['level']}] {i['code']}: {i['msg']}")

    row = {'host': host, 'ok': summary['ok'], 'broken': summary['broken'],
           'orphans': summary['orphans'], 'not_in_sitemap': summary['not_in_sitemap'],
           'indexable': summary['indexable'], 'with_schema': summary['with_schema'],
           'median_words': summary['median_words'], 'snapshot': out,
           'words_p10': summary['words_p10'], 'words_p90': summary['words_p90'],
           'thin_vs_median': summary['thin_vs_median'], 'thin_words': args.thin_words,
           'source': source, 'sitemap_urls': summary['sitemap_urls'],
           'discovered': discovered, 'truncated': truncated,
           'errors': summary['issues_by_level'].get('error', 0),
           'top_issue': next(iter(summary['issues_by_code']), '—')}

    if args.diff:
        prev, prev_name = load_snapshots(audit_dir, skip=os.path.basename(out))
        if not prev:
            print('\ndiff: предыдущий снимок не найден')
        else:
            d = compare(prev, snap)
            path = out.replace('.json', '-diff.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
            print(f"\ndiff с {prev_name}: +{len(d['added'])} URL, "
                  f"-{len(d['removed'])} URL, изменений метрик: {d['metric_changes_total']}")
            for ch in d['metric_changes'][:10]:
                print(f"  {ch['field']}: {ch['from']} → {ch['to']}  {ch['url']}")
            for ic in d['issue_counts']:
                print(f"  {ic['code']}: {ic['from']} → {ic['to']} ({ic['delta']:+d})")
            print(f'diff: {path}')
            if d['reliable']:
                row['diff'] = {'added': len(d['added']), 'removed': len(d['removed']),
                               'metric_changes': d['metric_changes_total'],
                               'regressions': [ic for ic in d['issue_counts'] if ic['delta'] > 0]}
            else:
                print(f'  ДЕЛЬТА НЕНАДЁЖНА: {d["unreliable_reason"]}. '
                      'Сравнивать снимки разного объёма бессмысленно — сначала прогони без --limit.')
    return row


def print_overview(rows: list) -> None:
    """Сводка по всем доменам — на неё смотришь, чтобы понять, куда лезть."""
    if not rows:
        return
    print(f'\n{"=" * 60}\nСВОДКА ПО СЕТИ — {len(rows)} домен(ов)\n')
    print('ист = источник URL; bfs = sitemap недоступен, обход с главной по ссылкам (покрытие неполное)')
    head = (f"{'домен':<22} {'ист':<6} {'покрытие':>9} {'404':>4} {'ошиб':>5} {'сирот':>6} "
            f"{'не в sm':>8} {' слов':>6}  топ-проблема")
    print(head)
    print('-' * len(head))
    for r in rows:
        words = r['median_words'] if r['median_words'] is not None else '—'
        missed = r['not_in_sitemap'] if r['not_in_sitemap'] is not None else '—'
        cover = f"{r['ok']}/{r['discovered']}" + ('!' if r['truncated'] else '')
        print(f"{r['host']:<22} {r['source']:<6} {cover:>9} {r['broken']:>4} "
              f"{r['errors']:>5} {r['orphans']:>6} {str(missed):>8} {str(words):>6}  {r['top_issue']}")
    total_err = sum(r['errors'] for r in rows)
    total_broken = sum(r['broken'] for r in rows)
    partial = [r['host'] for r in rows if r['source'] != 'sitemap']
    cut = [f"{r['host']} ({r['ok']} из {r['discovered']})" for r in rows if r['truncated']]
    print(f"\nвсего замечаний уровня error: {total_err}, битых URL: {total_broken}")
    if cut:
        print('УСЕЧЕНИЕ (проверена не часть сайта, а её срез): ' + ', '.join(cut))
    if partial:
        print('неполное покрытие (нужен sitemap или --urls): ' + ', '.join(partial))
    print('\nдлина текста (слов): p10 / медиана / p90, и сколько страниц в 2+ раза короче медианы')
    for r in rows:
        p10 = r['words_p10'] if r['words_p10'] is not None else '—'
        p90 = r['words_p90'] if r['words_p90'] is not None else '—'
        med = r['median_words'] if r['median_words'] is not None else '—'
        print(f"  {r['host']:<22} {str(p10):>4} / {str(med):>4} / {str(p90):>4}   "
              f"выбросов: {r['thin_vs_median']} (порог THIN_CONTENT = {r['thin_words']})")
    regressions = [(r['host'], ic) for r in rows if r.get('diff')
                   for ic in r['diff']['regressions']]
    if regressions:
        print('\nРЕГРЕССИИ относительно прошлого прогона (счётчик вырос):')
        for host, ic in sorted(regressions, key=lambda x: -x[1]['delta']):
            print(f"  {host:<24} {ic['code']}: {ic['from']} → {ic['to']} ({ic['delta']:+d})")
    else:
        print('\nрегрессий нет — ни один счётчик проблем не вырос')


def parse_site_line(line: str) -> tuple:
    """`domain # key=value key=value` → (домен, настройки прогона)."""
    domain, _, tail = line.partition('#')
    opts = {}
    for token in tail.split():
        if '=' in token:
            key, _, value = token.partition('=')
            opts[key.strip()] = value.strip()
    return domain.strip(), opts


def main() -> None:
    sys.stdout.reconfigure(encoding='utf-8')
    ap = argparse.ArgumentParser(description='On-page SEO-аудит с diff прогонов')
    ap.add_argument('site', nargs='?', help='URL сайта')
    ap.add_argument('--all', metavar='FILE', help='файл со списком доменов, по одному в строке')
    ap.add_argument('--limit', type=int, default=0,
                    help='максимум URL на сайт; 0 = без усечения (по умолчанию)')
    ap.add_argument('--urls', help='файл со списком URL одного сайта')
    ap.add_argument('--psi', action='store_true', help='добавить PageSpeed Insights (field data)')
    ap.add_argument('--psi-strategy', default='mobile', choices=('mobile', 'desktop'))
    ap.add_argument('--thin-words', type=int, default=THIN_WORDS,
                    help=f'порог THIN_CONTENT (по умолчанию {THIN_WORDS}); '
                         'для каталога карточек поставь меньше')
    ap.add_argument('--diff', action='store_true', help='сравнить с предыдущим снимком')
    ap.add_argument('--compare', nargs=2, metavar=('OLD', 'NEW'), help='diff двух файлов')
    ap.add_argument('--out', help='путь к файлу снимка (только для одного сайта)')
    args = ap.parse_args()

    if args.compare:
        with open(args.compare[0], encoding='utf-8') as f:
            old = json.load(f)
        with open(args.compare[1], encoding='utf-8') as f:
            new = json.load(f)
        print(json.dumps(compare(old, new), ensure_ascii=False, indent=2))
        return

    if args.all:
        if args.site or args.out:
            ap.error('--all несовместим с позиционным URL и --out')
        if args.urls:
            ap.error('--urls рассчитан на один сайт, с --all он применится к каждому домену')
        with open(args.all, encoding='utf-8') as f:
            lines = [parse_site_line(l) for l in f if l.strip() and not l.strip().startswith('#')]
        sites = [(d, o) for d, o in lines if d]
        if not sites:
            sys.exit(f'в файле {args.all} нет ни одного домена')
        print(f'доменов в списке: {len(sites)}')
        rows = []
        for domain, opts in sites:
            site_args = argparse.Namespace(**vars(args))
            if 'thin-words' in opts:
                site_args.thin_words = int(opts['thin-words'])
            if 'limit' in opts:
                site_args.limit = int(opts['limit'])
            if 'psi' in opts:
                site_args.psi = opts['psi'].lower() in ('1', 'true', 'yes')
            for key in opts:
                if key not in ('thin-words', 'limit', 'psi'):
                    print(f'  игнорирую неизвестную настройку {key} у {domain}')
            if site_args.thin_words != args.thin_words:
                print(f'  {domain}: порог THIN_CONTENT = {site_args.thin_words}')
            row = run_site(domain, site_args)
            if row:
                rows.append(row)
        print_overview(rows)
        return

    if not args.site:
        ap.error('нужен URL сайта, --all FILE или --compare OLD NEW')
    row = run_site(args.site, args)
    if row:
        print_overview([row])


if __name__ == '__main__':
    main()
