#!/usr/bin/env python3
"""Самопроверка onpage_audit: парсер, чеки, diff. Без фреймворков.

  python onpage_selfcheck.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout.reconfigure(encoding='utf-8')

from onpage_audit import (PageParser, analyze, compare, count_words, ld_types,
                          norm_key)

HOST = 'zavodsvay.ru'

FILLER = ('Стальные винтовые сваи применяют для забора, гаража, бани и лёгких '
          'фундаментов. Толщина стенки четыре миллиметра, лопасть диаметром '
          'триста миллиметров, нагрузка до двенадцати тонн. Монтаж бригадой '
          'за один день, гарантия десять лет, доставка по Пермскому краю.')

GOOD = ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<title>Винтовые сваи в Перми — купить с доставкой | Завод Свай</title>'
        '<meta name="description" content="Винтовые сваи для забора и фундамента '
        'в Перми. Собственное производство, монтаж за 1 день, гарантия 10 лет.">'
        '<link rel="canonical" href="https://zavodsvay.ru/vintovye-svai/">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta property="og:title" content="Винтовые сваи в Перми">'
        '<meta property="og:description" content="Собственное производство свай">'
        '<script type="application/ld+json">{"@context":"https://schema.org",'
        '"@type":"Organization","name":"Завод Свай"}</script>'
        '<script src="https://cdn.example.com/a.js"></script>'
        '<script>console.log("init")</script>'
        '</head><body><h1>Винтовые сваи в Перми</h1>'
        '<h2>Каталог</h2><p>' + FILLER * 12 + '</p>'
        '<img src="/img/svaya-1.jpg" alt="Винтовая свая в грунте">'
        '<img src="/img/montazh.jpg" alt="Монтаж свай бригадой">'
        '<a href="/katalog/">Каталог свай</a> <a href="https://example.com/blog/">Блог</a>'
        '</body></html>')

BROKEN = ('<html><head>'
          '<title>Котел газовый цена</title>'
          '<meta name="robots" content="noindex, nofollow">'
          '<meta http-equiv="refresh" content="0; url=/novoe/">'
          '<link rel="canonical" href="https://zavodsvay.ru/drugoe/">'
          '</head><body><p>Котел газовый.</p>'
          '<img src="http://cdn.example.com/logo.png">'
          '<a href="/katalog/"></a>'
          '<h1>Котел</h1><h3>Подзаголовок</h3>'
          '</body></html>')

MINIMAL = ('<html><head><title>x</title>'
           '<script type="application/ld+json">{"@type":"FAQPage"}</script>'
           + '<script src="/a.js"></script>' * 20 +
           '</head><body>' + FILLER * 8 +
           '<a href="/a/"></a><a href="/b/"></a><a href="/c/"></a>'
           '<h2>Заголовок</h2></body></html>')

SCHEMA_BAD = ('<html><head><title>x</title>'
              '<script type="application/ld+json">{not json}</script>'
              '</head><body><h1>y</h1><h2>z</h2>' + FILLER * 8 +
              '</body></html>')

MULTILANG = ('<html lang="ru"><head><title>Сваи — купить в Перми</title>'
             '<link rel="canonical" href="https://example.ru/svai/">'
             '<link rel="alternate" hreflang="ru" href="https://example.ru/svai/">'
             '<link rel="alternate" hreflang="en" href="https://example.com/piles/">'
             '</head><body><h1>Сваи</h1>' + FILLER * 8 +
             '<a href="/katalog/">Каталог</a></body></html>')


def res(url, body, status=200, ctype='text/html; charset=utf-8', error=None):
    return {'url': url, 'final_url': url, 'status': status, 'content_type': ctype,
            'bytes': len(body.encode('utf-8')), 'body': body, 'error': error,
            'elapsed_ms': 12}


def codes(page):
    return {i['code'] for i in page['issues']}


def main() -> None:
    fails = []

    def check(name, got, want):
        if got != want:
            fails.append(f'{name}: получено {got!r}, ожидалось {want!r}')

    check('norm_key убирает схему, query, якорь, регистр',
          norm_key('https://Zavodsvay.ru/catalog/?utm=1#top'), 'zavodsvay.ru/catalog')
    check('norm_key корень', norm_key('https://zavodsvay.ru/'), 'zavodsvay.ru/')
    check('words считает только слова',
          count_words('привет мир 42 ! --'), 2)
    check('ld_types собирает список и вложенность',
          ld_types([{'@type': ['A', 'B']}, {'@type': 'C'}], set()), {'A', 'B', 'C'})

    p = PageParser()
    p.feed(GOOD)
    check('title без мусора из скриптов', p.title.strip(),
          'Винтовые сваи в Перми — купить с доставкой | Завод Свай')
    check('один ld+json блок', len(p.jsonld), 1)
    check('скриптов (1 внешний + 1 inline)', len(p.scripts), 2)
    check('внешний скрипт', p.scripts[0]['src'], 'https://cdn.example.com/a.js')
    check('console.log не попал в текст', 'console.log' in p.visible_text(), False)
    check('skip-стек опустел', p._skip, [])
    check('ссылок', len(p.a_links), 2)
    check('картинок', len(p.images), 2)
    check('заголовков', [h['level'] for h in p.headings], [1, 2])

    good = analyze(res('https://zavodsvay.ru/vintovye-svai/', GOOD), HOST)
    check('чистая страница: ни одного замечания', codes(good), set())
    check('title len', good['title_len'], 55)
    check('canonical совпал', good['canonical_matches'], True)
    check('в индексе', good['indexable'], True)
    check('h1', good['h1'], 'Винтовые сваи в Перми')
    check('schema', good['schema_types'], ['Organization'])
    check('lang', good['lang'], 'ru')
    check('og собран', 'og:title' in good['og'], True)
    check('alt у всех картинок', good['images_no_alt'], 0)
    check('внутр. ссылок', good['links_internal'], 1)
    check('внеш. ссылок', good['links_external'], 1)
    check('скриптов (1 внешний + 1 inline)', good['scripts'], 2)
    check('styles', good['styles'], 0)
    check('нет mixed content', good['mixed_content'], 0)
    check('текстовая доля', good['text_rate'] > 0.5, True)
    check('слов много', good['word_count'] > 300, True)
    check('url нормальный', good['seo_url'], True)
    check('иерархия заголовков', good['heading_order_ok'], True)

    want = {'NO_LANG', 'NO_OG', 'NO_VIEWPORT', 'NO_DESCRIPTION', 'NO_SCHEMA',
            'IMG_NO_ALT', 'MIXED_CONTENT', 'NOINDEX', 'THIN_CONTENT',
            'HTTP_REFRESH', 'CANONICAL_OTHER', 'HEADING_ORDER', 'SEO_URL'}
    bad = analyze(res('https://zavodsvay.ru/kotly_gasovye/', BROKEN), HOST)
    check('битая страница: нет незаявленных замечаний', want - codes(bad), set())
    check('битая страница: все заявленные сработали', want, want & codes(bad))
    check('noindex', bad['indexable'], False)
    check('mixed content', bad['mixed_content'], 1)
    check('иерархия сломана', bad['heading_order_ok'], False)
    check('пустой anchor посчитан', bad['links_empty_anchor'], 1)
    check('одна пустая ссылка — не дефект', 'EMPTY_ANCHOR' in codes(bad), False)
    check('подчёркивание в URL', bad['seo_url'], False)

    mini = analyze(res('https://zavodsvay.ru/min/', MINIMAL), HOST)
    check('нет H1', 'NO_H1' in codes(mini), True)
    check('мёртвый schema', 'DEAD_SCHEMA' in codes(mini), True)
    check('нет DEAD_SCHEMA на чистой странице', 'DEAD_SCHEMA' in codes(good), False)
    check('много скриптов', 'MANY_SCRIPTS' in codes(mini), True)
    check('3 пустые ссылки — дефект', 'EMPTY_ANCHOR' in codes(mini), True)

    sb = analyze(res('https://zavodsvay.ru/x/', SCHEMA_BAD), HOST)
    check('битый JSON-LD', 'SCHEMA_INVALID' in codes(sb), True)

    parked = analyze(res('http://ubm-85.su/',
                         '<html><head><title>Срок регистрации домена истек</title></head>'
                         '<body>Домен зарегистрирован в Рег.ру. Срок регистрации домена '
                         'ubm-85.su истек. Требуется продление.</body></html>'), 'ubm-85.su')
    check('парковка регистратора распознана', 'DOMAIN_EXPIRED' in codes(parked), True)

    ml = analyze(res('https://example.ru/svai/', MULTILANG), 'example.ru')
    check('hreflang собран как lang+url', ml['hreflang'],
          [{'lang': 'ru', 'href': 'https://example.ru/svai/'},
           {'lang': 'en', 'href': 'https://example.com/piles/'}])
    from onpage_audit import site_report, parse_site_line
    check('настройки домена из строки списка',
          parse_site_line('zavodsvay.ru # thin-words=45 limit=100'),
          ('zavodsvay.ru', {'thin-words': '45', 'limit': '100'}))
    check('домен без настроек', parse_site_line('  ubm.ru  '), ('ubm.ru', {}))
    from onpage_audit import site_options, apply_site_options
    check('настройки домена читаются из sites.txt',
          site_options('zavodsvay.ru').get('thin-words'), '45')
    check('домен без настроек не выдумывает их', site_options('example.org'), {})

    import argparse
    ns = argparse.Namespace(thin_words=300, limit=0, psi=False, out=None, urls=None,
                            site='zavodsvay.ru', diff=False, psi_strategy='mobile',
                            compare=None, all=None)
    apply_site_options(ns, 'zavodsvay.ru', 'тест')
    check('порог реально применился (дефис → подчёркивание)', ns.thin_words, 45)
    check('поля не плодились мусором', 'thin-words' in vars(ns), False)
    check('неизвестный ключ не роняет прогон',
          (lambda: (apply_site_options(ns, 'example.org', 'тест'),
                     ns.thin_words)[1])(), 45)
    site_issues, summary, _ = site_report(
        [ml], {'exists': True, 'sitemaps': [], 'disallow_all': False},
        {'files': [], 'errors': [], 'urls': []}, 'https://example.ru')
    site_codes = {i['code'] for i in site_issues}
    check('hreflang помечен как неполный (нет x-default)',
          'HREFLANG_INCOMPLETE' in site_codes and 'HREFLANG_NO_XDEFAULT' in site_codes, True)

    plain_codes = {i['code'] for i in site_report(
        [good], {'exists': True, 'sitemaps': [], 'disallow_all': False},
        {'files': [], 'errors': [], 'urls': []}, 'https://zavodsvay.ru')[0]}
    check('одноязычный сайт не получает замечаний по hreflang',
          {c for c in plain_codes if c.startswith('HREFLANG')}, set())

    http = analyze({'url': 'http://zavodsvay.ru/a/', 'final_url': 'https://zavodsvay.ru/a/',
                    'status': 200, 'content_type': 'text/html', 'bytes': 100,
                    'body': GOOD, 'error': None, 'elapsed_ms': 5}, HOST)
    check('редирект зафиксирован', http['redirected'], True)
    check('вид редиректа — схема', http['redirect_kind'], 'scheme')
    check('редирект схемы = warn', 'REDIRECT_SCHEME' in codes(http), True)

    moved = analyze({'url': 'https://zavodsvay.ru/old-page/', 'final_url': 'https://zavodsvay.ru/new-page/',
                     'status': 200, 'content_type': 'text/html', 'bytes': 100,
                     'body': GOOD, 'error': None, 'elapsed_ms': 5}, HOST)
    check('редирект на другой путь', moved['redirect_kind'], 'path')
    check('редирект пути = info', moved['issues'][-1]['level'], 'info')

    check('404', codes(analyze(res('https://zavodsvay.ru/gone/', '', status=404,
                                   ctype=''), HOST)), {'HTTP_STATUS'})
    check('недоступен', codes(analyze(res('https://zavodsvay.ru/x/', '', status=0,
                                         ctype='',
                                         error='TimeoutError: timed out'),
                                      HOST)), {'FETCH_FAILED'})

    old = {'generated': '2026-01-01', 'pages': [
        {'url': 'https://zavodsvay.ru/a/', 'status': 200, 'title_len': 10,
         'word_count': 100, 'indexable': True, 'issues': []},
        {'url': 'https://zavodsvay.ru/old/', 'status': 200, 'issues': []}],
        'summary': {'issues_by_code': {'NO_TITLE': 5, 'IMG_NO_ALT': 2}, 'ok': 2,
                    'truncated': True, 'discovered': 500}}
    new = {'generated': '2026-02-01', 'pages': [
        {'url': 'https://zavodsvay.ru/a', 'status': 200, 'title_len': 60,
         'word_count': 100, 'indexable': True, 'issues': []},
        {'url': 'https://zavodsvay.ru/new/', 'status': 200, 'issues': []}],
        'summary': {'issues_by_code': {'NO_TITLE': 1, 'IMG_NO_ALT': 2}, 'ok': 2,
                    'truncated': False}}
    d = compare(old, new)
    check('diff: добавленных', d['added'], ['https://zavodsvay.ru/new/'])
    check('diff: удалённых', d['removed'], ['https://zavodsvay.ru/old/'])
    check('diff: срез и полный прогон помечаются ненадёжными',
          (old['summary'].get('truncated'), new['summary'].get('truncated'), d['reliable']),
          (True, False, False))
    check('diff: причина указана', 'усечён' in (d['unreliable_reason'] or ''), True)
    check('diff без усечения надёжен', compare(new, new)['reliable'], True)
    check('diff: /a/ и /a — одна страница, изменён title_len', d['metric_changes'],
          [{'url': 'https://zavodsvay.ru/a', 'field': 'title_len', 'from': 10, 'to': 60}])
    check('diff: счётчики кодов', d['issue_counts'],
          [{'code': 'NO_TITLE', 'from': 5, 'to': 1, 'delta': -4}])

    if fails:
        print(f'FAIL ({len(fails)}):')
        for f in fails:
            print('  ' + f)
        sys.exit(1)
    print('OK — все проверки прошли')


if __name__ == '__main__':
    main()
