import os
import time
import schedule
import requests
import feedparser
import anthropic
from supabase import create_client
from datetime import datetime, timezone
import json
import re
import hashlib

# ── Config & startup validation ───────────────────────────────────────────
REQUIRED_ENV = ['ANTHROPIC_API_KEY', 'SUPABASE_URL', 'SUPABASE_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']
for key in REQUIRED_ENV:
    if not os.environ.get(key):
        raise RuntimeError(f'Missing required env var: {key}')

ANTHROPIC_KEY = os.environ['ANTHROPIC_API_KEY']
SUPABASE_URL = os.environ['SUPABASE_URL']
SUPABASE_KEY = os.environ['SUPABASE_KEY']
TG_TOKEN = os.environ['TELEGRAM_BOT_TOKEN']
TG_CHAT = os.environ['TELEGRAM_CHAT_ID']
EIA_KEY = os.environ.get('EIA_API_KEY', '')
SIS_SUPABASE_URL = os.environ.get('SIS_SUPABASE_URL', '')
SIS_SUPABASE_KEY = os.environ.get('SIS_SUPABASE_KEY', '')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

GAMMA_API = 'https://gamma-api.polymarket.com'

# ── Scan lock — prevent overlapping runs ──────────────────────────────────
_is_running = False
_is_running_b = False

# ── Requests session with retries ─────────────────────────────────────────
session = requests.Session()
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retry))
session.mount('http://', HTTPAdapter(max_retries=retry))

def now_utc():
    return datetime.now(timezone.utc)

def now_utc_iso():
    return now_utc().isoformat()

# ── RSS Sources ───────────────────────────────────────────────────────────
RSS_FEEDS = [
    ('Reuters World', 'https://feeds.reuters.com/reuters/worldNews'),
    ('AP News', 'https://rsshub.app/apnews/topics/apf-intlnews'),
    ('BBC World', 'http://feeds.bbci.co.uk/news/world/rss.xml'),
    ('Al Jazeera', 'https://www.aljazeera.com/xml/rss/all.xml'),
    ('Middle East Eye', 'https://www.middleeasteye.net/rss'),
    ('Iran International', 'https://www.iranintl.com/en/rss'),
    ('Defense One', 'https://www.defenseone.com/rss/all/'),
    ('Politico', 'https://rss.politico.com/politics-news.xml'),
    ('UN News', 'https://news.un.org/feed/subscribe/en/news/all/rss.xml'),
    ('State Department', 'https://www.state.gov/rss-feeds/press-releases/'),
    ('TechCrunch', 'https://techcrunch.com/feed/'),
    ('The Verge', 'https://www.theverge.com/rss/index.xml'),
]

# ── Excluded market keywords — Scanner B will skip these ──────────────────
EXCLUDED_KEYWORDS = [
    'election', 'vote', 'ballot', 'federal reserve', 'fed rate', 'interest rate',
    'nfl', 'nba', 'mlb', 'nhl', 'soccer', 'football', 'basketball', 'baseball',
    'hockey', 'tennis', 'golf', 'formula 1', 'f1', 'ufc', 'boxing', 'wrestling',
    'valorant', 'esport', 'dota', 'league of legends', 'cs2', 'overwatch',
    'oscar', 'grammy', 'emmy', 'golden globe', 'academy award',
]

def is_excluded_market(question):
    q = question.lower()
    return any(kw in q for kw in EXCLUDED_KEYWORDS)

