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

# ── Fetch Polymarket markets ───────────────────────────────────────────────
def get_active_markets():
    try:
        r = session.get(f'{GAMMA_API}/markets', params={
            'active': 'true',
            'closed': 'false',
            'limit': 100,
            'order': 'volume',
            'ascending': 'false'
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        markets = data if isinstance(data, list) else data.get('markets', [])

        # Build clean market objects with validated fields
        clean = []
        for m in markets:
            try:
                prices = m.get('outcomePrices') or []
                outcomes = m.get('outcomes') or ['YES', 'NO']
                # Build price map by outcome label
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
    """
    Instead of dumping everything to Claude, pre-match news headlines
    to relevant markets using keyword overlap. Returns clusters.
    """
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
        signals = sis_sb.table('sis_signals').select('*').eq('signal_strength', 'HIGH').eq('processed', False).order('created_at', desc=True).limit(5).execute()
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
        knowledge = sb.table('pit_knowledge').select('*').order('created_at', desc=True).limit(10).execute()
        lessons = sb.table('pit_lessons').select('*').order('created_at', desc=True).limit(20).execute()
        lines = []
        if knowledge.data:
            lines.append('=== KNOWLEDGE BASE ===')
            for k in knowledge.data:
                lines.append(f'[{k["category"].upper()}] {k["title"]}: {k["content"][:400]}')
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
    """Check if we already sent this signal today."""
    try:
        today = now_utc().strftime('%Y-%m-%d')
        existing = sb.table('pit_signals').select('id').eq('condition_id', condition_id).eq('direction', direction).gte('created_at', today).execute()
        return len(existing.data) > 0
    except:
        return False

def paper_trade_already_exists(condition_id, direction):
    """Check if open paper trade already exists for this market."""
    try:
        existing = sb.table('pit_paper_trades').select('id').eq('condition_id', condition_id).eq('direction', direction).eq('status', 'OPEN').execute()
        return len(existing.data) > 0
    except:
        return False

# ── Validate signal against real market data ───────────────────────────────
def validate_signal(signal, markets_by_id):
    """
    Hard validation after Claude output.
    Returns (valid, reason, enriched_signal)
    """
    condition_id = signal.get('conditionId', '')
    direction = signal.get('direction', '').upper()
    true_p = signal.get('trueP', 0)
    market_odds = signal.get('marketOdds', 0)
    edge_pp = signal.get('edgePp', 0)

    # 1. conditionId must exist in our fetched markets
    market = markets_by_id.get(condition_id)
    if not market:
        return False, f'conditionId {condition_id} not found in active markets', signal

    # 2. Direction must be valid
    if direction not in ['YES', 'NO']:
        return False, f'Invalid direction: {direction}', signal

    # 3. Verify market odds against real data (allow 15pp tolerance)
    real_yes_pct = market['yes_pct']
    if direction == 'YES':
        real_market_odds = real_yes_pct
    else:
        real_market_odds = round(100 - real_yes_pct, 1)

    odds_diff = abs(real_market_odds - market_odds)
    if odds_diff > 15:
        print(f'WARNING: Claude said {market_odds}% but real odds are {real_market_odds}% — correcting')
        signal['marketOdds'] = real_market_odds

    # 4. Verify edge is mathematically consistent
    expected_edge = abs(true_p - real_market_odds)
    if abs(expected_edge - edge_pp) > 10:
        signal['edgePp'] = round(expected_edge, 1)

    # 5. Verify betType follows our rules
    conviction = signal.get('conviction', 0)
    rr = signal.get('resolutionRisk', 5)
    actual_edge = signal['edgePp']

    if signal.get('betType') == 'real':
        if conviction < 8 or rr > 2 or actual_edge < 7:
            signal['betType'] = 'paper'
            print(f'Downgraded to paper: conviction={conviction}, rr={rr}, edge={actual_edge}')

    # 6. Enrich with real market data
    signal['question'] = market['question']
    signal['resolutionDate'] = market['endDate']
    signal['realYesPct'] = real_yes_pct

    return True, 'valid', signal

# ── Send Telegram ──────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    # Sanitize markdown
    try:
        r = session.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=10
        )
        if not r.ok:
            # Try without markdown if formatting fails
            session.post(
                f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT, 'text': msg.replace('*', '').replace('_', '')},
                timeout=10
            )
    except Exception as e:
        print(f'Telegram error: {e}')

