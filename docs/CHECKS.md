# Проверки и пороги

> Проект: SEO Auditor · Обновлено: 2026-09-30
> Уровни: `error` — ломает индексацию или доступность страницы, `warn` — вредит
> ранжированию или UX в выдаче, `info` — стоит поправить, на позиции не влияет.

## Страница (`analyze`)

| Код | Уровень | Условие | Что делать |
|-----|---------|----------|------------|
| `FETCH_FAILED` | error | сеть/таймаут, страница не отдана | проверить доступность и редиректы |
| `HTTP_STATUS` | error | HTTP ≥ 400 | закрыть 404/редиректом, убрать из sitemap и меню |
| `NO_TITLE` | error | нет `<title>` | title с ключевым запросом в начало |
| `NO_H1` | error | нет `<h1>` | один H1 с основным запросом страницы |
| `NOINDEX` | error | `meta robots` с `noindex` | снять, если страница должна быть в выдаче |
| `MIXED_CONTENT` | error | http-ресурс на https-странице | перевести на https, иначе браузер блокирует |
| `SCHEMA_INVALID` | error | JSON-LD не парсится | синтаксис в `script ld+json` — частая причина незакрытые кавычки в значениях |
| `TITLE_LONG` | warn | title > 70 символов | сократить до 50-70, риск обрезки в выдаче |
| `DESC_LONG` | warn | description > 180 символов | сократить до 150-180 |
| `NO_DESCRIPTION` | warn | нет `meta description` | 100-180 символов с призывом и ключевым запросом |
| `MULTI_H1` | warn | H1 больше одного | оставить один, остальные понизить до H2 |
| `HEADING_ORDER` | warn | пропущен уровень (H1 → H3) | выстроить H1 → H2 → H3 без пропусков |
| `NO_CANONICAL` | warn | нет `link rel=canonical` | canonical на саму себя |
| `CANONICAL_OTHER` | warn | canonical ведёт на другой URL | проверить намерение: дубли могут уйти из индекса |
| `NO_VIEWPORT` | warn | нет `meta viewport` | `width=device-width`, влияет на мобильную выдачу |
| `HTTP_REFRESH` | warn | `http-equiv refresh` | заменить на серверный 301/302 |
| `REDIRECT_SCHEME` | warn | адрес в обходе http, отдаётся https | выдать конечный адрес в sitemap и ссылках |
| `SEO_URL` | warn | кириллица, `_` или длина > 75 | транслит, без подчёркиваний, короче |
| `THIN_CONTENT` | warn | < 300 слов | расширить или объединить со страницей-приёмником |
| `IMG_NO_ALT` | warn | у контентной картинки нет атрибута `alt` | заполнить; `alt=""` допустим, счётчики аналитики исключены |
| `NO_INTERNAL_LINKS` | warn | нет исходящих внутренних ссылок | ссылки на связанные страницы из меню/блока |
| `MANY_SCRIPTS` | warn | ≥ 15 внешних скриптов | свести к 5-7, инлайнить мелкие, отложить |
| `EMPTY_ANCHOR` | warn | > 2 ссылки без анкора, картинки и `aria-label` | описательные анкоры или `aria-label` |
| `NO_LANG` | info | нет `lang` на `<html>` | `lang="ru"` |
| `NO_OG` | info | нет Open Graph | не влияет на выдачу Яндекса, нужно для соцсетей |
| `NO_SCHEMA` | info | нет JSON-LD (не выставляется, если JSON-LD сломан) | Organization/LocalBusiness + BreadcrumbList + WebSite |
| `DEAD_SCHEMA` | info | типы без rich results: FAQPage, HowTo, SpecialAnnouncement, ClaimReview, EstimatedSalary, LearningVideo, CourseInfo | оставить ради семантики или удалить |
| `ICON_ONLY_LINKS` | info | > 5 ссылок-иконок без `aria-label` | доступность и скринридеры |
| `REDIRECT` | info | редирект на другой путь | убедиться, что одношаговый и 301 |

## Сайт (`site_report`)

| Код | Уровень | Условие |
|-----|---------|----------|
| `ROBOTS_DISALLOW_ALL` | error | `Disallow: /` — сайт закрыт роботам |
| `BROKEN_PAGES` | error | есть URL с 4xx/5xx |
| `SITEMAP_ERROR` | error | sitemap недоступен или невалидный XML |
| `NO_ROBOTS` | error | robots.txt отдаёт 404 |
| `ROBOTS_NO_SITEMAP` | warn | нет директивы `Sitemap:` в robots.txt |
| `NO_SITEMAP_URLS` | warn | sitemap не отдал ни одного URL |
| `MANY_REDIRECTS` | warn | > 10% URL редиректят |
| `DUP_TITLE` / `DUP_DESCRIPTION` / `DUP_H1` | warn | одинаковые значения на разных страницах |
| `ORPHAN_PAGES` | warn | страницы без входящих внутренних ссылок |
| `NOT_IN_SITEMAP` | info | страницы, которых нет в sitemap |
| `SITEMAP_SPLIT` | info | файл sitemap больше 50 000 URL — лимит Яндекса |

## Пороги в коде

| Константа | Значение | Обоснование |
|-----------|----------|-------------|
| `TITLE_MAX` | 70 | выше — риск обрезки сниппета |
| `DESC_MAX` | 180 | выше — описание обрезается |
| `THIN_WORDS` | 300 | ниже — страница считается тонкой |
| `YANDEX_URL_MAX` | 75 | длинные транслитовые URL хуже читаются и обрезаются |
| `MANY_SCRIPTS` | 15 | выше — заметное падение скорости на мобильных |
| `EMPTY_ANCHOR` | 2 | 1-2 пустые ссылки — норма (обёртка логотипа) |
| `WORKERS` / `DELAY` | 4 / 0.7 с | `techdebt`: `Crawl-delay` из robots.txt не читается |

## Что НЕ проверяет

Осознанные пробелы, чтобы не тратить время на ложную полноту:

- **Core Web Vitals** — только через `--psi` (field data CrUX). Без флага метрик нет.
- **Внешние битые ссылки** — `broken_links` как у DataForSEO не проверяются: это отдельный обход сети, дорогой и шумный.
- **Дубли контента между страницами** — считаются только дубли title/description/H1. Семантическое сравнение текста не делается.
- **Скорость и вес ресурсов** — байты страницы есть, но разбор и склейка CSS/JS не делаются.
- **Индексация в Яндексе** — только свой сайт через Вебмастер API, это зона `SEO-Zavodsvay`.