# ── Fetch Polymarket markets ───────────────────────────────────────────────
def get_active_markets(limit=100):
    try:
        r = session.get(f'{GAMMA_API}/markets', params={
            'active': 'true',
            'closed': 'false',
            'limit': limit,
            'order': 'volume',
            'ascending': 'false'
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        markets = data if isinstance(data, list) else data.get('markets', [])

        clean = []
        for m in markets:
            try:
                prices = m.get('outcomePrices') or []
                outcomes = m.get('outcomes') or ['YES', 'NO']
                price_map = {}
                for i, outcome in enumerate(outcomes):
                    if i < len(prices):
                        try:
                            price_map[str(outcome).upper()] = float(prices[i])
                        except:
                            pass

                yes_price = price_map.get('YES', 0.5)
                yes_pct = round(yes_price * 100, 1)

                clean.append({
                    'question': m.get('question', ''),
                    'conditionId': m.get('conditionId', ''),
                    'slug': m.get('slug', ''),
                    'volume': float(m.get('volume') or 0),
                    'yes_pct': yes_pct,
                    'endDate': str(m.get('endDate', ''))[:10],
                    'resolved': m.get('resolved', False),
                    'closed': m.get('closed', False),
                    'outcomes': outcomes,
                    'price_map': price_map,
                    'raw': m,
                })
            except Exception as e:
                continue

        print(f'Fetched {len(clean)} valid markets')
# Debug: show top 10 prices to verify accuracy
for m in clean[:10]:
    print(f'  PRICE CHECK: "{m["question"][:50]}" → YES={m["yes_pct"]}%')
return clean
    except Exception as e:
        print(f'ERROR fetching markets: {e}')
        return []

# ── Fetch RSS news ─────────────────────────────────────────────────────────
def get_recent_news():
    headlines = []
    seen_titles = set()
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:6]:
                title = entry.get('title', '').strip()
                if not title or title in seen_titles:
                    continue
                seen_titles.add(title)
                headlines.append({
                    'source': source,
                    'title': title,
                    'summary': entry.get('summary', '')[:300],
                    'link': entry.get('link', ''),
                })
        except Exception as e:
            print(f'RSS error {source}: {e}')
    print(f'Fetched {len(headlines)} news items from {len(RSS_FEEDS)} sources')
    return headlines

# ── Pre-filter: match news to markets ─────────────────────────────────────
def match_news_to_markets(news, markets):
    clusters = []
    for market in markets:
        if market['volume'] < 5000:
            continue
        q_words = set(re.findall(r'\b\w{4,}\b', market['question'].lower()))
        matched_news = []
        for n in news:
            n_words = set(re.findall(r'\b\w{4,}\b', (n['title'] + ' ' + n['summary']).lower()))
            overlap = q_words & n_words
            if len(overlap) >= 2:
                matched_news.append({
                    'source': n['source'],
                    'title': n['title'],
                    'overlap_score': len(overlap),
                })
        if matched_news:
            matched_news.sort(key=lambda x: -x['overlap_score'])
            clusters.append({
                'market': market,
                'news': matched_news[:3],
            })

    clusters.sort(key=lambda x: -len(x['news']))
    print(f'Matched {len(clusters)} market-news clusters')
    return clusters[:20]

# ── Metaculus ──────────────────────────────────────────────────────────────
def get_metaculus_context():
    try:
        r = session.get(
            'https://www.metaculus.com/api2/questions/',
            params={'status': 'open', 'order_by': '-activity', 'limit': 20},
            headers={'Accept': 'application/json'},
            timeout=10
        )
        data = r.json()
        questions = data.get('results', [])
        lines = ['=== METACULUS BASE RATES ===']
        for q in questions[:10]:
            cp = q.get('community_prediction', {})
            pred = cp.get('full', {}).get('q2') if cp else None
            if pred:
                lines.append(f'- {q.get("title", "")}: {round(pred*100)}%')
        return '\n'.join(lines)
    except Exception as e:
        print(f'Metaculus error: {e}')
        return ''

# ── GDELT ──────────────────────────────────────────────────────────────────
def get_gdelt_context():
    try:
        r = session.get(
            'https://api.gdeltproject.org/api/v2/doc/doc',
            params={
                'query': 'Iran OR Hormuz OR ceasefire OR sanctions OR AI',
                'mode': 'artlist',
                'maxrecords': 10,
                'format': 'json',
                'timespan': '6h'
            },
            timeout=15
        )
        data = r.json()
        articles = data.get('articles', [])
        if not articles:
            return ''
        lines = ['=== GDELT GLOBAL EVENTS (6h) ===']
        for a in articles[:8]:
            lines.append(f'- [{a.get("sourcecountry", "")}] {a.get("title", "")}')
        return '\n'.join(lines)
    except Exception as e:
        print(f'GDELT error: {e}')
        return ''

# ── EIA ────────────────────────────────────────────────────────────────────
def get_eia_context():
    if not EIA_KEY:
        return ''
    try:
        r = session.get(
            'https://api.eia.gov/v2/natural-gas/stor/wkly/data/',
            params={
                'api_key': EIA_KEY,
                'frequency': 'weekly',
                'data[0]': 'value',
                'sort[0][column]': 'period',
                'sort[0][direction]': 'desc',
                'length': 4
            },
            timeout=10
        )
        data = r.json()
        rows = data.get('response', {}).get('data', [])
        if not rows:
            return ''
        lines = ['=== EIA NATURAL GAS STORAGE ===']
        for row in rows[:3]:
            lines.append(f'- {row.get("period")}: {row.get("value")} Bcf')
        return '\n'.join(lines)
    except Exception as e:
        print(f'EIA error: {e}')
        return ''

# ── SIS signals ────────────────────────────────────────────────────────────
def get_sis_signals():
    if not SIS_SUPABASE_URL or not SIS_SUPABASE_KEY:
        return ''
    try:
        sis_sb = create_client(SIS_SUPABASE_URL, SIS_SUPABASE_KEY)
        signals = sis_sb.table('signals').select('*').eq('signal_strength', 'HIGH').order('created_at', desc=True).limit(5).execute()
        if not signals.data:
            return ''
        lines = ['=== SIS HIGH SIGNALS ===']
        for s in signals.data:
            lines.append(f'- [{s.get("theme")}] {s.get("headline")} | {s.get("second_order", "")}')
        return '\n'.join(lines)
    except Exception as e:
        print(f'SIS error: {e}')
        return ''

# ── Knowledge base ─────────────────────────────────────────────────────────
def get_knowledge_context():
    try:
        knowledge = sb.table('pit_knowledge').select('*').order('created_at', desc=True).limit(5).execute()
        lessons = sb.table('pit_lessons').select('*').order('created_at', desc=True).limit(10).execute()
        lines = []
        if knowledge.data:
            lines.append('=== KNOWLEDGE BASE ===')
            for k in knowledge.data:
                lines.append(f'[{k["category"].upper()}] {k["title"]}: {k["content"][:200]}')
        if lessons.data:
            lines.append('=== LESSONS LEARNED ===')
            for l in lessons.data:
                tag = f'[{l["market_type"].upper()}] ' if l.get('market_type') else ''
                lines.append(f'{tag}{l["lesson"]}')
        if lines:
            lines.append('=== END KNOWLEDGE ===')
        return '\n'.join(lines)
    except Exception as e:
        print(f'Knowledge error: {e}')
        return ''

# ── Deduplication ──────────────────────────────────────────────────────────
def signal_already_exists(condition_id, direction):
    try:
        today = now_utc().strftime('%Y-%m-%d')
        existing = sb.table('pit_signals').select('id').eq('condition_id', condition_id).eq('direction', direction).gte('created_at', today).execute()
        return len(existing.data) > 0
    except:
        return False

def paper_trade_already_exists(condition_id, direction):
    try:
        existing = sb.table('pit_paper_trades').select('id').eq('condition_id', condition_id).eq('direction', direction).eq('status', 'OPEN').execute()
        return len(existing.data) > 0
    except:
        return False

# ── Validate signal against real market data ───────────────────────────────
def validate_signal(signal, markets_by_id):
    condition_id = signal.get('conditionId', '')
    direction = signal.get('direction', '').upper()
    true_p = signal.get('trueP', 0)
    market_odds = signal.get('marketOdds', 0)
    edge_pp = signal.get('edgePp', 0)

    market = markets_by_id.get(condition_id)
    if not market:
        return False, f'conditionId {condition_id} not found in active markets', signal

    if direction not in ['YES', 'NO']:
        return False, f'Invalid direction: {direction}', signal

    real_yes_pct = market['yes_pct']
    if direction == 'YES':
        real_market_odds = real_yes_pct
    else:
        real_market_odds = round(100 - real_yes_pct, 1)

    odds_diff = abs(real_market_odds - market_odds)
    if odds_diff > 15:
        print(f'WARNING: Claude said {market_odds}% but real odds are {real_market_odds}% — correcting')
        signal['marketOdds'] = real_market_odds

    expected_edge = abs(true_p - real_market_odds)
    if abs(expected_edge - edge_pp) > 10:
        signal['edgePp'] = round(expected_edge, 1)

    conviction = signal.get('conviction', 0)
    rr = signal.get('resolutionRisk', 5)
    actual_edge = signal['edgePp']

    if signal.get('betType') == 'real':
        if conviction < 8 or rr > 2 or actual_edge < 7:
            signal['betType'] = 'paper'
            print(f'Downgraded to paper: conviction={conviction}, rr={rr}, edge={actual_edge}')

    signal['question'] = market['question']
    signal['resolutionDate'] = market['endDate']
    signal['realYesPct'] = real_yes_pct

    return True, 'valid', signal

# ── Send Telegram ──────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        return None
    try:
        r = session.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=10
        )
        if r.ok:
            return r.json().get('result', {}).get('message_id')
        else:
            r2 = session.post(
                f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg.replace('*', '').replace('_', '')},
                timeout=10
            )
            if r2.ok:
                return r2.json().get('result', {}).get('message_id')
    except Exception as e:
        print(f'Telegram error: {e}')
    return None

