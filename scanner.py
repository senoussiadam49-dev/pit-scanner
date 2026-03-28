import os
import time
import schedule
import requests
import feedparser
import anthropic
from supabase import create_client
from datetime import datetime, timedelta
import json

# ── Config ────────────────────────────────────────────────────────────────
ANTHROPIC_KEY = os.environ.get('ANTHROPIC_API_KEY')
SUPABASE_URL = os.environ.get('SUPABASE_URL')
SUPABASE_KEY = os.environ.get('SUPABASE_KEY')
TG_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN')
TG_CHAT = os.environ.get('TELEGRAM_CHAT_ID')

sb = create_client(SUPABASE_URL, SUPABASE_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

GAMMA_API = 'https://gamma-api.polymarket.com'

# ── RSS Sources ───────────────────────────────────────────────────────────
RSS_FEEDS = [
    ('Reuters World', 'https://feeds.reuters.com/reuters/worldNews'),
    ('AP News', 'https://rsshub.app/apnews/topics/apf-intlnews'),
    ('BBC World', 'http://feeds.bbci.co.uk/news/world/rss.xml'),
    ('Al Jazeera', 'https://www.aljazeera.com/xml/rss/all.xml'),
    ('Middle East Eye', 'https://www.middleeasteye.net/rss'),
    ('Defense One', 'https://www.defenseone.com/rss/all/'),
    ('TechCrunch', 'https://techcrunch.com/feed/'),
    ('The Verge', 'https://www.theverge.com/rss/index.xml'),
]

# ── Fetch Polymarket markets ───────────────────────────────────────────────
def get_active_markets():
    try:
        r = requests.get(f'{GAMMA_API}/markets', params={
            'active': 'true',
            'closed': 'false',
            'limit': 100,
            'order': 'volume',
            'ascending': 'false'
        }, timeout=15)
        data = r.json()
        return data if isinstance(data, list) else data.get('markets', [])
    except Exception as e:
        print(f'Error fetching markets: {e}')
        return []

# ── Fetch RSS news ─────────────────────────────────────────────────────────
def get_recent_news():
    headlines = []
    cutoff = datetime.utcnow() - timedelta(hours=6)
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                headlines.append({
                    'source': source,
                    'title': entry.get('title', ''),
                    'summary': entry.get('summary', '')[:200],
                    'link': entry.get('link', ''),
                })
        except Exception as e:
            print(f'RSS error {source}: {e}')
    return headlines

# ── Load knowledge base + lessons ─────────────────────────────────────────
def get_knowledge_context():
    try:
        knowledge = sb.table('pit_knowledge').select('*').order('created_at', desc=True).limit(10).execute()
        lessons = sb.table('pit_lessons').select('*').order('created_at', desc=True).limit(20).execute()
        lines = []
        if knowledge.data:
            lines.append('=== KNOWLEDGE BASE ===')
            for k in knowledge.data:
                lines.append(f'[{k["category"].upper()}] {k["title"]}: {k["content"][:500]}')
        if lessons.data:
            lines.append('=== LESSONS LEARNED ===')
            for l in lessons.data:
                tag = f'[{l["market_type"].upper()}] ' if l.get('market_type') else ''
                lines.append(f'{tag}{l["lesson"]}')
        return '\n'.join(lines)
    except Exception as e:
        print(f'Knowledge error: {e}')
        return ''

# ── Send Telegram ──────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=10
        )
    except Exception as e:
        print(f'Telegram error: {e}')

