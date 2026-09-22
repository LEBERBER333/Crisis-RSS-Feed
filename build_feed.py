"""
Merge every feed in feeds.txt into one RSS feed (feed.xml) and a
readable web page (index.html), written to the _site/ folder.

Run by GitHub Actions every hour. You shouldn't need to edit this file;
change feeds.txt instead, or the settings just below.
"""
import os
import re
import html
import socket
import calendar
import datetime as dt
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlparse
from xml.sax.saxutils import escape

import feedparser

# ---- Settings you can change -------------------------------------------
FEED_TITLE = "Crisis Comms, Reputation & Marketing Daily"
FEED_DESCRIPTION = "Crisis communications, reputation and marketing news from the past day."
HOURS_TO_KEEP = 36        # how far back articles are kept
MAX_ITEMS = 400           # cap on articles in the combined feed
# ------------------------------------------------------------------------

SITE_URL = os.environ.get("SITE_URL", "").rstrip("/") + "/"
OUT = Path("_site")
socket.setdefaulttimeout(25)
AGENT = "Mozilla/5.0 (compatible; CombinedFeedBot/1.0; +https://github.com)"


def load_feeds(path="feeds.txt"):
    feeds = []
    for raw in Path(path).read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|", 2)]
        if len(parts) != 3 or not parts[2].startswith("http"):
            print(f"  ! Skipping line that isn't 'Category | Name | URL': {line}")
            continue
        feeds.append(tuple(parts))
    return feeds


def clean_text(s, limit=300):
    s = re.sub(r"<[^>]+>", " ", s or "")
    s = html.unescape(re.sub(r"\s+", " ", s)).strip()
    return (s[: limit - 1] + "…") if len(s) > limit else s


def title_key(title):
    # "Brand X faces backlash - Adweek" and "Brand X faces backlash" count as the same story
    t = re.sub(r"\s+[-–|]\s+[^-–|]{2,60}$", "", title.lower())
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def main():
    feeds = load_feeds()
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=HOURS_TO_KEEP)
    items, seen_links, seen_titles = [], set(), set()
    ok, failed = 0, []

    for category, name, url in feeds:
        parsed = feedparser.parse(url, agent=AGENT)
        if parsed.get("bozo") and not parsed.entries:
            failed.append(name)
            print(f"  x {name}: could not read feed ({parsed.get('bozo_exception')})")
            continue
        ok += 1
        is_search = "news.google.com" in urlparse(url).netloc
        added = 0
        for e in parsed.entries:
            when = e.get("published_parsed") or e.get("updated_parsed")
            if not when:
                continue
            published = dt.datetime.fromtimestamp(calendar.timegm(when), dt.timezone.utc)
            if published < cutoff:
                continue
            link = (e.get("link") or "").strip()
            title = clean_text(e.get("title"), 250)
            if not link or not title:
                continue
            key = title_key(title)
            if link in seen_links or key in seen_titles:
                continue
            seen_links.add(link)
            seen_titles.add(key)
            # Trade-publication titles get their source added; Google News titles already have it
            if not is_search and not title.endswith(name):
                title = f"{title} - {name}"
            summary = "" if is_search else clean_text(e.get("summary"))
            items.append(dict(title=title, link=link, date=published, category=category,
                              source=name, source_url=url, summary=summary))
            added += 1
        print(f"  ✓ {name}: {added} new articles")

    items.sort(key=lambda i: i["date"], reverse=True)
    items = items[:MAX_ITEMS]
    now = dt.datetime.now(dt.timezone.utc)
    OUT.mkdir(exist_ok=True)
    write_rss(items, now)
    write_html(items, now, ok, len(feeds), failed)
    print(f"\nDone: {len(items)} articles from {ok}/{len(feeds)} sources.")
    if failed:
        print("Sources that failed this run: " + ", ".join(failed))


def write_rss(items, now):
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">', "<channel>",
           f"<title>{escape(FEED_TITLE)}</title>",
           f"<link>{escape(SITE_URL)}</link>",
           f"<description>{escape(FEED_DESCRIPTION)}</description>",
           f'<atom:link href="{escape(SITE_URL)}feed.xml" rel="self" type="application/rss+xml"/>',
           f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
           "<ttl>60</ttl>"]
    for i in items:
        out += ["<item>",
                f"<title>{escape(i['title'])}</title>",
                f"<link>{escape(i['link'])}</link>",
                f'<guid isPermaLink="false">{escape(i["link"])}</guid>',
                f"<pubDate>{format_datetime(i['date'])}</pubDate>",
                f"<category>{escape(i['category'])}</category>",
                f'<source url="{escape(i["source_url"], {chr(34): "&quot;"})}">{escape(i["source"])}</source>']
        if i["summary"]:
            out.append(f"<description>{escape(i['summary'])}</description>")
        out.append("</item>")
    out += ["</channel>", "</rss>"]
    (OUT / "feed.xml").write_text("\n".join(out), encoding="utf-8")