# ── Build rich alert ───────────────────────────────────────────────────────
def build_alert(signal, news_items=None, source='A'):
    triggering_news = signal.get('newsSource', '')
    if not triggering_news and news_items:
        for n in news_items:
            q_words = set(re.findall(r'\b\w{4,}\b', signal.get('question', '').lower()))
            n_words = set(re.findall(r'\b\w{4,}\b', n['title'].lower()))
            if len(q_words & n_words) >= 2:
                triggering_news = f'[{n["source"]}] {n["title"]}'
                break

    market_url = f'https://polymarket.com/search?q={requests.utils.quote(signal.get("question", "")[:60])}'
    scanner_tag = '📡 Scanner B — Market Mispricing' if source == 'B' else '📰 Scanner A — News Signal'

    return (
        f'⚡ *PIT ALERT*\n'
        f'_{scanner_tag}_\n\n'
        f'*{signal["question"][:100]}*\n\n'
        f'*Direction:* {signal["direction"]} @ {signal["marketOdds"]}%\n'
        f'*True P:* {signal["trueP"]}% | *Edge:* +{signal["edgePp"]}pp\n'
        f'*Conviction:* {signal["conviction"]}/10 | *RR:* {signal["resolutionRisk"]}/5\n'
        f'*Resolves:* {signal.get("resolutionDate", "?")}\n\n'
        f'🎯 *Edge:* _{signal.get("thesis", "")}_\n\n'
        f'⚠️ *Bear case:* _{signal.get("bearCase", "Not specified")}_\n\n'
        f'📋 *Resolution:* _{signal.get("resolutionCriteria", "Check Polymarket")}_\n\n'
        + (f'📰 *Signal:* _{triggering_news}_\n\n' if triggering_news else '')
        + f'🔗 {market_url}\n\n'
        f'Reply *Y* to place $25 | *N* to skip\n'
        f'⏳ 5 minute window'
    )

# ── Save signal ────────────────────────────────────────────────────────────
def save_signal(signal):
    try:
        sb.table('pit_signals').insert({
            'market_question': signal.get('question'),
            'condition_id': signal.get('conditionId'),
            'token_id': signal.get('tokenId', ''),
            'direction': signal.get('direction'),
            'market_odds': signal.get('marketOdds'),
            'true_p': signal.get('trueP'),
            'edge_pp': signal.get('edgePp'),
            'conviction': signal.get('conviction'),
            'resolution_risk': signal.get('resolutionRisk'),
            'edge_type': signal.get('edgeType'),
            'resolution_date': signal.get('resolutionDate') or None,
            'thesis': signal.get('thesis'),
        }).execute()
    except Exception as e:
        print(f'Signal save error: {e}')

# ── Save paper trade ───────────────────────────────────────────────────────
def save_paper_trade(signal):
    try:
        sb.table('pit_paper_trades').insert({
            'market_question': signal.get('question'),
            'condition_id': signal.get('conditionId'),
            'token_id': signal.get('tokenId', ''),
            'direction': signal.get('direction'),
            'market_odds': signal.get('marketOdds'),
            'true_p': signal.get('trueP'),
            'edge_pp': signal.get('edgePp'),
            'stake': 25,
            'resolution_date': signal.get('resolutionDate'),
            'thesis': signal.get('thesis'),
            'status': 'OPEN'
        }).execute()
        print(f'Paper trade saved: {signal.get("question", "")[:60]}')
    except Exception as e:
        print(f'Paper trade save error: {e}')

