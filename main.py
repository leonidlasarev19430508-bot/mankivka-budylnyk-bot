import os, json, logging
import requests
import feedparser
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
import pytz

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger(__name__)

BOT_TOKEN   = os.environ['BOT_TOKEN']
CHANNEL_ID  = os.environ['CHANNEL_ID']
GROQ_KEY    = os.environ['GROQ_API_KEY']
OWM_KEY     = os.environ['OWM_API_KEY']
KYIV        = pytz.timezone('Europe/Kyiv')
CHANNEL_URL = "https://t.me/Mankivka8am"

MONTHS_UA = {
    1:'січня', 2:'лютого', 3:'березня', 4:'квітня',
    5:'травня', 6:'червня', 7:'липня', 8:'серпня',
    9:'вересня', 10:'жовтня', 11:'листопада', 12:'грудня'
}

RSS_FEEDS = [
    ('УП',        'https://www.pravda.com.ua/rss/'),
    ('BBC',       'https://www.bbc.com/ukrainian/index.xml'),
    ('Суспільне', 'https://suspilne.media/rss/news.xml'),
    ('Ліга',      'https://www.liga.net/news/all/rss.xml'),
    ('ЕП',        'https://www.epravda.com.ua/rss/'),
]

# RSS-стрічки для місцевих новин: Уманщина + Черкащина
LOCAL_RSS_FEEDS = [
    # Суспільне Черкаси — регіональне ТБ/радіо
    'https://suspilne.media/cherkasy/rss/news.xml',
    # Google News: Уманський район
    'https://news.google.com/rss/search?q=Уманський+район&hl=uk&gl=UA&ceid=UA:uk',
    # Google News: Маньківка
    'https://news.google.com/rss/search?q=Маньківка+Черкаська&hl=uk&gl=UA&ceid=UA:uk',
    # Google News: Умань новини
    'https://news.google.com/rss/search?q=Умань+новини&hl=uk&gl=UA&ceid=UA:uk',
    # Google News: Черкаська область
    'https://news.google.com/rss/search?q=Черкаська+область&hl=uk&gl=UA&ceid=UA:uk',
]

# Сайти для скрапінгу (без RSS)
LOCAL_SCRAPE_SOURCES = [
    {
        'name': 'Умань24',
        'url': 'https://uman24.org.ua/news/',
        'item_selector': 'article',
        'title_selector': 'h2,h3',
        'link_attr': 'a',
    },
    {
        'name': 'УманьNews',
        'url': 'https://umannews.city/',
        'item_selector': 'article',
        'title_selector': 'h2,h3',
        'link_attr': 'a',
    },
]

NUM_EMOJI = ['1️⃣','2️⃣','3️⃣','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣']

def _t(val):
    v = round(val)
    return f"+{v}°C" if v >= 0 else f"{v}°C"

def dedup(items):
    seen = set()
    result = []
    for item in items:
        key = item['title'].strip().lower()[:60]
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result

def fetch_weather():
    url = (f"https://api.openweathermap.org/data/2.5/forecast"
           f"?lat=49.02&lon=30.31&appid={OWM_KEY}&units=metric&lang=ua&cnt=8")
    data = requests.get(url, timeout=8).json()
    fc = data['list']
    temp_min = min(f['main']['temp_min'] for f in fc)
    temp_max = max(f['main']['temp_max'] for f in fc)
    wind_min = min(f['wind']['speed'] for f in fc)
    wind_max = max(f['wind']['speed'] for f in fc)
    s = f"{_t(temp_min)}..{_t(temp_max)}, вітер {round(wind_min)}-{round(wind_max)} м/с"
    for f in fc:
        if f.get('pop', 0) >= 0.4:
            hour = datetime.fromtimestamp(f['dt'], tz=KYIV).strftime('%H:%M')
            s += f". Після {hour} можливий дощ"
            break
    return s