# ── Save paper trade ───────────────────────────────────────────────────────
def save_paper_trade(signal):
    try:
        sb.table('pit_paper_trades').insert({
            'market_question': signal.get('question'),
            'condition_id': signal.get('conditionId'),
            'token_id': signal.get('tokenId'),
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

# ── Save signal to pit_signals ─────────────────────────────────────────────
def save_signal(signal):
    try:
        sb.table('pit_signals').insert({
            'market_question': signal.get('question'),
            'condition_id': signal.get('conditionId'),
            'token_id': signal.get('tokenId'),
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

# ── Resolve paper trades ───────────────────────────────────────────────────
def resolve_paper_trades():
    try:
        open_trades = sb.table('pit_paper_trades').select('*').eq('status', 'OPEN').execute()
        if not open_trades.data:
            return
        for trade in open_trades.data:
            if not trade.get('condition_id'):
                continue
            try:
                r = requests.get(f'{GAMMA_API}/markets/{trade["condition_id"]}', timeout=10)
                market = r.json()
                if market.get('closed') or market.get('resolved'):
                    outcome = 'YES' if market.get('outcomePrices', [0])[0] > 0.99 else 'NO'
                    won = outcome == trade['direction']
                    pnl = round(trade['stake'] * (1 / (trade['market_odds'] / 100) - 1), 2) if won else -trade['stake']

                    # Auto-extract lesson via Claude
                    lesson = extract_lesson(trade, outcome, won)

                    sb.table('pit_paper_trades').update({
                        'status': 'CLOSED',
                        'outcome': outcome,
                        'pnl': pnl,
                        'lesson': lesson,
                        'resolved_at': datetime.utcnow().isoformat()
                    }).eq('id', trade['id']).execute()

                    # Save lesson permanently
                    if lesson:
                        sb.table('pit_lessons').insert({
                            'lesson': lesson,
                            'lesson_type': 'auto',
                            'source': f'Paper trade resolution: {trade["market_question"][:50]}'
                        }).execute()

                    result = 'WON' if won else 'LOST'
                    send_telegram(f'📊 *PAPER TRADE RESOLVED*\n{trade["market_question"][:70]}\n{trade["direction"]} → {outcome} | {result} | P&L: ${pnl}\n\n💡 {lesson[:100] if lesson else ""}')
                    print(f'Resolved: {trade["market_question"][:60]} → {outcome}')
            except Exception as e:
                print(f'Resolution error for {trade.get("id")}: {e}')
    except Exception as e:
        print(f'Resolve paper trades error: {e}')

def extract_lesson(trade, outcome, won):
    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=200,
            messages=[{
                'role': 'user',
                'content': f'''Generate one specific, actionable lesson (1-2 sentences) from this resolved paper trade.

Market: "{trade["market_question"]}"
Direction: {trade["direction"]}
My P estimate: {trade["true_p"]}%
Market odds: {trade["market_odds"]}%
Edge claimed: {trade["edge_pp"]}pp
Outcome: {outcome}
Won: {won}
Thesis: {trade.get("thesis", "")}

Return ONLY the lesson text, nothing else.'''
            }]
        )
        return msg.content[0].text.strip()
    except:
        return ''

# ── Main scan ──────────────────────────────────────────────────────────────
def run_scan():
    print(f'\n=== PIT SCAN {datetime.utcnow().strftime("%Y-%m-%d %H:%M")} ===')

    markets = get_active_markets()
    news = get_recent_news()
    knowledge = get_knowledge_context()

    if not markets:
        print('No markets fetched')
        return

    # Build market summary
    market_summary = '\n'.join([
        f'- "{m.get("question", "")}" | YES: {m.get("outcomePrices", ["?"])[0]} | Vol: ${int(float(m.get("volume", 0))):,} | Ends: {m.get("endDate", "?")[:10]} | ID: {m.get("conditionId", "")}'
        for m in markets[:50]
    ])

    # Build news summary
    news_summary = '\n'.join([
        f'[{n["source"]}] {n["title"]}'
        for n in news[:30]
    ])

    prompt = f'''{knowledge}

You are a Polymarket intelligence scanner. Analyse the news and markets below.

Find opportunities where:
1. Breaking news hasn't been priced into Polymarket odds yet (slow update edge)
2. Second-order consequences of events that most bettors haven't connected yet
3. Markets resolving in 24-48h where you have high confidence

ACTIVE POLYMARKET MARKETS (sorted by volume):
{market_summary}

BREAKING NEWS (last 6 hours):
{news_summary}

For each opportunity found, return JSON inside <signals> tags:
<signals>
[
  {{
    "question": "exact market question",
    "conditionId": "condition id from market list",
    "tokenId": "first token id if available",
    "direction": "YES or NO",
    "marketOdds": 45,
    "trueP": 62,
    "edgePp": 17,
    "conviction": 8,
    "resolutionRisk": 2,
    "edgeType": "slow_update or second_order or info_lag",
    "resolutionDate": "YYYY-MM-DD",
    "thesis": "one sentence explaining the edge",
    "betType": "real or paper"
  }}
]
</signals>

Rules:
- conviction 1-10, only include >= 6
- resolutionRisk 1-5, exclude >= 4
- betType "real" only if conviction >= 8 AND resolutionRisk <= 2 AND edge >= 7pp
- betType "paper" if conviction 6-7 OR resolutionRisk = 3
- Exclude elections, Fed decisions, slow political markets
- Max 8 signals
- If no genuine edge found, return <signals>[]</signals>'''

    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=3000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        response = msg.content[0].text

        import re
        match = re.search(r'<signals>(.*?)</signals>', response, re.DOTALL)
        if not match:
            print('No signals found')
            return

        signals = json.loads(match.group(1).strip())
        print(f'Found {len(signals)} signals')

        for signal in signals:
            save_signal(signal)

            if signal.get('betType') == 'real':
                # Send Telegram alert with Y/N
                msg_text = (
                    f'⚡ *PIT AUTO-BET PENDING*\n\n'
                    f'*{signal["question"][:80]}*\n\n'
                    f'{signal["direction"]} @ {signal["marketOdds"]}% | '
                    f'Your P: {signal["trueP"]}% | '
                    f'Edge: +{signal["edgePp"]}pp\n'
                    f'Conviction: {signal["conviction"]}/10 | RR: {signal["resolutionRisk"]}\n'
                    f'Resolves: {signal.get("resolutionDate", "?")}\n\n'
                    f'_{signal["thesis"]}_\n\n'
                    f'Reply *Y* to place $25 | *N* to skip\n'
                    f'⏳ 5 minute window'
                )
                send_telegram(msg_text)

                # Save to pit_signals as unprocessed
                sb.table('pit_signals').update({'processed': False}).eq(
                    'market_question', signal['question']
                ).execute()

            elif signal.get('betType') == 'paper':
                save_paper_trade(signal)
                print(f'Paper trade: {signal["question"][:60]}')

    except Exception as e:
        print(f'Scan error: {e}')

    # Also resolve any open paper trades
    resolve_paper_trades()

# ── Schedule ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('PIT Scanner starting...')
    send_telegram('🟢 *PIT Scanner started* — scanning every 30 min')
    run_scan()  # Run immediately on start
    schedule.every(30).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(60)