# ── Process signals (shared by Scanner A and B) ───────────────────────────
def process_signals(raw_signals, markets_by_id, news_items=None, source='A'):
    real_count = 0
    paper_count = 0

    for raw in raw_signals:
        valid, reason, signal = validate_signal(raw, markets_by_id)
        if not valid:
            print(f'INVALID signal: {reason}')
            continue

        condition_id = signal.get('conditionId')
        direction = signal.get('direction')

        if signal_already_exists(condition_id, direction):
            print(f'DUPLICATE skipped: {signal["question"][:50]}')
            continue

        save_signal(signal)

        if signal.get('betType') == 'real':
            real_count += 1
            alert = build_alert(signal, news_items, source)
            msg_id = send_telegram(alert)
            print(f'REAL ALERT sent [{source}]: {signal["question"][:60]}')
            # Store message_id so reply-to works
            if msg_id:
                try:
                    sb.table('pit_signals').update({
                        'telegram_message_id': msg_id
                    }).eq('condition_id', signal.get('conditionId')).eq('direction', signal.get('direction')).execute()
                except Exception as e:
                    print(f'Message ID save error: {e}')

        elif signal.get('betType') == 'paper':
            if not paper_trade_already_exists(condition_id, direction):
                paper_count += 1
                save_paper_trade(signal)
                print(f'Paper trade [{source}]: {signal["question"][:60]}')
            else:
                print(f'Paper trade already open: {signal["question"][:50]}')

    return real_count, paper_count

# ── Resolve paper trades ───────────────────────────────────────────────────
def generate_resolution_questions(trade, outcome, won):
    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=400,
            messages=[{
                'role': 'user',
                'content': (
                    f'A prediction market paper trade just resolved. Generate 3 SPECIFIC questions '
                    f'to ask the trader to extract maximum learning value. Be specific to THIS trade.\n\n'
                    f'Market: "{trade["market_question"]}"\n'
                    f'Direction: {trade["direction"]} @ {trade["market_odds"]}%\n'
                    f'True P estimate: {trade["true_p"]}%\n'
                    f'Edge claimed: {trade["edge_pp"]}pp\n'
                    f'Thesis: {trade.get("thesis", "")}\n'
                    f'Outcome: {outcome} | {"WON" if won else "LOST"}\n\n'
                    f'Questions should cover:\n'
                    f'- What specifically triggered the resolution\n'
                    f'- Whether the thesis reasoning held or broke\n'
                    f'- Any resolution criteria technicality\n\n'
                    f'Return ONLY 3 numbered questions, one per line.'
                )
            }]
        )
        return msg.content[0].text.strip()
    except:
        return '1. What actually happened that triggered resolution?\n2. Did your thesis reasoning hold or break?\n3. Was there any resolution criteria technicality?'


def resolve_paper_trades():
    try:
        open_trades = sb.table('pit_paper_trades').select('*').eq('status', 'OPEN').eq('notified', False).execute()
        if not open_trades.data:
            return
        print(f'Checking {len(open_trades.data)} open paper trades...')
        today = now_utc().strftime('%Y-%m-%d')
        for trade in open_trades.data:
            if not trade.get('condition_id'):
                continue
            res_date = trade.get('resolution_date', '')
            if not res_date or res_date > today:
                continue
            try:
                r = session.get(
                    f'{GAMMA_API}/markets',
                    params={'conditionIds': trade['condition_id']},
                    timeout=10
                )
                data = r.json()
                markets = data if isinstance(data, list) else data.get('markets', [])
                if not markets:
                    continue
                market = markets[0]

                outcomes = market.get('outcomes') or ['YES', 'NO']
                prices = market.get('outcomePrices') or []
                price_map = {}
                for i, outcome in enumerate(outcomes):
                    if i < len(prices):
                        try:
                            price_map[str(outcome).upper()] = float(prices[i])
                        except:
                            pass

                outcome = None
                for o, p in price_map.items():
                    if p > 0.95:
                        outcome = o
                        break

                if not outcome:
                    continue

                won = outcome == trade['direction']
                pnl = round(trade['stake'] * (100 / trade['market_odds'] - 1), 2) if won else -float(trade['stake'])
                result = 'WON ✅' if won else 'LOST ❌'

                questions = generate_resolution_questions(trade, outcome, won)

                sb.table('pit_paper_trades').update({
                    'outcome': outcome,
                    'pnl': pnl,
                    'notified': True,
                    'status': 'PENDING_RESOLVE'
                }).eq('id', trade['id']).execute()

                market_url = f'https://polymarket.com/search?q={requests.utils.quote(trade["market_question"][:60])}'
                send_telegram(
                    f'📊 *PAPER TRADE RESOLVED*\n\n'
                    f'*{trade["market_question"][:80]}*\n\n'
                    f'Your call: *{trade["direction"]}* @ {trade["market_odds"]}%\n'
                    f'Resolved: *{outcome}* | {result}\n'
                    f'Paper P&L: ${pnl:+.2f}\n\n'
                    f'Your thesis was: _{trade.get("thesis", "")[:150]}_\n\n'
                    f'🔗 {market_url}\n\n'
                    f'━━━━━━━━━━━━━━━━━━\n'
                    f'*Reply with answers to:*\n\n'
                    f'{questions}\n\n'
                    f'More detail = better lesson saved to KB.\n'
                    f'Reply *Y* for basic lesson | *N* to skip'
                )
                print(f'Notified: {trade["market_question"][:50]} → {outcome} | {result}')

            except Exception as e:
                print(f'Resolution error trade {trade.get("id")}: {e}')

    except Exception as e:
        print(f'Resolve paper trades error: {e}')

def extract_lesson(trade, outcome, won):
    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=200,
            messages=[{
                'role': 'user',
                'content': (
                    f'Generate one specific actionable lesson (1-2 sentences) from this resolved paper trade.\n\n'
                    f'Market: "{trade["market_question"]}"\n'
                    f'Direction: {trade["direction"]}\n'
                    f'My P: {trade["true_p"]}% | Market odds: {trade["market_odds"]}%\n'
                    f'Edge claimed: {trade["edge_pp"]}pp\n'
                    f'Outcome: {outcome} | Won: {won}\n'
                    f'Thesis: {trade.get("thesis", "")}\n\n'
                    f'Return ONLY the lesson text.'
                )
            }]
        )
        return msg.content[0].text.strip()
    except:
        return ''