def fetch_currencies():
    lines = []
    try:
        nbu = requests.get("https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange?json", timeout=8).json()
        usd = next((x['rate'] for x in nbu if x['cc'] == 'USD'), None)
        eur = next((x['rate'] for x in nbu if x['cc'] == 'EUR'), None)
        if usd and eur:
            lines.append(f"💱 <b>Курс НБУ:</b> $ {usd:.2f} | € {eur:.2f}")
    except Exception as e:
        log.warning("НБУ: %s", e)
    cash = []
    try:
        pb = requests.get("https://api.privatbank.ua/p24api/pubinfo?json&exchange&coursid=5", timeout=8).json()
        for x in pb:
            if x['ccy'] == 'USD': cash.append(f"$ {float(x['sale']):.2f}")
            elif x['ccy'] == 'EUR': cash.append(f"€ {float(x['sale']):.2f}")
    except Exception as e:
        log.warning("PrivatBank: %s", e)
    try:
        btc = requests.get("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd", timeout=8).json()
        cash.append(f"₿ ${int(btc['bitcoin']['usd']):,}")
    except Exception as e:
        log.warning("BTC: %s", e)
    if cash:
        lines.append(f"💵 <b>Готівка:</b> {' | '.join(cash)}")
    return '\n'.join(lines)

def fetch_national_news():
    items = []
    for src, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:10]:
                t = e.get('title', '').strip()
                l = e.get('link', '').strip()
                if t and l:
                    items.append({'title': t, 'link': l, 'source': 'national'})
        except Exception as ex:
            log.warning("RSS %s: %s", src, ex)
    return items

def scrape_local_site(source):
    """Скрапінг новин з сайтів які не мають RSS"""
    items = []
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (compatible; NewsBot/1.0)'}
        resp = requests.get(source['url'], timeout=8, headers=headers)
        soup = BeautifulSoup(resp.text, 'html.parser')
        articles = soup.select(source['item_selector'])[:10]
        seen = set()
        for art in articles:
            title_tag = art.select_one(source['title_selector'])
            link_tag = art.select_one(source['link_attr'])
            if not title_tag or not link_tag:
                continue
            t = title_tag.get_text(strip=True)
            href = link_tag.get('href', '')
            if not href.startswith('http'):
                base = source['url'].rstrip('/')
                href = base + '/' + href.lstrip('/')
            key = t.lower()[:60]
            if t and href and key not in seen:
                seen.add(key)
                items.append({'title': t, 'link': href, 'source': 'local'})
    except Exception as e:
        log.warning("Scrape %s: %s", source['name'], e)
    return items

def fetch_local_news():
    """Збирає місцеві новини з RSS + скрапінгу"""
    items = []
    seen = set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=3)

    # 1. RSS джерела
    for url in LOCAL_RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for e in feed.entries[:8]:
                t = e.get('title', '').strip()
                l = e.get('link', '').strip()
                pub = e.get('published_parsed')

                if not t or not l:
                    continue
                key = t.lower()[:60]
                if key in seen:
                    continue
                if pub:
                    pub_dt = datetime(*pub[:6], tzinfo=timezone.utc)
                    if pub_dt < cutoff:
                        continue
                seen.add(key)
                items.append({'title': t, 'link': l, 'source': 'local'})
        except Exception as ex:
            log.warning("Local RSS %s: %s", url, ex)

    # 2. Скрапінг сайтів без RSS
    for source in LOCAL_SCRAPE_SOURCES:
        scraped = scrape_local_site(source)
        for item in scraped:
            key = item['title'].lower()[:60]
            if key not in seen:
                seen.add(key)
                items.append(item)

    log.info("Місцеві новини знайдено: %d", len(items))
    return items[:6]