# ── Build rich alert ───────────────────────────────────────────────────────
def build_alert(signal, news_items):
    triggering_news = signal.get('newsSource', '')
    if not triggering_news:
        for n in news_items:
            q_words = set(re.findall(r'\b\w{4,}\b', signal.get('question', '').lower()))
            n_words = set(re.findall(r'\b\w{4,}\b', n['title'].lower()))
            if len(q_words & n_words) >= 2:
                triggering_news = f'[{n["source"]}] {n["title"]}'
                break

    market_url = f'https://polymarket.com/event/{signal.get("conditionId", "")}'

    return (
        f'⚡ *PIT AUTO-BET PENDING*\n\n'
        f'*{signal["question"][:100]}*\n\n'
        f'*Direction:* {signal["direction"]} @ {signal["marketOdds"]}%\n'
        f'*True P:* {signal["trueP"]}% | *Edge:* +{signal["edgePp"]}pp\n'
        f'*Conviction:* {signal["conviction"]}/10 | *RR:* {signal["resolutionRisk"]}/5\n'
        f'*Resolves:* {signal.get("resolutionDate", "?")}\n\n'
        f'📰 *Why NOW:*\n_{triggering_news or "Multiple signals converging"}_\n\n'
        f'🎯 *Edge:* _{signal.get("thesis", "")}_\n\n'
        f'⚠️ *Bear case:* _{signal.get("bearCase", "Not specified")}_\n\n'
        f'📋 *Resolution:* _{signal.get("resolutionCriteria", "Check Polymarket")}_\n\n'
        f'🔗 {market_url}\n\n'
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
            'resolution_date': signal.get('resolutionDate'),
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

# ── Resolve paper trades ───────────────────────────────────────────────────
def resolve_paper_trades():
    try:
        open_trades = sb.table('pit_paper_trades').select('*').eq('status', 'OPEN').execute()
        if not open_trades.data:
            return
        print(f'Checking {len(open_trades.data)} open paper trades...')
        for trade in open_trades.data:
            if not trade.get('condition_id'):
                continue
            try:
                # Use slug-based endpoint which is more reliable
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

                if not (market.get('closed') or market.get('resolved')):
                    continue

                # Get outcome from resolved market
                outcomes = market.get('outcomes') or ['YES', 'NO']
                prices = market.get('outcomePrices') or []
                price_map = {}
                for i, outcome in enumerate(outcomes):
                    if i < len(prices):
                        try:
                            price_map[str(outcome).upper()] = float(prices[i])
                        except:
                            pass

                # Winning outcome has price close to 1.0
                outcome = None
                for o, p in price_map.items():
                    if p > 0.95:
                        outcome = o
                        break

                if not outcome:
                    print(f'Could not determine outcome for {trade["market_question"][:50]}')
                    continue

                won = outcome == trade['direction']
                pnl = round(trade['stake'] * (100 / trade['market_odds'] - 1), 2) if won else -float(trade['stake'])
                lesson = extract_lesson(trade, outcome, won)

                sb.table('pit_paper_trades').update({
                    'status': 'CLOSED',
                    'outcome': outcome,
                    'pnl': pnl,
                    'lesson': lesson,
                    'resolved_at': now_utc_iso()
                }).eq('id', trade['id']).execute()

                if lesson:
                    sb.table('pit_lessons').insert({
                        'lesson': lesson,
                        'lesson_type': 'auto',
                        'source': f'Paper trade: {trade["market_question"][:50]}'
                    }).execute()

                result = 'WON ✅' if won else 'LOST ❌'
                send_telegram(
                    f'📊 *PAPER TRADE RESOLVED*\n\n'
                    f'*{trade["market_question"][:80]}*\n'
                    f'{trade["direction"]} → Resolved {outcome} | {result}\n'
                    f'P&L: ${pnl:+.2f}\n\n'
                    f'💡 *Lesson:*\n_{lesson[:200] if lesson else "None"}_'
                )
                print(f'Resolved: {trade["market_question"][:50]} → {outcome} | {result}')

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

# ── Stage 1: shortlist candidates ─────────────────────────────────────────
def stage1_shortlist(clusters, knowledge, metaculus, gdelt, eia, sis):
    """Ask Claude to shortlist the most promising market-news pairs."""
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
        f'Select the TOP 5 clusters where there is a genuine slow-update or second-order edge.\n'
        f'Apply all knowledge base lessons before selecting.\n'
        f'Exclude elections, Fed decisions, slow political markets.\n\n'
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

# ── Stage 2: score shortlisted candidates ─────────────────────────────────
def stage2_score(shortlisted_ids, clusters, knowledge, news):
    """Deep score only the shortlisted markets."""
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
            max_tokens=2000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        response = msg.content[0].text
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

# ── Main scan ──────────────────────────────────────────────────────────────
def run_scan():
    global _is_running
    if _is_running:
        print('Scan already running — skipping')
        return
    _is_running = True

    try:
        print(f'\n=== PIT SCAN v2 {now_utc().strftime("%Y-%m-%d %H:%M")} UTC ===')

        # Fetch all data sources
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

        # Build lookup by conditionId
        markets_by_id = {m['conditionId']: m for m in markets}

        # Pre-filter: match news to markets
        clusters = match_news_to_markets(news, markets)
        if not clusters:
            print('No market-news clusters found')
            return

        # Stage 1: shortlist
        shortlisted_ids = stage1_shortlist(clusters, knowledge, metaculus, gdelt, eia, sis)
        if not shortlisted_ids:
            print('No shortlisted markets')
            return

        # Stage 2: score
        raw_signals = stage2_score(shortlisted_ids, clusters, knowledge, news)
        if not raw_signals:
            print('No signals from stage 2')
            return

        # Validate, deduplicate, save
        real_count = 0
        paper_count = 0

        for raw in raw_signals:
            valid, reason, signal = validate_signal(raw, markets_by_id)
            if not valid:
                print(f'INVALID signal: {reason}')
                continue

            condition_id = signal.get('conditionId')
            direction = signal.get('direction')

            # Deduplication check
            if signal_already_exists(condition_id, direction):
                print(f'DUPLICATE skipped: {signal["question"][:50]}')
                continue

            save_signal(signal)

            if signal.get('betType') == 'real':
                real_count += 1
                alert = build_alert(signal, news)
                send_telegram(alert)
                print(f'REAL ALERT sent: {signal["question"][:60]}')

            elif signal.get('betType') == 'paper':
                if not paper_trade_already_exists(condition_id, direction):
                    paper_count += 1
                    save_paper_trade(signal)
                    print(f'Paper trade: {signal["question"][:60]}')
                else:
                    print(f'Paper trade already open: {signal["question"][:50]}')

        print(f'=== Scan complete: {real_count} real alerts, {paper_count} paper trades ===')

    except Exception as e:
        print(f'SCAN ERROR: {e}')
        send_telegram(f'⚠️ PIT Scanner error: {str(e)[:100]}')
    finally:
        _is_running = False

    # Resolve paper trades
    resolve_paper_trades()

# ── Schedule ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('PIT Scanner v2 starting...')
    send_telegram(
        '🟢 *PIT Scanner v2 started*\n\n'
        'Improvements:\n'
        '✓ Two-stage signal detection\n'
        '✓ Hard market validation\n'
        '✓ Deduplication\n'
        '✓ Fixed resolution logic\n'
        '✓ Scan lock\n'
        '✓ 12 news sources + Metaculus + GDELT + EIA + SIS\n\n'
        'Scanning every 30 min 24/7'
    )
    run_scan()
    schedule.every(30).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(60)
