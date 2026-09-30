<p align="center">
  <a href="https://www.python.org/"><img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-3776AB?logo=Python&logoColor=white"></a>
  <a href="https://opensource.org/licenses/MIT"><img alt="License" src="https://img.shields.io/badge/License-MIT-blue.svg"></a>
  <a href="https://yandex.ru/"><img alt="Yandex SEO" src="https://img.shields.io/badge/%D0%A7%D0%B5%D0%BA%D0%B8-%D0%AF%D0%BD%D0%B4%D0%B5%D0%BA%D0%BC1-red.svg"></a>
</p>

<h1 align="center">SEO Auditor</h1>
<p align="center">On-page аудит сайта со снимками и diff прогонов — под Яндекс, Google и Bing</p>

---

Zero-dependency CLI на стандартной библиотеке Python. Обходит сайт, разбирает HTML, проверяет ~40 on-page условий и складывает снимок в JSON. Главная ценность — **diff двух прогонов**: «до и после» миграции, деплоя или правки — с отчётом, что именно сломалось.

Чек-лист разделён на два слоя: **базовый** (title, description, canonical, meta robots, иерархия заголовков, alt, robots.txt, sitemap, дубли, Core Web Vitals, hreflang — трактуется одинаково у всех поисковиков) и **слой специфики Яндекса** (кириллица в URL, типы Schema без rich results, длины под обрезку в сниппете). Bing-специфика добавлялась бы третьим слоем.

Аналог на GitHub (`AgriciDaniel/on-page-seo`) — обёртка над платными Firecrawl + DataForSEO. Здесь вместо них собственный краулер: без внешних API, без зарубежной оплаты картой РФ, с CWV из field data CrUX.

- **Zero dependencies** — только `urllib` и `html.parser`, ничего ставить не нужно.
- **Свой чек-лист** — title, description, canonical, meta robots, H1-H6, JSON-LD, alt, mixed content, редиректы, thin content, битые URL, hreflang.
- **Уровни сайта** — robots.txt, sitemap, дубли title/description/H1, сироты, покрытие sitemap, https.
- **Снимки + diff** — история по датам, отчёт о дельте метрик и счётчиков проблем.
- **Снимки по хостам** — сеть из нескольких доменов не перетирает друг друга.
- **Core Web Vitals** — LCP/INP/CLS из field data CrUX (75-й перцентиль реальных пользователей), не Labs. Метрика кросс-поисковиковая, требует ключ `PSI_API_KEY`.
- **Самопроверка** — `python onpage_selfcheck.py`, без фреймворков и фикстур на диске.

## Использование

```bash
# один сайт
python onpage_audit.py https://example.ru --limit 200

# сеть доменов из файла + сравнение с предыдущим прогоном
python onpage_audit.py --all sites.txt --limit 200 --diff

# после деплоя: посмотреть, что именно сломалось
python onpage_audit.py https://example.ru --limit 200 --diff

# diff двух произвольных снимков, без обхода
python onpage_audit.py --compare audits/example.ru/onpage-A.json audits/example.ru/onpage-B.json

# Core Web Vitals (нужен ключ PSI_API_KEY) и свой список URL
python onpage_audit.py --all sites.txt --psi
python onpage_audit.py https://example.ru --urls data/urls.txt
```

`sites.txt` — по одному домену в строке, без `https://`, строки с `#` игнорируются.
После `#` можно задать настройки домена через пробел: `thin-words`, `limit`, `psi`.
Так порог тонкого контента не приходится держать в голове между прогонами.

Каждый домен обходится отдельно, снимки изолированы по хостам, в конце печатается
сводная таблица и — при `--diff` — регрессии, то есть проблемы, которых стало больше.
Это первое, на что стоит смотреть.

Столбец `покрытие` показывает `проверено/найдено`; `!` означает усечение по `--limit`.
Столбец `ист` — источник URL: `sitemap` — полное покрытие, `bfs` — sitemap недоступен,
обход с главной по ссылкам, покрытие неполное, нужен свой список через `--urls`.

Если один из снимков был усечён, diff помечается ненадёжным и регрессии не
показываются: сравнивать 30 страниц с 575 — бессмысленно, и выглядит как катастрофа.

## Опции

| Флаг | Описание | Default |
|------|----------|---------|
| `--all FILE` | прогон по списку доменов + сводка по сети | — |
| `--limit N` | максимум URL на сайт; 0 = без усечения | 0 |
| `--thin-words N` | порог `THIN_CONTENT`, калибруется на домен | 300 |
| `--urls FILE` | свой список URL одного сайта | — |
| `--diff` | сравнить с предыдущим снимком | off |
| `--compare OLD NEW` | diff двух файлов, без обхода | — |
| `--psi` | добавить Core Web Vitals (нужен `PSI_API_KEY`) | off |
| `--psi-strategy` | `mobile` или `desktop` | mobile |
| `--out FILE` | свой путь к снимку (только для одного сайта) | `audits/<host>/onpage-<date>.json` |

## Проверка

```bash
python onpage_selfcheck.py
```

## Документация

- [`docs/CONTEXT.md`](docs/CONTEXT.md) — состояние проекта
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — архитектурные решения
- [`docs/CHECKS.md`](docs/CHECKS.md) — полный список проверок и порогов

## Статус

**v0.1.0** — рабочий аудит и diff, self-check зелёный, верифицирован на zavodsvay.ru (40 URL из sitemap).

## Лицензия

[MIT](LICENSE) © Alexander Kuzikov