# ── Stage 1: shortlist candidates (Scanner A) ──────────────────────────────
def stage1_shortlist(clusters, knowledge, metaculus, gdelt, eia, sis):
    if not clusters:
        return []

    cluster_text = ''
    for i, c in enumerate(clusters[:15]):
        m = c['market']
        news_titles = ' | '.join([n['title'] for n in c['news'][:2]])
        cluster_text += (
            f'{i+1}. Market: "{m["question"]}"\n'
            f'   conditionId: {m["conditionId"]}\n'
            f'   YES odds: {m["yes_pct"]}% | Volume: ${int(m["volume"]):,} | Ends: {m["endDate"]}\n'
            f'   Matched news: {news_titles}\n\n'
        )

    prompt = (
        f'{knowledge}\n\n'
        f'{metaculus}\n\n'
        f'{gdelt}\n\n'
        f'{eia}\n\n'
        f'{sis}\n\n'
        f'You are a Polymarket signal detector.\n\n'
        f'Below are market-news clusters where breaking news may not yet be priced in.\n\n'
        f'MARKET-NEWS CLUSTERS:\n{cluster_text}\n\n'
        f'Select up to 5 clusters where breaking news has NOT yet been priced into the market odds.\n'
        f'Only include if you can identify a specific reason the odds are stale or wrong.\n'
        f'Apply all knowledge base lessons before selecting.\n'
        f'Exclude only elections and Fed interest rate decisions. Include geopolitical, tech, corporate, and energy markets.\n\n'
        f'Return ONLY a JSON array of conditionIds inside <shortlist> tags:\n'
        f'<shortlist>["id1", "id2", "id3"]</shortlist>'
    )

    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=500,
            messages=[{'role': 'user', 'content': prompt}]
        )
        response = msg.content[0].text
        match = re.search(r'<shortlist>(.*?)</shortlist>', response, re.DOTALL)
        if not match:
            return []
        ids = json.loads(match.group(1).strip())
        print(f'Stage 1 shortlisted: {ids}')
        return ids
    except Exception as e:
        print(f'Stage 1 error: {e}')
        return []

# ── Stage 2: score shortlisted candidates (Scanner A) ─────────────────────
def stage2_score(shortlisted_ids, clusters, knowledge, news):
    shortlisted = [c for c in clusters if c['market']['conditionId'] in shortlisted_ids]
    if not shortlisted:
        return []

    market_details = ''
    for c in shortlisted:
        m = c['market']
        news_items = '\n'.join([f'   - [{n["source"]}] {n["title"]}' for n in c['news']])
        market_details += (
            f'Market: "{m["question"]}"\n'
            f'conditionId: {m["conditionId"]}\n'
            f'YES odds: {m["yes_pct"]}% | Volume: ${int(m["volume"]):,} | Ends: {m["endDate"]}\n'
            f'Related news:\n{news_items}\n\n'
        )

    prompt = (
        f'{knowledge}\n\n'
        f'You are a Polymarket edge analyst. Score these shortlisted markets.\n\n'
        f'{market_details}\n\n'
        f'For each market, provide a full analysis and betting decision.\n'
        f'Apply ALL knowledge base lessons before deciding.\n\n'
        f'Return JSON inside <signals> tags:\n'
        f'<signals>\n'
        f'[\n'
        f'  {{\n'
        f'    "question": "exact question",\n'
        f'    "conditionId": "exact conditionId from above",\n'
        f'    "tokenId": "",\n'
        f'    "direction": "YES or NO",\n'
        f'    "marketOdds": 45,\n'
        f'    "trueP": 62,\n'
        f'    "edgePp": 17,\n'
        f'    "conviction": 8,\n'
        f'    "resolutionRisk": 2,\n'
        f'    "edgeType": "slow_update",\n'
        f'    "resolutionDate": "YYYY-MM-DD",\n'
        f'    "thesis": "one sentence: what the market is getting wrong",\n'
        f'    "bearCase": "one sentence: what proves this wrong",\n'
        f'    "resolutionCriteria": "exact resolution criteria",\n'
        f'    "newsSource": "which source triggered this",\n'
        f'    "betType": "real or paper"\n'
        f'  }}\n'
        f']\n'
        f'</signals>\n\n'
        f'Rules:\n'
        f'- conviction 1-10, only include >= 6\n'
        f'- resolutionRisk 1-5, exclude >= 4\n'
        f'- betType real ONLY if conviction >= 8 AND resolutionRisk <= 2 AND edgePp >= 7\n'
        f'- betType paper if conviction 6-7 OR resolutionRisk = 3\n'
        f'- If no genuine edge: return <signals>[]</signals>'
    )

    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=3000,
            tools=[{'type': 'web_search_20250305', 'name': 'web_search'}],
            messages=[{'role': 'user', 'content': prompt}]
        )
        response = ''
        for block in msg.content:
            if hasattr(block, 'type') and block.type == 'text':
                response += block.text
        match = re.search(r'<signals>(.*?)</signals>', response, re.DOTALL)
        if not match:
            print('No signals in stage 2')
            return []
        signals = json.loads(match.group(1).strip())
        print(f'Stage 2 scored {len(signals)} signals')
        return signals
    except json.JSONDecodeError as e:
        print(f'Stage 2 JSON error: {e}')
        return []
    except Exception as e:
        print(f'Stage 2 error: {e}')
        return []

# ══════════════════════════════════════════════════════════════════════════
# SCANNER B — Market-Driven Mispricing Scanner
# No news dependency. Scans all markets and asks: is this price wrong?
# ══════════════════════════════════════════════════════════════════════════