def write_html(items, now, ok, total, failed):
    cats = sorted({i["category"] for i in items})
    chips = "".join(f'<button type="button" data-cat="{html.escape(c)}">{html.escape(c)}</button>' for c in cats)
    rows, last_day = [], None
    for i in items:
        day = i["date"].strftime("%A %-d %B")
        if day != last_day:
            rows.append(f'<h2 class="day">{day}</h2>')
            last_day = day
        summary = f'<p class="sum">{html.escape(i["summary"])}</p>' if i["summary"] else ""
        rows.append(
            f'<article data-cat="{html.escape(i["category"])}">'
            f'<a href="{html.escape(i["link"])}" target="_blank" rel="noopener">{html.escape(i["title"])}</a>'
            f'{summary}<p class="meta"><span class="cat">{html.escape(i["category"])}</span>'
            f'<time datetime="{i["date"].isoformat()}">{i["date"].strftime("%H:%M")} UTC</time></p></article>')
    if not rows:
        rows.append('<p class="empty">No articles in the past day yet. The feed refreshes every hour.</p>')
    status = f"{ok} of {total} sources updated"
    if failed:
        status += f"; not reachable this hour: {', '.join(failed)}"
    page = TEMPLATE.format(title=html.escape(FEED_TITLE), feed=html.escape(SITE_URL + "feed.xml"),
                           updated=now.strftime("%-d %b %Y, %H:%M UTC"), count=len(items),
                           chips=chips, rows="\n".join(rows), status=html.escape(status))
    (OUT / "index.html").write_text(page, encoding="utf-8")


TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<link rel="alternate" type="application/rss+xml" title="{title}" href="feed.xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Source+Serif+4:opsz,wght@8..60,400;8..60,600&family=Public+Sans:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{{--bg:#f3f5f8;--ink:#17202e;--muted:#5a6475;--line:#d6dce5;--accent:#1f4fd1;--chip:#e3e8f0}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121820;--ink:#e6eaf0;--muted:#98a2b3;--line:#2a3441;--accent:#7ea2ff;--chip:#1e2733}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 "Public Sans",system-ui,sans-serif}}
main{{max-width:46rem;margin:0 auto;padding:2.5rem 1.25rem 4rem}}
h1{{font:600 clamp(1.7rem,4vw,2.4rem)/1.15 "Source Serif 4",Georgia,serif;margin:0 0 .5rem}}
.lede{{color:var(--muted);margin:0 0 1.25rem}}
.lede a{{color:var(--accent)}}
.subscribe{{display:flex;gap:.5rem;flex-wrap:wrap;margin-bottom:1.5rem}}
.subscribe input{{flex:1 1 16rem;font:inherit;font-size:.9rem;padding:.5rem .7rem;border:1px solid var(--line);border-radius:6px;background:transparent;color:var(--ink)}}
button{{font:inherit;font-size:.9rem;cursor:pointer;border:1px solid var(--line);background:var(--chip);color:var(--ink);padding:.45rem .8rem;border-radius:999px}}
button[aria-pressed=true]{{background:var(--accent);border-color:var(--accent);color:#fff}}
button:focus-visible,a:focus-visible,input:focus-visible{{outline:2px solid var(--accent);outline-offset:2px}}
.chips{{display:flex;gap:.4rem;flex-wrap:wrap;margin-bottom:1rem}}
.day{{font:600 1.15rem "Source Serif 4",Georgia,serif;margin:2rem 0 .25rem;padding-bottom:.4rem;border-bottom:2px solid var(--ink)}}
article{{padding:.85rem 0;border-bottom:1px solid var(--line)}}
article a{{color:var(--ink);font-weight:600;text-decoration:none}}
article a:hover{{color:var(--accent);text-decoration:underline}}
.sum{{margin:.3rem 0 0;color:var(--muted);font-size:.93rem}}
.meta{{margin:.35rem 0 0;font-size:.8rem;color:var(--muted);display:flex;gap:.75rem}}
.cat{{color:var(--accent)}}
footer,.empty{{color:var(--muted);font-size:.85rem;margin-top:2rem}}
</style></head>
<body><main>
<h1>{title}</h1>
<p class="lede">{count} articles from the past day. Updated {updated}.</p>
<div class="subscribe"><input id="u" readonly value="{feed}" aria-label="Feed link">
<button type="button" id="copy">Copy feed link</button></div>
<div class="chips" role="group" aria-label="Filter by category"><button type="button" data-cat="" aria-pressed="true">All</button>{chips}</div>
{rows}
<footer>{status}. Paste the feed link into Feedly, Inoreader, Outlook or Slack to subscribe.</footer>
</main>
<script>
document.getElementById('copy').onclick=async e=>{{const i=document.getElementById('u');try{{await navigator.clipboard.writeText(i.value)}}catch(_){{i.select();document.execCommand('copy')}}e.target.textContent='Copied'}};
const chips=[...document.querySelectorAll('.chips button')];
chips.forEach(b=>b.onclick=()=>{{chips.forEach(c=>c.setAttribute('aria-pressed',c===b));const cat=b.dataset.cat;
document.querySelectorAll('article').forEach(a=>a.hidden=cat&&a.dataset.cat!==cat);
document.querySelectorAll('.day').forEach(h=>{{let n=h.nextElementSibling,any=false;while(n&&n.tagName==='ARTICLE'){{if(!n.hidden)any=true;n=n.nextElementSibling}}h.hidden=!any}})}});
</script>
</body></html>"""


if __name__ == "__main__":
    main()
