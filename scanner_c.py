import os
import time
import schedule
import requests
import anthropic
from supabase import create_client
from datetime import datetime, timezone, timedelta
import json
import re

# ── Config ────────────────────────────────────────────────────────────────
REQUIRED_ENV = ['ANTHROPIC_API_KEY', 'SUPABASE_URL', 'SUPABASE_KEY', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID']
for key in REQUIRED_ENV:
    if not os.environ.get(key):
        raise RuntimeError(f'Missing required env var: {key}')

ANTHROPIC_KEY = os.environ['ANTHROPIC_API_KEY']
SUPABASE_URL  = os.environ['SUPABASE_URL']
SUPABASE_KEY  = os.environ['SUPABASE_KEY']
TG_TOKEN      = os.environ['TELEGRAM_BOT_TOKEN']
TG_CHAT       = os.environ['TELEGRAM_CHAT_ID']

sb     = create_client(SUPABASE_URL, SUPABASE_KEY)
claude = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

GAMMA_API = 'https://gamma-api.polymarket.com'

# ── Requests session with retries ─────────────────────────────────────────
session = requests.Session()
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
retry = Retry(total=3, backoff_factor=1, status_forcelist=[429, 500, 502, 503, 504])
session.mount('https://', HTTPAdapter(max_retries=retry))

def now_utc():
    return datetime.now(timezone.utc)

def now_utc_iso():
    return now_utc().isoformat()

# ── Scanner C constants ───────────────────────────────────────────────────

# Probability thresholds — wider net, more opportunities
C_MIN_PROB     = 87.0   # market must be ≥87% in one direction
C_MAX_PROB     = 97.0   # cap at 97%
C_MIN_VOLUME   = 5000   # minimum $5k volume
C_MAX_DAYS     = 30     # max days to resolution
C_MIN_DAYS     = 0      # min days (includes today)
C_MIN_EDGE_PP  = 2.0    # minimum net edge in pp
C_MIN_BET      = 2.0    # min bet size $
C_MAX_BET      = 5.0    # max bet size $ (calibration phase)
C_CALIB_TRADES = 30     # paper trades needed before real money
C_BRIER_TARGET = 0.15   # Brier score needed to unlock real bets

# Resolution gate — auto-reject if any found in rules text
# These indicate Polymarket can resolve however they want
RED_FLAG_WORDS = [
    'polymarket discretion', 'may determine', 'reserves the right',
    'at our discretion', 'polymarket may', 'admin may',
    'polymarket will decide', 'at the sole discretion', 'subjective',
    'may use', 'may consider', 'at polymarket'
]

# Warning words — these create resolution ambiguity
# Market flagged as WARN — Claude must explicitly address each one
WARNING_WORDS = [
    'intercept', 'partial', 'substantially', 'approximately',
    'deemed', 'considered', 'qualify', 'unless', 'except',
    'provided that', 'subject to', 'may not', 'does not include',
    'excluding', 'regardless'
]

# Hard exclusions — these market types never have clean edges
C_EXCLUDED = [
    # Sports outcomes
    'exact score', 'end in a draw', 'spread:', 'o/u ', 'over/under',
    'handicap', 'half time', 'first goal', 'clean sheet',
    'nfl', 'nba', 'mlb', 'nhl', 'ufc', 'boxing', 'wrestling',
    'basketball', 'baseball', 'hockey', 'golf', 'formula 1', ' f1 ',
    'valorant', 'esport', 'dota', 'league of legends', 'cs2', 'overwatch',
    'wimbledon', 'premier league', 'champions league', 'europa league',
    'la liga', 'serie a', 'bundesliga', 'ligue 1', 'eredivisie',
    'win on 2026', 'win the 2025', 'win the 2026',
    ' fk ', ' fc ', ' cf ', ' afc ', ' bc ', ' sc ',
    # Entertainment
    'oscar', 'grammy', 'emmy', 'golden globe', 'academy award',
    'top chef', 'reality show', 'season finale',
    # Slow political
    'election', 'vote', 'ballot', 'referendum',
    'republican nominee', 'democratic nominee',
    'senate race', 'house race', 'primary',
    # Crypto price targets (too noisy)
    'market cap hit', 'reach $', 'hit $',
    # Weather
    'highest temperature', 'lowest temperature', 'rainfall',
    # Social media counts
    'tweets from', 'posts from', 'followers',
]

# Categories that get fee-free treatment on Polymarket
GEO_KEYWORDS = [
    'ceasefire', 'sanctions', 'strait', 'hormuz', 'war', 'conflict',
    'military', 'diplomatic', 'treaty', 'invasion', 'attack', 'strike',
    'iran', 'israel', 'ukraine', 'russia', 'china', 'taiwan', 'houthi',
    'nato', 'un security', 'peace deal', 'withdrawal', 'troops'
]

# ── Scan lock ─────────────────────────────────────────────────────────────
_is_running_c = False
# ── Telegram ──────────────────────────────────────────────────────────────
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


# ── Market helpers ────────────────────────────────────────────────────────
def is_excluded(question):
    q = question.lower()
    return any(kw in q for kw in C_EXCLUDED)

def is_preferred(question):
    q = question.lower()
    return any(kw in q for kw in GEO_KEYWORDS)

def days_until(end_date_str):
    try:
        end = datetime.strptime(end_date_str, '%Y-%m-%d').date()
        delta = end - now_utc().date()
        return delta.days
    except:
        return 999

def classify_category(question):
    """Returns (category_name, fee_pp) — geopolitics is fee-free."""
    q = question.lower()
    geo = ['ceasefire', 'sanctions', 'strait', 'hormuz', 'war', 'conflict',
           'military', 'diplomatic', 'treaty', 'invasion', 'attack', 'iran',
           'israel', 'ukraine', 'russia', 'china', 'taiwan', 'houthi']
    if any(kw in q for kw in geo):
        return 'geopolitics', 0.0
    crypto = ['bitcoin', 'btc', 'ethereum', 'eth', 'crypto', 'solana']
    if any(kw in q for kw in crypto):
        return 'crypto', 0.75
    return 'other', 0.75


# ── Deduplication ─────────────────────────────────────────────────────────
def signal_already_exists(condition_id, direction):
    try:
        today = now_utc().strftime('%Y-%m-%d')
        r = sb.table('pit_signals').select('id') \
            .eq('condition_id', condition_id) \
            .eq('direction', direction) \
            .gte('created_at', today).execute()
        return len(r.data) > 0
    except:
        return False

def paper_trade_already_exists(condition_id, direction):
    try:
        r = sb.table('pit_paper_trades').select('id') \
            .eq('condition_id', condition_id) \
            .eq('direction', direction) \
            .eq('status', 'OPEN').execute()
        return len(r.data) > 0
    except:
        return False


# ── Stability check ───────────────────────────────────────────────────────
def was_seen_last_scan(condition_id):
    """
    Returns True if this market was at 94%+ in the previous scan.
    Stability = crowd has validated this price for 2+ consecutive scans.
    """
    try:
        cutoff = (now_utc() - timedelta(hours=2)).isoformat()
        r = sb.table('pit_scanner_c_candidates').select('id') \
            .eq('condition_id', condition_id) \
            .gte('seen_at', cutoff).execute()
        return len(r.data) > 0
    except:
        return False

def log_candidate(condition_id, yes_pct):
    """Log that we saw this market at this probability this scan."""
    try:
        sb.table('pit_scanner_c_candidates').insert({
            'condition_id': condition_id,
            'yes_pct': yes_pct,
            'seen_at': now_utc_iso()
        }).execute()
    except:
        pass


# ── Save signal & paper trade ─────────────────────────────────────────────
def save_signal(signal):
    try:
        sb.table('pit_signals').insert({
            'market_question': signal.get('question'),
            'condition_id':    signal.get('conditionId'),
            'token_id':        '',
            'direction':       signal.get('direction'),
            'market_odds':     signal.get('marketOdds'),
            'true_p':          signal.get('trueP'),
            'edge_pp':         signal.get('edgePp'),
            'conviction':      8,
            'resolution_risk': signal.get('resolutionRisk', 1),
            'edge_type':       'high_probability',
            'resolution_date': signal.get('resolutionDate') or None,
            'thesis':          signal.get('thesis'),
            'kelly_stake':     signal.get('kellyStake', 2.0),
        }).execute()
    except Exception as e:
        print(f'Signal save error: {e}')

def save_paper_trade(signal):
    try:
        sb.table('pit_paper_trades').insert({
            'market_question': signal.get('question'),
            'condition_id':    signal.get('conditionId'),
            'token_id':        '',
            'direction':       signal.get('direction'),
            'market_odds':     signal.get('marketOdds'),
            'true_p':          signal.get('trueP'),
            'edge_pp':         signal.get('edgePp'),
            'stake':           signal.get('kellyStake', C_MIN_BET),
            'resolution_date': signal.get('resolutionDate'),
            'thesis':          signal.get('thesis'),
            'status':          'OPEN',
            'scanner':         'C'
        }).execute()
        print(f'Paper trade saved: {signal.get("question", "")[:60]}')
    except Exception as e:
        print(f'Paper trade save error: {e}')
      # ── Fetch markets ─────────────────────────────────────────────────────────
def get_active_markets(limit=300):
    try:
        r = session.get(f'{GAMMA_API}/markets', params={
            'active':    'true',
            'closed':    'false',
            'limit':     limit,
            'order':     'volume',
            'ascending': 'false'
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
        markets = data if isinstance(data, list) else data.get('markets', [])

        clean = []
        for m in markets:
            try:
                prices   = m.get('outcomePrices') or []
                outcomes = m.get('outcomes') or ['YES', 'NO']
                price_map = {}
                for i, outcome in enumerate(outcomes):
                    if i < len(prices):
                        try:
                            price_map[str(outcome).upper()] = float(prices[i])
                        except:
                            pass

                # Real price: outcomePrices → bestAsk → fallback 0.5
                yes_price = None
                if price_map.get('YES', 0) not in [0, 0.5]:
                    yes_price = price_map.get('YES')
                if not yes_price:
                    try:
                        best_ask = float(m.get('bestAsk') or 0)
                        if best_ask not in [0, 0.5]:
                            yes_price = best_ask
                    except:
                        pass
                if not yes_price:
                    yes_price = price_map.get('YES', 0.5)

                yes_pct = round((yes_price or 0.5) * 100, 1)

                clean.append({
                    'question':    m.get('question', ''),
                    'conditionId': m.get('conditionId', ''),
                    'slug':        m.get('slug', ''),
                    'volume':      float(m.get('volume') or 0),
                    'yes_pct':     yes_pct,
                    'endDate':     str(m.get('endDate', ''))[:10],
                    'resolved':    m.get('resolved', False),
                    'closed':      m.get('closed', False),
                    'description': m.get('description', ''),
                })
            except:
                continue

        print(f'Scanner C: fetched {len(clean)} markets')
        return clean
    except Exception as e:
        print(f'ERROR fetching markets: {e}')
        return []


# ── Resolution rules — fetch, validate, parse ─────────────────────────────

# Rules must contain at least one of these to be considered valid
RULES_VALIDATORS = [
    'resolve', 'resolution', 'will resolve',
    'yes if', 'no if', 'resolves yes', 'resolves no',
    'criteria', 'determined by', 'based on',
    'this market', 'market will', 'settle'
]

def fetch_and_validate_rules(market):
    """
    Fetch resolution rules from Gamma API and validate they are real rules.
    
    Returns: (rules_text, valid, reason)
    - rules_text: the actual rules string
    - valid: True if rules look genuine
    - reason: why invalid if not valid
    
    Three attempts:
    1. Description field from bulk fetch (already in market object)
    2. Slug-based fetch (more complete data)
    3. ConditionId-based fetch (final fallback)
    """
    question = market.get('question', '')[:50]

    # Attempt 1 — description already in market object
    description = market.get('description', '') or ''
    if _is_valid_rules(description):
        return description, True, 'ok'

    # Attempt 2 — fetch by slug
    try:
        slug = market.get('slug', '')
        if slug:
            r = session.get(
                f'{GAMMA_API}/markets',
                params={'slug': slug},
                timeout=10
            )
            data = r.json()
            m = data[0] if isinstance(data, list) and data else {}
            desc = m.get('description', '') or ''
            if _is_valid_rules(desc):
                return desc, True, 'ok'
    except Exception as e:
        print(f'  Rules slug fetch error: {e}')

    # Attempt 3 — fetch by conditionId
    try:
        condition_id = market.get('conditionId', '')
        if condition_id:
            r = session.get(
                f'{GAMMA_API}/markets',
                params={'conditionId': condition_id},
                timeout=10
            )
            data = r.json()
            markets = data if isinstance(data, list) else data.get('markets', [])
            if markets:
                desc = markets[0].get('description', '') or ''
                if _is_valid_rules(desc):
                    return desc, True, 'ok'
    except Exception as e:
        print(f'  Rules conditionId fetch error: {e}')

    # All attempts failed
    if description and len(description) > 50:
        # Has text but doesn't look like rules
        return description, False, f'Text found but no resolution language detected: "{description[:80]}"'

    return '', False, 'No resolution rules found after 3 fetch attempts'


def _is_valid_rules(text):
    """
    Check if text actually contains resolution rules.
    Must be >100 chars AND contain at least one resolution keyword.
    """
    if not text or len(text) < 100:
        return False
    text_lower = text.lower()
    return any(v in text_lower for v in RULES_VALIDATORS)


def parse_rules(rules_text):
    """
    Parse resolution rules for red flags and warnings.
    
    RED FLAGS → automatic REJECT (Polymarket has discretion)
    WARNINGS → flag to Claude, must explicitly address each one
    
    Returns: ('CLEAN'|'WARN'|'REJECT', list_of_flags)
    """
    if not rules_text:
        return 'REJECT', ['Empty rules text']

    text = rules_text.lower()

    # Check red flags first — any red flag = automatic reject
    red_flags = [w for w in RED_FLAG_WORDS if w in text]
    if red_flags:
        return 'REJECT', red_flags

    # Check warning words — these need Claude to explicitly address them
    warnings = [w for w in WARNING_WORDS if w in text]

    # More than 3 warnings = too ambiguous even for Claude
    if len(warnings) >= 3:
        return 'REJECT', warnings

    if warnings:
        return 'WARN', warnings

    return 'CLEAN', []


# ── Claude probability estimation ─────────────────────────────────────────
def estimate_probability(question, direction, rules_text, yes_pct, days_left, warnings):
    """
    Claude reads the actual resolution rules (fetched from Gamma API)
    and does one web search to estimate true probability.

    Claude cannot hallucinate rules — they are provided directly.
    Claude must explicitly address any warning words found in rules.

    Returns: (true_p_pct, edge_pp, pass_fail, reason)
    """
    try:
        market_odds = yes_pct if direction == 'YES' else round(100 - yes_pct, 1)
        warning_instruction = ''
        if warnings:
            warning_instruction = f'''
IMPORTANT — The resolution rules contain ambiguous language: {warnings}
You MUST explicitly address each of these in your analysis.
Explain whether they help or hurt the {direction} position.
If any creates genuine resolution risk — return FAIL.'''

        prompt = f"""You are evaluating a Polymarket prediction market position.

MARKET: "{question}"
CURRENT PRICE: {direction} @ {market_odds}%
DAYS TO RESOLUTION: {days_left}

RESOLUTION RULES (fetched directly from Polymarket API — use ONLY these rules, do not search for or modify them):
---
{rules_text}
---
{warning_instruction}

YOUR TASK:
1. Do ONE web search: "{question} latest news {days_left} days"
2. Based on what you find AND the resolution rules above:
   - Does current reality satisfy the resolution criteria for {direction}?
   - Has anything changed in the last 48-72 hours that affects this?
   - Are there any edge cases in the rules that could cause unexpected resolution?

3. Estimate true probability of {direction} resolving correctly.

4. Apply a 10pp conservative haircut to your estimate.

Return EXACTLY this format and nothing else:
TRUE_P: [number between 0 and 100]
EDGE: [true_p minus {market_odds} — can be negative]
METHOD: [base_rate OR decomposition OR resolution_technicality]
REASON: [one sentence explaining the edge or lack of edge]
WARNINGS_ADDRESSED: [one sentence addressing the ambiguous language, or 'none']
RESULT: PASS or FAIL
FAIL_REASON: [only if FAIL — one sentence why]

RESULT is PASS only if:
- Edge >= 2pp after haircut
- No unresolvable ambiguity in rules
- Web search confirms current reality supports {direction}
- You are genuinely confident this resolves {direction}

RESULT is FAIL if:
- Edge < 2pp
- Rules are ambiguous in a way that could hurt {direction}
- Web search found something that threatens {direction}
- You are not confident"""

        msg = claude.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=400,
            tools=[{'type': 'web_search_20250305', 'name': 'web_search'}],
            messages=[{'role': 'user', 'content': prompt}]
        )

        # Extract text from response
        response = ''
        for block in msg.content:
            if hasattr(block, 'type') and block.type == 'text':
                response += block.text

        # Parse structured response
        true_p_match  = re.search(r'TRUE_P:\s*(\d+\.?\d*)', response)
        edge_match    = re.search(r'EDGE:\s*(-?\d+\.?\d*)', response)
        result_match  = re.search(r'RESULT:\s*(PASS|FAIL)', response, re.IGNORECASE)
        reason_match  = re.search(r'REASON:\s*(.+?)(?=\n|WARNINGS|RESULT|$)', response)
        fail_match    = re.search(r'FAIL_REASON:\s*(.+?)(?=\n|$)', response)

        # If format not parseable — skip this market, log for debugging
        if not true_p_match or not result_match:
            print(f'  Claude format error — response: {response[:150]}')
            return None, None, 'SKIP', 'Could not parse Claude response'

        true_p    = float(true_p_match.group(1))
        edge      = float(edge_match.group(1)) if edge_match else round(true_p - market_odds, 1)
        result    = result_match.group(1).upper()
        reason    = reason_match.group(1).strip() if reason_match else 'No reason given'
        fail_reason = fail_match.group(1).strip() if fail_match else ''

        print(f'  Claude: TRUE_P={true_p}% EDGE={edge}pp RESULT={result} — {reason[:80]}')

        if result == 'FAIL':
            return true_p, edge, 'FAIL', fail_reason or reason

        return true_p, edge, 'PASS', reason

    except Exception as e:
        print(f'  Claude estimation error: {e}')
        # On error — skip this market rather than blindly approving
        return None, None, 'SKIP', f'Error: {str(e)[:50]}'
  # ══════════════════════════════════════════════════════════════════════════
# MATH ENGINE — pure Python, no AI
# ══════════════════════════════════════════════════════════════════════════




def calculate_kelly(true_p, q, bankroll):
    """
    Self-calibrating Kelly position sizing.

    Kelly fraction scales with both:
    1. Mathematical edge (true_p vs market price)
    2. Scanner's proven accuracy (Brier score)

    During calibration: ultra-conservative (15-25% of full Kelly)
    After calibration: up to half Kelly as accuracy is proven
    If Brier gets worse: Kelly multiplier drops back automatically
    """
    if q <= 0 or q >= 1 or true_p <= q:
        return 0.0

    b = (1.0 - q) / q
    if b <= 0:
        return 0.0

    f_star = (true_p * (b + 1) - 1) / b
    if f_star <= 0:
        return 0.0

    cal = get_calibration()
    brier = float(cal.get('brier_score') or 0.25)
    trades_done = int(cal.get('trades_resolved') or 0)
    is_calibrated = bool(cal.get('is_calibrated') or False)

    # Kelly multiplier scales with proven accuracy
    if trades_done < 10:
        kelly_mult = 0.15   # ultra conservative — learning phase
    elif trades_done < 20:
        kelly_mult = 0.20
    elif not is_calibrated:
        kelly_mult = 0.25   # quarter Kelly during calibration
    elif brier < 0.10:
        kelly_mult = 0.50   # half Kelly when excellent
    elif brier < 0.15:
        kelly_mult = 0.35   # above quarter Kelly when good
    else:
        kelly_mult = 0.25   # back to quarter Kelly if Brier slips

    f_scaled = f_star * kelly_mult
    raw      = f_scaled * bankroll

    # Hard cap during calibration, bankroll % cap after
    if not is_calibrated:
        position = max(C_MIN_BET, min(raw, C_MAX_BET))
    else:
        max_bet  = bankroll * 0.08
        position = max(C_MIN_BET, min(raw, max_bet))

    return round(position, 2)


def get_bankroll():
    """Current bankroll = base $55 + cumulative paper P&L."""
    try:
        r = sb.table('pit_calibration').select('total_pnl').eq('id', 1).execute()
        if r.data:
            pnl = float(r.data[0].get('total_pnl') or 0)
            return max(55.0 + pnl, 10.0)
    except:
        pass
    return 55.0
  # ══════════════════════════════════════════════════════════════════════════
# CALIBRATION SYSTEM
# ══════════════════════════════════════════════════════════════════════════

def get_calibration():
    """
    Get current calibration state from Supabase.
    Creates the row if it doesn't exist yet.
    """
    try:
        r = sb.table('pit_calibration').select('*').eq('id', 1).execute()
        if r.data:
            return r.data[0]
        # First run — initialise
        initial = {
            'id':               1,
            'trades_resolved':  0,
            'brier_score':      0.25,
            'brier_observations': [],
            'is_calibrated':    False,
            'total_pnl':        0.0,
            'wins':             0,
            'losses':           0,
        }
        sb.table('pit_calibration').insert(initial).execute()
        return initial
    except Exception as e:
        print(f'Calibration fetch error: {e}')
        return {
            'trades_resolved': 0, 'brier_score': 0.25,
            'brier_observations': [], 'is_calibrated': False,
            'total_pnl': 0.0, 'wins': 0, 'losses': 0
        }


def update_calibration(true_p_pct, won, pnl):
    """
    Update Brier score and P&L after a trade resolves.

    Brier score = mean( (predicted_p - actual_outcome)^2 )
    - actual_outcome = 1.0 if WON, 0.0 if LOST
    - predicted_p    = true_p at time of trade (0.0 to 1.0)
    - Perfect score  = 0.0
    - Random guess   = 0.25
    - Target         = < 0.15 to unlock real money
    """
    try:
        cal = get_calibration()
        observations = cal.get('brier_observations') or []

        # New Brier observation
        predicted = true_p_pct / 100.0
        actual    = 1.0 if won else 0.0
        new_obs   = round((predicted - actual) ** 2, 6)
        observations.append(new_obs)

        # Recalculate
        brier          = sum(observations) / len(observations)
        trades_resolved = len(observations)
        wins           = (cal.get('wins') or 0) + (1 if won else 0)
        losses         = (cal.get('losses') or 0) + (0 if won else 1)
        total_pnl      = round((cal.get('total_pnl') or 0) + pnl, 2)
        is_calibrated  = (
            trades_resolved >= C_CALIB_TRADES and
            brier < C_BRIER_TARGET
        )

        sb.table('pit_calibration').update({
            'trades_resolved':    trades_resolved,
            'brier_score':        round(brier, 4),
            'brier_observations': observations,
            'is_calibrated':      is_calibrated,
            'total_pnl':          total_pnl,
            'wins':               wins,
            'losses':             losses,
        }).eq('id', 1).execute()

        win_rate = round(wins / trades_resolved * 100, 1) if trades_resolved else 0
        print(f'Calibration: {trades_resolved} trades | Brier: {brier:.4f} | Win rate: {win_rate}% | P&L: ${total_pnl:+.2f} | Calibrated: {is_calibrated}')
        return brier, trades_resolved, is_calibrated

    except Exception as e:
        print(f'Calibration update error: {e}')
        return 0.25, 0, False


def calibration_summary_line(cal):
    """One-line calibration status for Telegram messages."""
    trades  = cal.get('trades_resolved', 0)
    brier   = cal.get('brier_score', 0.25)
    is_cal  = cal.get('is_calibrated', False)
    pnl     = cal.get('total_pnl', 0.0) or 0.0
    wins    = cal.get('wins', 0) or 0
    losses  = cal.get('losses', 0) or 0
    win_rate = round(wins / trades * 100, 1) if trades else 0

    if is_cal:
        return f'✅ Calibrated | {trades} trades | Brier: {brier:.3f} | Win: {win_rate}% | P&L: ${pnl:+.2f}'
    else:
        remaining = max(C_CALIB_TRADES - trades, 0)
        return f'📊 Calibrating: {trades}/{C_CALIB_TRADES} | Brier: {brier:.3f} | Win: {win_rate}% | P&L: ${pnl:+.2f} | Need {remaining} more trades'
      # ══════════════════════════════════════════════════════════════════════════
# AUTO-RESOLVE & LESSON EXTRACTION
# ══════════════════════════════════════════════════════════════════════════

def extract_lesson(trade, resolution_context):
    """
    One Claude call per resolved trade — the only AI cost in Scanner C.
    Extracts a calibration lesson saved to KB for future reference.
    """
    try:
        msg = claude.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=300,
            messages=[{
                'role': 'user',
                'content': f"""Extract a calibration lesson from this resolved prediction market trade.

TRADE:
Market: "{trade.get('market_question')}"
Direction: {trade.get('direction')} @ {trade.get('market_odds')}%
True P estimate: {trade.get('true_p')}%
Edge claimed: {trade.get('edge_pp')}pp
Outcome: {resolution_context}
Thesis: {trade.get('thesis', 'not recorded')}

Format as exactly two lines:
LESSON: [2-3 sentences: what happened, was the thesis correct, one actionable rule]
PATTERN: [One sentence: what type of market this applies to in future]

Return ONLY the LESSON and PATTERN lines."""
            }]
        )
        return msg.content[0].text.strip()
    except Exception as e:
        print(f'Lesson extraction error: {e}')
        return ''


def save_lesson(lesson_text, trade):
    """Save LESSON and PATTERN lines to pit_lessons table."""
    if not lesson_text:
        return
    try:
        lesson_match  = re.search(r'LESSON:\s*(.+?)(?=PATTERN:|$)', lesson_text, re.DOTALL)
        pattern_match = re.search(r'PATTERN:\s*(.+)', lesson_text, re.DOTALL)
        lesson_str    = lesson_match.group(1).strip() if lesson_match else lesson_text
        pattern_str   = pattern_match.group(1).strip() if pattern_match else None

        sb.table('pit_lessons').insert({
            'lesson':      lesson_str,
            'lesson_type': 'auto',
            'market_type': 'high_probability',
            'source':      f'Scanner C: {trade.get("market_question", "")[:50]}'
        }).execute()

        if pattern_str:
            sb.table('pit_lessons').insert({
                'lesson':      f'PATTERN: {pattern_str}',
                'lesson_type': 'auto',
                'market_type': 'high_probability',
                'source':      f'Pattern from: {trade.get("market_question", "")[:50]}'
            }).execute()
    except Exception as e:
        print(f'Lesson save error: {e}')


def auto_resolve():
    """
    Daily job: check Gamma API for resolved paper trades.
    For each resolved trade:
      1. Determine outcome (YES/NO)
      2. Calculate P&L
      3. Update Brier score
      4. Extract lesson via Claude (one small call)
      5. Send Telegram notification
    """
    try:
        open_trades = sb.table('pit_paper_trades') \
            .select('*').eq('status', 'OPEN').execute()

        if not open_trades.data:
            print('Auto-resolve: no open paper trades')
            return

        print(f'Auto-resolve: checking {len(open_trades.data)} open trades')
        resolved_count = 0

        for trade in open_trades.data:
            condition_id = trade.get('condition_id')
            if not condition_id:
                continue

            try:
                r = session.get(
                    f'{GAMMA_API}/markets',
                    params={'conditionId': condition_id},
                    timeout=10
                )
                data    = r.json()
                markets = data if isinstance(data, list) else data.get('markets', [])
                if not markets:
                    continue

                market = markets[0]
                if not market.get('resolved', False):
                    continue

                # Determine outcome
                outcomes = market.get('outcomes', ['YES', 'NO'])
                prices   = market.get('outcomePrices', [])
                outcome  = None
                for i, o in enumerate(outcomes):
                    if i < len(prices):
                        try:
                            if float(prices[i]) >= 0.99:
                                outcome = str(o).upper()
                                break
                        except:
                            pass

                if not outcome:
                    continue

                direction    = trade.get('direction', '').upper()
                won          = outcome == direction
                stake        = float(trade.get('stake') or C_MIN_BET)
                market_odds  = float(trade.get('market_odds') or 94.0)

                # P&L
                if won:
                    pnl = round(stake * (100.0 - market_odds) / market_odds, 2)
                else:
                    pnl = -stake

                # Update calibration
                true_p = float(trade.get('true_p') or market_odds)
                brier, trades_done, is_calibrated = update_calibration(true_p, won, pnl)

                # Extract lesson
                resolution_context = f'Market resolved {outcome}. {"WON" if won else "LOST"}.'
                lesson = extract_lesson(trade, resolution_context)
                save_lesson(lesson, trade)

                # Close trade in Supabase
                sb.table('pit_paper_trades').update({
                    'status':      'CLOSED',
                    'outcome':     outcome,
                    'pnl':         pnl,
                    'resolved_at': now_utc_iso(),
                    'lesson':      lesson or None
                }).eq('id', trade['id']).execute()

                resolved_count += 1

                # Telegram notification
                cal = get_calibration()
                send_telegram(
                    f'{"✅" if won else "❌"} *PAPER TRADE RESOLVED*\n\n'
                    f'*{trade["market_question"][:80]}*\n'
                    f'{direction} → Resolved {outcome}\n'
                    f'Stake: ${stake} | P&L: ${"+" if pnl >= 0 else ""}{pnl}\n\n'
                    f'💡 _{lesson[:250] if lesson else "No lesson extracted"}_\n\n'
                    f'_{calibration_summary_line(cal)}_'
                )

                print(f'Resolved: {trade["market_question"][:50]} → {outcome} | P&L: ${pnl:+.2f} | Brier: {brier:.4f}')
                time.sleep(2)

            except Exception as e:
                print(f'Auto-resolve error for {condition_id}: {e}')
                continue

        print(f'Auto-resolve complete: {resolved_count} trades closed')

    except Exception as e:
        print(f'AUTO-RESOLVE ERROR: {e}')


def send_daily_summary():
    """8am daily summary of calibration progress."""
    try:
        cal     = get_calibration()
        trades  = cal.get('trades_resolved', 0)
        brier   = cal.get('brier_score', 0.25)
        pnl     = cal.get('total_pnl', 0.0) or 0.0
        wins    = cal.get('wins', 0) or 0
        losses  = cal.get('losses', 0) or 0
        is_cal  = cal.get('is_calibrated', False)
        win_rate = round(wins / trades * 100, 1) if trades else 0
        remaining = max(C_CALIB_TRADES - trades, 0)

        # Open trades count
        open_r = sb.table('pit_paper_trades').select('id').eq('status', 'OPEN').execute()
        open_count = len(open_r.data) if open_r.data else 0

        status_emoji = '✅' if is_cal else '🔄'

        send_telegram(
            f'📊 *PIT SCANNER C — Daily Summary*\n'
            f'_{now_utc().strftime("%d %b %Y %H:%M UTC")}_\n\n'
            f'{status_emoji} *Calibration:* {trades}/{C_CALIB_TRADES} trades resolved\n'
            f'*Brier Score:* {brier:.4f} (target < {C_BRIER_TARGET})\n'
            f'*Win Rate:* {win_rate}% ({wins}W / {losses}L)\n'
            f'*Paper P&L:* ${"+" if pnl >= 0 else ""}{pnl:.2f}\n'
            f'*Open trades:* {open_count}\n\n'
            + (f'🎉 *Calibration complete — real money unlocked!*\n' if is_cal else
               f'⏳ {remaining} more resolved trades needed for real money\n')
        )
    except Exception as e:
        print(f'Daily summary error: {e}')
        # ══════════════════════════════════════════════════════════════════════════
# SCANNER C - MAIN FUNCTION
# ══════════════════════════════════════════════════════════════════════════

def run_high_prob_scan():
    # Scanner C v2 - High Probability Volume Strategy.
    # 1. Fetch 300 markets from Gamma API
    # 2. Gate 1: Category exclusions
    # 3. Gate 2: Probability 87-97% in one direction
    # 4. Gate 3: Volume min 5000
    # 5. Gate 4: Resolves within 7 days
    # 6. Gate 5: Not already open
    # 7. Gate 6: Stability check
    # 8. Gate 7: Resolution rules fetched from Gamma API
    # 9. Gate 8: Claude probability estimation with web search
    # 10. Kelly sizing self-calibrating based on Brier score
    global _is_running_c
    if _is_running_c:
        print('Scanner C already running - skipping')
        return
    _is_running_c = True

    try:
        print(f'\n=== SCANNER C {now_utc().strftime("%Y-%m-%d %H:%M")} UTC ===')

        all_markets = get_active_markets(limit=300)
        if not all_markets:
            print('Scanner C: No markets fetched')
            return

        cal      = get_calibration()
        bankroll = get_bankroll()
        today    = now_utc().date()
        cutoff   = (now_utc() + timedelta(days=C_MAX_DAYS)).date()
        tomorrow = (now_utc() + timedelta(days=C_MIN_DAYS)).date()

        candidates_this_scan = []
        alerts_sent = 0

        g1_pass = g2_pass = g3_pass = g4_pass = 0

        for m in all_markets:
            question     = m['question']
            yes_pct      = m['yes_pct']
            volume       = m['volume']
            condition_id = m['conditionId']
            end_date_str = m.get('endDate', '')

            # ── GATE 1: Excluded categories ───────────────────────────
            if is_excluded(question):
                continue
            g1_pass += 1

            # ── GATE 2: Probability range ──────────────────────────────
            is_high_yes = C_MIN_PROB <= yes_pct <= C_MAX_PROB
            is_high_no  = (100 - C_MAX_PROB) <= yes_pct <= (100 - C_MIN_PROB)

            if not is_high_yes and not is_high_no:
                continue
            g2_pass += 1

            # ── GATE 3: Volume ─────────────────────────────────────────
            if volume < C_MIN_VOLUME:
                continue
            g3_pass += 1

            # ── GATE 4: Time window ────────────────────────────────────
            if not end_date_str:
                continue
            try:
                end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date()
            except:
                continue
            if end_date < tomorrow or end_date > cutoff:
                continue
            g4_pass += 1

            days_left = (end_date - today).days
            if days_left < C_MIN_DAYS:
                continue

            # ── GATE 5: Not already open ───────────────────────────────
            direction_check = 'YES' if is_high_yes else 'NO'
            if paper_trade_already_exists(condition_id, direction_check):
                continue
            if signal_already_exists(condition_id, direction_check):
                continue

            # Log this candidate for next run's stability check
            candidates_this_scan.append((condition_id, yes_pct))

            # ── GATE 6: Stability check ────────────────────────────────
            # Must have been at 94%+ in the PREVIOUS scan
            # First sighting = log and skip, don't bet yet
            if not was_seen_last_scan(condition_id):
                print(f'Scanner C: First sighting — logging for stability — {question[:50]}')
                continue

            # ── GATE 7: Resolution rules ───────────────────────────────
            # Fetch actual rules from Gamma API — three attempts
            rules_text, rules_valid, rules_reason = fetch_and_validate_rules(m)

            if not rules_valid:
                print(f'Scanner C: NO VALID RULES — {rules_reason[:60]} — {question[:50]}')
                continue

            # Parse rules for red flags and warnings
            gate_result, flags = parse_rules(rules_text)

            if gate_result == 'REJECT':
                print(f'Scanner C: REJECTED — red flags {flags} — {question[:50]}')
                continue

            # ── GATE 8: Claude probability estimation ──────────────────
            # Claude receives actual rules text — cannot hallucinate them
            # Does one web search for current reality
            # Returns structured probability estimate
            direction = 'YES' if is_high_yes else 'NO'
            true_p_pct, edge_pp, result, reason = estimate_probability(
                question, direction, rules_text, yes_pct, days_left, flags
            )

            if result == 'SKIP':
                print(f'Scanner C: SKIP (estimation error) — {reason[:60]} — {question[:50]}')
                continue

            if result == 'FAIL':
                print(f'Scanner C: FAIL — {reason[:60]} — {question[:50]}')
                continue

            if edge_pp < C_MIN_EDGE_PP:
                print(f'Scanner C: Edge too small ({edge_pp}pp) — {question[:50]}')
                continue

            # ── MATH: Kelly sizing (pure Python) ───────────────────────
            market_odds = round(yes_pct if direction == 'YES' else 100 - yes_pct, 1)
            q_cost = market_odds / 100.0
            category, fee_pp = classify_category(question)
            net_edge_pp = round(edge_pp - fee_pp, 2)

            if net_edge_pp < C_MIN_EDGE_PP:
                print(f'Scanner C: Net edge too small after fees ({net_edge_pp}pp) — {question[:50]}')
                continue

            kelly_stake = calculate_kelly(true_p_pct / 100.0, q_cost, bankroll)
            if kelly_stake <= 0:
                print(f'Scanner C: No Kelly edge — {question[:50]}')
                continue

            # Annual yield
            if days_left <= 0:
                days_left = 1
            annual_yield = round(((true_p_pct/100 - q_cost) / q_cost) / days_left * 365 * 100, 1)

            # ── BUILD SIGNAL ───────────────────────────────────────────
            signal = {
                'question':           question,
                'conditionId':        condition_id,
                'direction':          direction,
                'verifyReason':       reason,
                'marketOdds':         market_odds,
                'trueP':              true_p_pct,
                'edgePp':             net_edge_pp,
                'annualYield':        annual_yield,
                'daysLeft':           days_left,
                'kellyStake':         kelly_stake,
                'resolutionRisk':     1 if gate_result == 'CLEAN' else 2,
                'resolutionDate':     end_date_str,
                'resolutionCriteria': rules_text[:500] if rules_text else 'See Polymarket',
                'warnings':           flags if gate_result == 'WARN' else [],
                'category':           category,
                'thesis': (
                    f'{direction} @ {market_odds}% | True P: {true_p_pct}% | '
                    f'Edge: {net_edge_pp}pp | Yield: {annual_yield}%/yr | '
                    f'{days_left}d | Vol: ${int(volume):,} | {reason}'
                ),
            }

            # ── SAVE SIGNAL ────────────────────────────────────────────
            save_signal(signal)

            # ── BUILD AND SEND ALERT ───────────────────────────────────
            market_url = (
                f'https://polymarket.com/search?q='
                f'{requests.utils.quote(question[:60])}'
            )
            warnings_str = ''
            if flags:
                warnings_str = (
                    f'\n⚠️ *Fine print detected:* '
                    f'_{", ".join(flags[:3])}_\n'
                    f'_Read full resolution rules before confirming_\n'
                )

            alert = (
                f'🎯 *PIT SCANNER C*\n'
                f'_High Probability Volume Strategy_\n\n'
                f'*{question[:100]}*\n\n'
                f'*Direction:* {direction} @ {market_odds}%\n'
                f'*True P:* {true_p_pct}% | *Edge:* +{net_edge_pp}pp\n'
                f'*Annual Yield:* {annual_yield}%\n'
                f'*Resolves:* {end_date_str} ({days_left}d)\n'
                f'*Category:* {category} | *Vol:* ${int(volume):,}\n'
                f'*Kelly Stake:* ${kelly_stake}\n\n'
                f'🧠 *Claude analysis:* _{reason[:200]}_\n\n'
                f'📋 *Resolution rules:*\n'
                f'_{rules_text[:400] if rules_text else "Not found — check Polymarket"}_\n'
                f'{warnings_str}\n'
                f'🔗 {market_url}\n\n'
                f'_{calibration_summary_line(cal)}_\n\n'
                f'Reply *Y* to paper trade ${kelly_stake} | *N* to skip\n'
                f'⏳ 5 minute window'
            )

            msg_id = send_telegram(alert)

            # Store message_id for reply-to matching
            if msg_id:
                try:
                    sb.table('pit_signals').update({
                        'telegram_message_id': msg_id
                    }).eq('condition_id', condition_id) \
                      .eq('direction', direction).execute()
                except:
                    pass

            print(
                f'Scanner C ALERT: {direction} @ {market_odds}% | '
                f'Edge: {net_edge_pp}pp | Yield: {annual_yield}%/yr | '
                f'Stake: ${kelly_stake} | {question[:50]}'
            )
            alerts_sent += 1
            time.sleep(2)  # rate limit between alerts

        # Log all candidates seen this scan for next run's stability check
        for condition_id, yes_pct in candidates_this_scan:
            log_candidate(condition_id, yes_pct)

        print(f'Gate debug: G1={g1_pass} G2(87-97%)={g2_pass} G3(volume)={g3_pass} G4(time)={g4_pass}')
        print(f'=== Scanner C complete: {alerts_sent} alerts sent ===')

    except Exception as e:
        print(f'SCANNER C ERROR: {e}')
        send_telegram(f'⚠️ Scanner C error: {str(e)[:100]}')
    finally:
        _is_running_c = False
        # ══════════════════════════════════════════════════════════════════════════
# SCHEDULE & ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    print('PIT Scanner C v1 starting...')
    print(f'Strategy: High Probability Volume | Calibration: {C_CALIB_TRADES} trades needed')

    # Show current calibration status on startup
    cal = get_calibration()
    print(f'Calibration status: {calibration_summary_line(cal)}')

    send_telegram(
        '🟢 *PIT Scanner C v2 started*\n\n'
        '📐 _High Probability Volume Strategy_\n\n'
        '✓ 87-97% probability range\n'
        '✓ Resolution rules fetched from Gamma API\n'
        '✓ Stability check — 2 consecutive scans required\n'
        '✓ Claude probability estimation with web search\n'
        '✓ Self-calibrating Kelly sizing\n'
        '✓ Auto-resolve — lessons extracted on every close\n\n'
        f'_{calibration_summary_line(cal)}_\n\n'
        'Scanning every 30 min 24/7'
    )

    # Run immediately on startup
    run_high_prob_scan()

    # Schedule
    schedule.every(30).minutes.do(run_high_prob_scan)
    schedule.every().day.at('08:00').do(auto_resolve)
    schedule.every().day.at('08:05').do(send_daily_summary)

    while True:
        schedule.run_pending()
        time.sleep(60)