def get_scanner_b_markets(all_markets):
    """
    Filter markets for Scanner B:
    - Exclude sports, esports, elections, Fed decisions
    - Minimum volume $10k (liquid enough to bet)
    - Resolving within 90 days (not too far out)
    - Not already in an open paper trade
    """
    today = now_utc().strftime('%Y-%m-%d')
    cutoff_90d = now_utc().replace(month=now_utc().month).strftime('%Y-%m-%d')

    from datetime import timedelta
    cutoff = (now_utc() + timedelta(days=90)).strftime('%Y-%m-%d')

    filtered = []
    for m in all_markets:
        if is_excluded_market(m['question']):
            continue
        if m['volume'] < 10000:
            continue
        if not m['endDate'] or m['endDate'] < today:
            continue
        if m['endDate'] > cutoff:
            continue
        if m.get('resolved') or m.get('closed'):
            continue
        # Skip if already have open paper trade
        if paper_trade_already_exists(m['conditionId'], 'YES') or paper_trade_already_exists(m['conditionId'], 'NO'):
            continue
        filtered.append(m)

    # Sort by volume descending, take top 150
    filtered.sort(key=lambda x: -x['volume'])
    print(f'Scanner B: {len(filtered)} eligible markets after filtering')
    return filtered[:150]


def scanner_b_score_batch(markets_batch, knowledge):
    """
    Ask Claude to identify mispricings in a batch of markets.
    No news context — uses Claude's world knowledge only.
    """
    market_text = ''
    for m in markets_batch:
        market_text += (
            f'Market: "{m["question"]}"\n'
            f'conditionId: {m["conditionId"]}\n'
            f'YES odds: {m["yes_pct"]}% | Volume: ${int(m["volume"]):,} | Resolves: {m["endDate"]}\n\n'
        )

    MASTER_PROMPT = """You are PIT Scanner — a prediction market analyst. Your only job is to find markets where the crowd price is provably wrong. You do this by running a rigorous mathematical framework, not by intuition.

Your default is PASS. You output a signal only when the math — built entirely on verified, web-searched facts — shows a genuine edge.

═══ ABSOLUTE RULE ═══

Every single number you use in any calculation must be sourced from a web search you run during this session. Training knowledge is a prior only — it tells you where to look, not what to use. If you cannot find a verified source for a number, you cannot use it. If you cannot source enough numbers to run the math, output PASS.

═══ THREE GATES ═══

All three must pass before any analysis:

G1 — PERMITTED TYPE?
Permitted: geopolitical events, military actions, diplomatic outcomes, ceasefire/sanctions/strait markets, tech/AI corporate milestones, FDA approvals, merger completions, CEO tenure, energy/commodity thresholds, scientific regulatory decisions.
Auto-PASS (stop immediately, no analysis): elections, Fed rate decisions, sports of any kind, esports, entertainment awards, earnings surprise markets, any Powell/Fed Chair market.

G2 — CAN YOU STATE THE EDGE IN ONE FALSIFIABLE SENTENCE?
Not "this seems underpriced." One sentence: what specific fact does the market not yet reflect, and where did you find it?

G3 — IS RESOLUTION RISK ≤3?
Read the actual resolution rules before assigning RR. If you haven't quoted the exact trigger language, you haven't done this step.

═══ STEP 1 — MANDATORY RESEARCH ═══

Before touching probability, run web searches to establish ground truth on this specific market.

Search for:
1. The most recent factual development directly relevant to this market (last 48-72 hours)
2. The exact resolution criteria — what event, which named source, what exact wording triggers YES
3. Historical base rate for this type of event — find an actual data source, not a guess
4. Any other forecasting source covering this question (Metaculus, Kalshi, expert consensus)
5. Anything that would definitively close the trade (kill condition already happened?)

After each search, record:
- What you found
- The source and approximate date
- Whether it moves the probability toward YES or NO, and by how much
- Whether it changes the resolution risk assessment

If searches return nothing relevant to the market: price is probably efficient. PASS.

═══ STEP 2 — METHOD SELECTION ═══

Now that you have the research, decide which analytical method(s) will most accurately estimate the true probability for THIS specific market.

You must explicitly reason: "For this market, Method X gives the most signal because [specific reason]. Method Y is less useful here because [specific reason]."

Do not follow a category lookup table. Think about what this particular market is testing.

THE FOUR METHODS:

METHOD 1 — BASE RATE + BAYESIAN UPDATE
Best when: there is a well-defined reference class with documented historical frequency (ceasefires in active conflicts, FDA approval rates by drug class, OPEC compliance rates, CEO tenure patterns).
How to run:
  P(YES)_true = P_base × likelihood_ratio
  P_base = the historically verified frequency of this event type resolving YES [WEB SEARCH REQUIRED — cite source]
  likelihood_ratio = P(current specific evidence | YES) ÷ P(current specific evidence | NO)
  Show the ratio calculation explicitly. Name every piece of evidence used.
Do NOT use this method if: you cannot find a defensible reference class with a real source, or the situation is too unique for base rates to apply.

METHOD 2 — DECOMPOSITION
Best when: the market resolves YES only if MULTIPLE independent conditions are all simultaneously true (political will + legal mechanism + timing + verification), OR when any single condition collapsing kills the bet.
How to run:
  List every necessary condition for YES resolution
  For each condition: P(condition_n) [WEB SEARCH REQUIRED — what evidence supports this?]
  P(YES) = P(C1) × P(C2|C1) × P(C3|C1∧C2) × ...
  If any condition has P < 10% — the product collapses. Likely PASS.
Do NOT use this method if: there is really only one condition that matters (use Method 1 or 4 instead).

METHOD 3 — WISDOM OF CROWDS CROSS-CHECK
Best when: multiple independent forecasting platforms exist for this question, and you can compare their estimates to Polymarket's price.
How to run:
  Search for Metaculus forecast on this topic [WEB SEARCH]
  Search for Kalshi or Manifold equivalent [WEB SEARCH]
  If Polymarket diverges from all other sources by >10pp: structural mispricing candidate. Investigate why.
  If all sources agree within 5pp: no edge. PASS.
Do NOT use this method as your primary: it tells you a mispricing exists, not why. Always combine with another method to explain the gap.

METHOD 4 — RESOLUTION TECHNICALITY
Best when: the resolution criteria uses specific language that the average bettor is likely misreading — they're pricing what they think will happen, not what the rules actually require.
How to run:
  Quote the exact resolution trigger language [WEB SEARCH — find the actual Polymarket rules]
  P(YES)_true = P(event happens) × P(resolves YES | event happens)
              + P(event doesn't happen) × P(resolves YES | event doesn't happen)
  The edge here is the gap between P(event happens) and P(resolves YES). Name it precisely.
Do NOT use this method if: the resolution language is straightforward and unambiguous (RR would be 1-2 anyway).

═══ STEP 3 — RUN THE METHODS ═══

Run your selected primary method first, with full working shown.
Run your secondary method, with full working shown.

Every number used must have a source from Step 1 searches. If you need a number and don't have a source: do another web search now. If you still can't source it: acknowledge it as an assumption and apply a 10pp uncertainty penalty to your estimate.

═══ STEP 4 — RECONCILE ═══

Compare your primary and secondary results:
- Agree within 10pp → weighted average (60% primary, 40% secondary)
- Conflict by 10-20pp → investigate why. Run an additional search to resolve. Paper trade maximum.
- Conflict by >20pp → the inputs are too uncertain. PASS.

═══ STEP 5 — CALIBRATION ═══

Apply a mandatory 10-15pp haircut to your estimate. You are systematically overconfident. When you feel 80% sure, your calibrated estimate is 65-70%.

Additional haircuts:
- Any unsourced assumption used: −5pp per assumption
- Resolution language ambiguous (RR=3): −5pp additional
- Primary and secondary methods conflicted: −5pp additional

═══ STEP 6 — NET EDGE ═══

Geopolitics & world events (fee-free): Net edge = P(YES)_calibrated − P(YES)_market
All other categories: Net edge = P(YES)_calibrated − P(YES)_market − 0.75pp

Thresholds:
  <3pp → PASS
  3-7pp → paper trade
  7-15pp → real bet candidate
  >15pp → you missed something, recheck

═══ STEP 7 — RESOLUTION RISK ═══

Assign based on what you actually found in Step 1, not a guess:
RR1: Named source, objective trigger, zero interpretation needed
RR2: Named source, small edge cases unlikely to matter
RR3: One identified technicality — acknowledge it, paper trade only
RR4+: PASS

═══ STEP 8 — FINAL CLASSIFICATION ═══

REAL BET: net edge ≥7pp AND RR≤2 AND conviction≥8 AND all inputs sourced
PAPER TRADE: net edge ≥3pp AND RR≤3 AND conviction 6-7
PASS: anything else — including any case where a critical input could not be sourced

═══ MACRO CONTEXT MARCH 2026 ═══

Hormuz crisis: US-Israel strikes on Iran from Feb 28. Strait effectively closed. Qatar LNG offline. Brent $70→$126. Ceasefire talks via Oman ongoing. Geopolitical markets are FEE-FREE — primary hunting ground.
Second-order plays: US LNG export markets, war risk insurance, pipeline bypass infrastructure.
Agentic AI: OpenClaw confirmed fastest-growing open source project at GTC 2026. Compute demand 1000x+. Nvidia confirmed compute platform through 2027. Tech category has fees from March 30.
Petrodollar stress: Saudi ended exclusive dollar pricing June 2024. CIPS $245T yuan 2025. Iran demanding yuan for Hormuz passage. Gold primary beneficiary.
Fee regime: From March 30 2026, ONLY geopolitics/world events are fee-free. Exploit this structural advantage.

═══ CRITICAL REMINDERS ═══

- If a number has no web search source: do not use it
- If web search finds the kill condition already happened: PASS immediately
- If the market has already repriced toward the edge: PASS
- 0 signals is the correct output if nothing clears all 8 steps
- Never claim edgePp >15
- Never assign conviction ≥8 without multiple corroborating web search findings"""

    prompt = (
        f'{MASTER_PROMPT}\n\n'
        f'{knowledge}\n\n'
        f'You have web search. USE IT NOW before scoring any market.\n\n'
        f'MARKETS TO ASSESS ({len(markets_batch)} markets):\n{market_text}\n\n'
        f'Search for current news and developments on EACH market before scoring.\n'
        f'Return JSON inside <signals> tags:\n'
        f'<signals>\n'
        f'[\n'
        f'  {{\n'
        f'    "question": "exact question",\n'
        f'    "conditionId": "exact conditionId from above",\n'
        f'    "tokenId": "",\n'
        f'    "direction": "YES or NO",\n'
        f'    "marketOdds": 45,\n'
        f'    "trueP": 62,\n'
        f'    "edgePp": 17,\n'
        f'    "conviction": 7,\n'
        f'    "resolutionRisk": 2,\n'
        f'    "edgeType": "slow_update|second_order|base_rate|resolution_technicality",\n'
        f'    "resolutionDate": "YYYY-MM-DD",\n'
        f'    "thesis": "one specific sentence: exactly what the market is getting wrong and why",\n'
        f'    "bearCase": "one sentence: what proves this wrong",\n'
        f'    "resolutionCriteria": "exact resolution criteria from the market rules",\n'
        f'    "newsSource": "what web search found that supports this edge",\n'
        f'    "betType": "real or paper"\n'
        f'  }}\n'
        f']\n'
        f'</signals>\n\n'
        f'Rules:\n'
        f'- Only include markets with a SPECIFIC mispricing reason confirmed by web search\n'
        f'- conviction 1-10, only include ≥6\n'
        f'- resolutionRisk 1-5, exclude ≥4\n'
        f'- betType real ONLY if conviction ≥8 AND resolutionRisk ≤2 AND edgePp ≥7\n'
        f'- betType paper if conviction 6-7 OR resolutionRisk = 3\n'
        f'- NEVER claim edgePp >15 — if you think it is that large, recheck\n'
        f'- NEVER assign conviction ≥8 without a specific web search finding\n'
        f'- 0 signals is correct if nothing is genuinely mispriced\n'
        f'- If no genuine mispricing: return <signals>[]</signals>'
    )
    

    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=4000,
            tools=[{'type': 'web_search_20250305', 'name': 'web_search'}],
            messages=[{'role': 'user', 'content': prompt}]
        )
        response = ''
        for block in msg.content:
            if hasattr(block, 'type') and block.type == 'text':
                response += block.text
        match = re.search(r'<signals>(.*?)</signals>', response, re.DOTALL)
        if not match:
            return []
        signals = json.loads(match.group(1).strip())
        return signals
    except json.JSONDecodeError as e:
        print(f'Scanner B JSON error: {e}')
        return []
    except Exception as e:
        print(f'Scanner B batch error: {e}')
        return []