SYSTEM_PROMPT = (
    "Ти редактор Telegram-каналу «Маньківка 8:00».\n"
    "З наданих новин відбери 3-5 національних і до 2-3 місцевих (якщо є). "
    "Місцеві — це новини про Маньківку, Уманський район, Умань або Черкаську область. "
    "Для кожної: короткий заголовок (до 10 слів) + 2-4 речення суті.\n"
    "Відповідай ТІЛЬКИ валідним JSON без markdown:\n"
    '{"national":[{"title":"...","summary":"...","link":"..."}],"local":[...]}'
)

def ai_summarize(items):
    resp = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
        headers={"Authorization": f"Bearer {GROQ_KEY}"},
        json={
            "model": "llama3-70b-8192",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
            ],
            "max_tokens": 1500,
            "temperature": 0.3,
        },
        timeout=60,
    ).json()
    raw = resp['choices'][0]['message']['content'].strip()
    if '```' in raw:
        parts = raw.split('```')
        for part in parts:
            part = part.lstrip('json').strip()
            if part.startswith('{'):
                raw = part
                break
    return json.loads(raw)

def build_message(now, weather, currencies, digest):
    date_str = f"{now.day} {MONTHS_UA[now.month]}"
    time_str = now.strftime('%H:%M')
    lines = [f"🌅 <b>Маньківка {time_str} | Будильник новин на {date_str}</b>\n"]
    counter = 0
    if digest.get('national'):
        lines.append("🌍 <b>В УКРАЇНІ</b>")
        for item in digest['national']:
            n = NUM_EMOJI[counter] if counter < len(NUM_EMOJI) else f"{counter+1}."
            summary = item.get('summary', '').strip()
            link = item.get('link', '')
            read = f' <a href="{link}">Читати</a>' if link else ''
            lines.append(f"{n} <b>{item['title']}</b>\n{summary}{read}")
            counter += 1
    if digest.get('local'):
        lines.append("\n🏡 <b>У ГРОМАДІ ТА НА УМАНЩИНІ</b>")
        for item in digest['local']:
            n = NUM_EMOJI[counter] if counter < len(NUM_EMOJI) else f"{counter+1}."
            summary = item.get('summary', '').strip()
            link = item.get('link', '')
            read = f' <a href="{link}">Читати</a>' if link else ''
            lines.append(f"{n} <b>{item['title']}</b>\n{summary}{read}")
            counter += 1
    lines.append("\n📊 <b>КОРИСНО ЗНАТИ</b>")
    if weather:
        lines.append(f"🌡️ <b>Погода в Маньківці:</b> {weather}")
    if currencies:
        lines.append(currencies)
    return '\n'.join(lines)

def send_digest():
    now = datetime.now(KYIV)
    log.info("Digest started: %s", now.strftime('%H:%M %d.%m.%Y'))

    weather = ""
    try:
        weather = fetch_weather()
    except Exception as e:
        log.error("Weather: %s", e)

    currencies = ""
    try:
        currencies = fetch_currencies()
    except Exception as e:
        log.error("Currencies: %s", e)

    local = fetch_local_news()
    national = fetch_national_news()
    all_news = dedup(local + national)

    if not all_news:
        log.warning("No news, skipping.")
        return

    try:
        digest = ai_summarize(all_news[:25])
    except Exception as e:
        log.error("AI failed: %s", e)
        nat = [n for n in all_news if n['source'] == 'national']
        loc = [n for n in all_news if n['source'] == 'local']
        digest = {
            "national": [{"title": n['title'], "summary": "", "link": n['link']} for n in nat[:5]],
            "local": [{"title": n['title'], "summary": "", "link": n['link']} for n in loc[:3]],
        }

    text = build_message(now, weather, currencies, digest)

    resp = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json={
            "chat_id": CHANNEL_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "reply_markup": {"inline_keyboard": [[
                {"text": "📢 Підписатись на Будильник", "url": CHANNEL_URL}
            ]]},
        },
        timeout=15,
    )
    if resp.ok:
        log.info("Digest sent OK")
    else:
        log.error("Telegram %s: %s", resp.status_code, resp.text)

if __name__ == '__main__':
    send_digest()
