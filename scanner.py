import os
import time
import schedule
import requests
import feedparser
import anthropic
from supabase import create_client
from datetime import datetime, timedelta
import json
import re

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
    for source, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:5]:
                headlines.append({
                    'source': source,
                    'title': entry.get('title', ''),
                    'summary': entry.get('summary', '')[:300],
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
            lines.append('=== KNOWLEDGE BASE (apply before every analysis) ===')
            for k in knowledge.data:
                lines.append(f'[{k["category"].upper()}] {k["title"]}: {k["content"][:500]}')
        if lessons.data:
            lines.append('=== LESSONS LEARNED (apply to every bet decision) ===')
            for l in lessons.data:
                tag = f'[{l["market_type"].upper()}] ' if l.get('market_type') else ''
                lines.append(f'{tag}{l["lesson"]}')
        if lines:
            lines.append('=== END KNOWLEDGE ===')
        return '\n'.join(lines)
    except Exception as e:
        print(f'Knowledge error: {e}')
        return ''

# ── Send Telegram ──────────────────────────────────────────────────────────
def send_telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        print('No Telegram config')
        return
    try:
        r = requests.post(
            f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'Markdown'},
            timeout=10
        )
        if not r.ok:
            print(f'Telegram error: {r.text}')
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

# ── Save signal ────────────────────────────────────────────────────────────
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
                    outcome = 'YES' if float((market.get('outcomePrices') or ['0'])[0]) > 0.99 else 'NO'
                    won = outcome == trade['direction']
                    pnl = round(trade['stake'] * (100 / trade['market_odds'] - 1), 2) if won else -trade['stake']
                    lesson = extract_lesson(trade, outcome, won)
                    sb.table('pit_paper_trades').update({
                        'status': 'CLOSED',
                        'outcome': outcome,
                        'pnl': pnl,
                        'lesson': lesson,
                        'resolved_at': datetime.now(datetime.UTC).isoformat()
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
                        f'💡 *Lesson learned:*\n_{lesson[:200] if lesson else "None extracted"}_'
                    )
                    print(f'Resolved: {trade["market_question"][:60]} → {outcome} | {result}')
            except Exception as e:
                print(f'Resolution error {trade.get("id")}: {e}')
    except Exception as e:
        print(f'Resolve error: {e}')

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

# ── Build rich Telegram alert ──────────────────────────────────────────────
def build_alert(signal, news_items):
    # Find the news item that triggered this signal
    triggering_news = ''
    for n in news_items:
        title_lower = n['title'].lower()
        question_lower = signal.get('question', '').lower()
        keywords = [w for w in question_lower.split() if len(w) > 4]
        if any(k in title_lower for k in keywords[:3]):
            triggering_news = f'[{n["source"]}] {n["title"]}'
            break

    market_url = f'https://polymarket.com/event/{signal.get("conditionId", "")}'

    alert = (
        f'⚡ *PIT AUTO-BET PENDING*\n\n'
        f'*{signal["question"][:100]}*\n\n'
        f'*Direction:* {signal["direction"]} @ {signal["marketOdds"]}%\n'
        f'*True P:* {signal["trueP"]}% | *Edge:* +{signal["edgePp"]}pp\n'
        f'*Conviction:* {signal["conviction"]}/10 | *RR:* {signal["resolutionRisk"]}/5\n'
        f'*Resolves:* {signal.get("resolutionDate", "?")}\n\n'
        f'📰 *Why NOW:*\n_{triggering_news or "Multiple signals converging"}_\n\n'
        f'🎯 *Edge:* _{signal.get("thesis", "")}_\n\n'
        f'⚠️ *Bear case:* _{signal.get("bearCase", "Not specified")}_\n\n'
        f'📋 *Resolution criteria:* _{signal.get("resolutionCriteria", "Check Polymarket")}_\n\n'
        f'🔗 {market_url}\n\n'
        f'Reply *Y* to place $25 | *N* to skip\n'
        f'⏳ 5 minute window'
    )
    return alert

# ── Main scan ──────────────────────────────────────────────────────────────
def run_scan():
    print(f'\n=== PIT SCAN {datetime.now().strftime("%Y-%m-%d %H:%M")} ===')

    markets = get_active_markets()
    news = get_recent_news()
    knowledge = get_knowledge_context()

    if not markets:
        print('No markets fetched')
        return

    print(f'Markets: {len(markets)} | News: {len(news)} | Knowledge: {len(knowledge)} chars')

    market_summary = '\n'.join([
        f'- "{m.get("question", "")}" | YES: {(m.get("outcomePrices") or ["?"])[0]} | Vol: ${int(float(m.get("volume") or 0)):,} | Ends: {str(m.get("endDate", "?"))[:10]} | ID: {m.get("conditionId", "")}'
        for m in markets[:50]
    ])

    news_summary = '\n'.join([
        f'[{n["source"]}] {n["title"]} — {n["summary"][:150]}'
        for n in news[:30]
    ])

    prompt = f'''{knowledge}

You are a Polymarket intelligence scanner with deep knowledge of prediction market edges.

ACTIVE POLYMARKET MARKETS (by volume):
{market_summary}

BREAKING NEWS (last 6 hours):
{news_summary}

Apply the knowledge base and lessons above before making any decisions.

Find opportunities where breaking news has NOT yet been priced into Polymarket odds.
Focus on:
1. Slow update edge — news broke <6h ago, odds unchanged
2. Second-order consequences — downstream effects not yet connected
3. Markets resolving in 24-72h with clear edge

For each opportunity, return JSON inside <signals> tags:
<signals>
[
  {{
    "question": "exact market question from the list above",
    "conditionId": "condition id",
    "tokenId": "",
    "direction": "YES or NO",
    "marketOdds": 65,
    "trueP": 25,
    "edgePp": 40,
    "conviction": 8,
    "resolutionRisk": 2,
    "edgeType": "slow_update",
    "resolutionDate": "2026-03-31",
    "thesis": "one sentence: what the market is getting wrong and why",
    "bearCase": "one sentence: what would prove this wrong",
    "resolutionCriteria": "how does this market actually resolve — exact criteria",
    "newsSource": "which RSS source triggered this",
    "betType": "real or paper"
  }}
]
</signals>

Rules:
- conviction 1-10, only include >= 6
- resolutionRisk 1-5, exclude >= 4  
- betType "real" ONLY if conviction >= 8 AND resolutionRisk <= 2 AND edgePp >= 7
- betType "paper" if conviction 6-7 OR resolutionRisk = 3
- Exclude elections, Fed decisions, slow political markets
- Apply all lessons from knowledge base before deciding
- Max 6 signals
- If no genuine edge: return <signals>[]</signals>'''

    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=3000,
            messages=[{'role': 'user', 'content': prompt}]
        )
        response = msg.content[0].text
        match = re.search(r'<signals>(.*?)</signals>', response, re.DOTALL)
        if not match:
            print('No signals found')
            return

        signals = json.loads(match.group(1).strip())
        print(f'Found {len(signals)} signals')

        real_count = 0
        paper_count = 0

        for signal in signals:
            save_signal(signal)

            if signal.get('betType') == 'real':
                real_count += 1
                alert = build_alert(signal, news)
                send_telegram(alert)
                print(f'REAL BET ALERT: {signal["question"][:60]}')

            elif signal.get('betType') == 'paper':
                paper_count += 1
                save_paper_trade(signal)
                print(f'Paper trade: {signal["question"][:60]}')

        print(f'Scan complete — {real_count} real alerts, {paper_count} paper trades')

        if real_count == 0 and paper_count == 0:
            print('No qualifying signals this scan')

    except json.JSONDecodeError as e:
        print(f'JSON parse error: {e}')
    except Exception as e:
        print(f'Scan error: {e}')

    resolve_paper_trades()

# ── Schedule ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    print('PIT Scanner starting...')
    send_telegram('🟢 *PIT Scanner started* — scanning every 30 min\nSources: Reuters, AP, BBC, Al Jazeera, Middle East Eye, Defense One, TechCrunch, The Verge + Polymarket live odds')
    run_scan()
    schedule.every(30).minutes.do(run_scan)
    while True:
        schedule.run_pending()
        time.sleep(60)