def run_market_scan():
    """
    Scanner B — Market-driven mispricing scanner.
    Scans all eligible markets and finds mispricings using Claude's world knowledge.
    No news dependency.
    """
    global _is_running_b
    if _is_running_b:
        print('Scanner B already running — skipping')
        return
    _is_running_b = True

    try:
        time.sleep(30)  # Wait 30s after Scanner A to avoid rate limit collision
        print(f'\n=== SCANNER B {now_utc().strftime("%Y-%m-%d %H:%M")} UTC ===')

        # Fetch markets and knowledge
        all_markets = get_active_markets(limit=200)
        if not all_markets:
            print('Scanner B: No markets fetched')
            return

        knowledge = get_knowledge_context()
        markets_by_id = {m['conditionId']: m for m in all_markets}

        # Filter to eligible markets
        eligible = get_scanner_b_markets(all_markets)
        if not eligible:
            print('Scanner B: No eligible markets after filtering')
            return

        print(f'Scanner B: Scoring {len(eligible)} markets in batches of 20')

        # Process in batches of 20
        all_signals = []
        batch_size = 5
        for i in range(0, min(len(eligible), 100), batch_size):
            batch = eligible[i:i + batch_size]
            print(f'Scanner B: Batch {i//batch_size + 1} ({len(batch)} markets)...')
            signals = scanner_b_score_batch(batch, knowledge)
            if signals:
                all_signals.extend(signals)
                print(f'Scanner B: Found {len(signals)} signals in batch')
            time.sleep(60)  # Rate limiting between batches

        if not all_signals:
            print('Scanner B: No mispricings found this scan')
            return

        print(f'Scanner B: Total {len(all_signals)} signals to validate')
        real_count, paper_count = process_signals(all_signals, markets_by_id, source='B')
        print(f'=== Scanner B complete: {real_count} real alerts, {paper_count} paper trades ===')

    except Exception as e:
        print(f'SCANNER B ERROR: {e}')
        send_telegram(f'⚠️ Scanner B error: {str(e)[:100]}')
    finally:
        _is_running_b = False

# ── Scanner A — News-driven scan ──────────────────────────────────────────
def run_scan():
    global _is_running
    if _is_running:
        print('Scanner A already running — skipping')
        return
    _is_running = True

    try:
        print(f'\n=== SCANNER A {now_utc().strftime("%Y-%m-%d %H:%M")} UTC ===')

        markets = get_active_markets()
        news = get_recent_news()
        knowledge = get_knowledge_context()
        metaculus = get_metaculus_context()
        gdelt = get_gdelt_context()
        eia = get_eia_context()
        sis = get_sis_signals()

        if not markets:
            print('No markets — aborting scan')
            return

        markets_by_id = {m['conditionId']: m for m in markets}

        clusters = match_news_to_markets(news, markets)
        if not clusters:
            print('No market-news clusters found')
            return

        # Stage 1: shortlist
        shortlisted_ids = stage1_shortlist(clusters, knowledge, metaculus, gdelt, eia, sis)
        if not shortlisted_ids:
            print('Stage 1 returned nothing — falling back to top 5 clusters')
            shortlisted_ids = [c['market']['conditionId'] for c in clusters[:5]]

        # Stage 2: score
        raw_signals = stage2_score(shortlisted_ids, clusters, knowledge, news)
        if not raw_signals:
            print('No signals from stage 2')
            return

        real_count, paper_count = process_signals(raw_signals, markets_by_id, news, source='A')
        print(f'=== Scanner A complete: {real_count} real alerts, {paper_count} paper trades ===')

    except Exception as e:
        print(f'SCANNER A ERROR: {e}')
        send_telegram(f'⚠️ Scanner A error: {str(e)[:100]}')
    finally:
        _is_running = False

    # Resolve paper trades after every Scanner A run
    resolve_paper_trades()

# ── Schedule ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('PIT Scanner v3 starting...')
    send_telegram(
        '🟢 *PIT Scanner v3 started*\n\n'
        '✓ Scanner A — News-driven signals\n'
        '✓ Scanner B — Market mispricing detection\n'
        '✓ Expanded market types\n'
        '✓ KB + lessons injected into both scanners\n'
        '✓ Auto paper trade resolution alerts\n\n'
        'Scanning every 30 min 24/7'
    )
    # Run both scanners on startup
    run_scan()
    run_market_scan()

    # Scanner A every 30 min, Scanner B every 60 min (heavier)
    schedule.every(30).minutes.do(run_scan)
    schedule.every(60).minutes.do(run_market_scan)

    while True:
        schedule.run_pending()
        time.sleep(60)
