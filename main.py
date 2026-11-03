import os
import json
import re
import math
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from supabase import create_client, Client

app = FastAPI()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

EXCHANGE_COMMISSION = 0.02  # 2% exchange commission

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5"
}

def send_message(chat_id: int, text: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=8)
        return res.json().get("result", {})
    except Exception as e:
        print(f"Telegram dispatch error: {e}")
        return {}

def calculate_green_book(back_stake: int, back_odds: float, current_lay_odds: float):
    lay_stake = round((back_stake * back_odds) / current_lay_odds)
    gross_profit = lay_stake - back_stake
    net_profit = round(gross_profit * (1 - EXCHANGE_COMMISSION))
    return int(lay_stake), int(net_profit)

def calculate_cut_loss(back_stake: int, back_odds: float, current_lay_odds: float):
    lay_stake = round((back_stake * back_odds) / current_lay_odds)
    loss = int(back_stake - lay_stake)
    salvaged = int(back_stake - loss)
    return int(lay_stake), loss, salvaged

def is_alert_sent(alert_key: str) -> bool:
    res = supabase.table("sent_alerts").select("id").eq("alert_key", alert_key).execute().data
    return len(res) > 0

def mark_alert_sent(alert_key: str):
    try:
        supabase.table("sent_alerts").insert({"alert_key": alert_key}).execute()
    except Exception:
        pass

def calculate_internal_t20_odds(runs: int, wickets: int, overs_bowled: float, target: int):
    total_balls = 120
    wickets_in_hand = max(0, 10 - wickets)
    completed_ov = int(overs_bowled)
    balls_bowled = completed_ov * 6 + int(round((overs_bowled - completed_ov) * 10))
    balls_remaining = max(1, total_balls - balls_bowled)
    runs_needed = max(0, target - runs)

    if runs_needed <= 0:
        return 99.0, 1.01
    if wickets_in_hand == 0 or balls_remaining <= 0:
        return 1.0, 50.0

    rrr = (runs_needed / balls_remaining) * 6
    par_rrr = 4.0 + (0.75 * wickets_in_hand)
    delta = par_rrr - rrr

    phase_scale = 0.35 + 0.65 * (1.0 - (balls_remaining / total_balls))
    z = delta * phase_scale

    try:
        win_prob = 1.0 / (1.0 + math.exp(-z))
    except OverflowError:
        win_prob = 0.01 if z < 0 else 0.99

    win_prob = max(0.02, min(0.98, win_prob))
    return round(win_prob * 100, 1), round(1.0 / win_prob, 2)

# ===================================================
# 1. LIVE CREX SCRAPER (IN-PLAY DECIMAL ODDS)
# ===================================================
def fetch_crex_live_data():
    url = "https://crex.com/live-score"
    matches = []
    t20_whitelist = ["T20", "WBBL", "BBL", "IPL", "WPL", "PSL", "SA20", "CPL", "BLAST", "HUNDRED"]
    exclusions = ["ODI", "TEST", "FIRST-CLASS", "LIST A"]

    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                data = json.loads(next_script.string)
                page_props = data.get("props", {}).get("pageProps", {})
                live_list = page_props.get("liveMatches", []) or page_props.get("matches", [])
                
                for m in live_list:
                    s_name = str(m.get("seriesName", "")).upper()
                    m_format = str(m.get("format", "")).upper()
                    m_title = str(m.get("matchName", "") or m.get("title", "")).upper()
                    
                    if any(ex in f"{s_name} {m_format}" for ex in exclusions):
                        continue
                    if not any(kw in f"{s_name} {m_format} {m_title}" for kw in t20_whitelist):
                        continue

                    odds_data = m.get("odds", {}) or m.get("matchOdds", {})
                    t1_odds = float(odds_data.get("team1Odds") or odds_data.get("t1Odds") or 0.0)
                    t2_odds = float(odds_data.get("team2Odds") or odds_data.get("t2Odds") or 0.0)
                    
                    matches.append({
                        "id": str(m.get("matchId") or m.get("id")),
                        "series": m.get("seriesName", "T20 Match"),
                        "title": f"{m.get('team1', {}).get('name', 'T1')} vs {m.get('team2', {}).get('name', 'T2')}",
                        "team1": m.get("team1", {}).get("name", "Team 1"),
                        "team2": m.get("team2", {}).get("name", "Team 2"),
                        "team1_odds": t1_odds,
                        "team2_odds": t2_odds,
                        "status": m.get("statusText", "Live")
                    })
    except Exception as e:
        print(f"CREX live fetch error: {e}")

    return matches

# ===================================================
# 2. 3-LAYER SCHEDULE ENGINE (UPCOMING WBBL & T20s)
# ===================================================
def fetch_upcoming_t20_schedule():
    url = "https://crex.com/schedule"
    upcoming = []
    seen = set()

    t20_whitelist = ["T20", "WBBL", "BBL", "IPL", "WPL", "PSL", "SA20", "CPL", "BLAST", "HUNDRED"]
    exclusions = ["ODI", "TEST", "FIRST-CLASS", "LIST A", "YOUTH"]

    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            
            # --- LAYER 1: Deep JSON Traversal across all team key aliases ---
            next_script = soup.find("script", id="__NEXT_DATA__")
            if next_script and next_script.string:
                try:
                    data = json.loads(next_script.string)
                    
                    def traverse_node(node):
                        found = []
                        if isinstance(node, dict):
                            t1, t2 = None, None
                            
                            # Search for Team 1 aliases
                            for k in ["team1Name", "team1", "t1Name", "t1", "teamA", "teamAName", "homeTeam"]:
                                if k in node:
                                    v = node[k]
                                    if isinstance(v, str) and v.strip():
                                        t1 = v.strip()
                                        break
                                    elif isinstance(v, dict):
                                        for sub_k in ["name", "teamName", "n", "title"]:
                                            if sub_k in v and isinstance(v[sub_k], str) and v[sub_k].strip():
                                                t1 = v[sub_k].strip()
                                                break
                                if t1:
                                    break

                            # Search for Team 2 aliases
                            for k in ["team2Name", "team2", "t2Name", "t2", "teamB", "teamBName", "awayTeam"]:
                                if k in node:
                                    v = node[k]
                                    if isinstance(v, str) and v.strip():
                                        t2 = v.strip()
                                        break
                                    elif isinstance(v, dict):
                                        for sub_k in ["name", "teamName", "n", "title"]:
                                            if sub_k in v and isinstance(v[sub_k], str) and v[sub_k].strip():
                                                t2 = v[sub_k].strip()
                                                break
                                if t2:
                                    break

                            # Both teams MUST be present in this exact same node
                            if t1 and t2 and t1.lower() != t2.lower() and len(t1) < 40 and len(t2) < 40:
                                if not any(c in t1 for c in ["{", "}", "[", "]"]):
                                    series = str(node.get("seriesName") or node.get("series") or node.get("tournamentName") or "T20 League").strip()
                                    m_format = str(node.get("format") or node.get("matchType") or "").strip()
                                    status = str(node.get("statusText") or node.get("startTime") or node.get("startDate") or "Scheduled").strip()
                                    
                                    t1 = re.sub(r"^[,\s]+", "", t1)
                                    t2 = re.sub(r"^[,\s]+", "", t2)
                                    series = re.sub(r"^[,\s]+", "", series)
                                    
                                    combined = f"{t1} {t2} {series} {m_format}".upper()
                                    if any(kw in combined for kw in t20_whitelist) and not any(ex in combined for ex in exclusions):
                                        found.append({
                                            "title": f"{t1} vs {t2}",
                                            "series": series,
                                            "status_text": status
                                        })

                            # Continue recursing into children
                            for val in node.values():
                                found.extend(traverse_node(val))

                        elif isinstance(node, list):
                            for item in node:
                                found.extend(traverse_node(item))
                        return found

                    extracted = traverse_node(data.get("props", {}).get("pageProps", {}))
                    for m in extracted:
                        if m["title"] not in seen:
                            seen.add(m["title"])
                            upcoming.append(m)
                except Exception as je:
                    print(f"JSON traversal error: {je}")

            # --- LAYER 2: Isolated Match Card Links (href with -vs-) ---
            if not upcoming:
                for a in soup.find_all("a", href=True):
                    href = a["href"].lower()
                    if ("/scoreboard/" in href or "/live-score/" in href or "/match/" in href) and "-vs-" in href:
                        slug = [p for p in href.split("/") if "-vs-" in p]
                        if slug:
                            parts = slug[0].split("-vs-")
                            raw_t1 = parts[0].replace("-", " ").strip().title()
                            raw_t2 = parts[1].split("-202")[0].split("-match")[0].replace("-", " ").strip().title()
                            
                            card_text = a.get_text(separator=" ").strip()
                            combined = f"{raw_t1} {raw_t2} {card_text}".upper()
                            
                            if any(kw in combined for kw in t20_whitelist) and not any(ex in combined for ex in exclusions):
                                m_title = f"{raw_t1} vs {raw_t2}"
                                if m_title not in seen:
                                    seen.add(m_title)
                                    upcoming.append({
                                        "title": m_title,
                                        "series": "WBBL / T20",
                                        "status_text": "Scheduled"
                                    })
    except Exception as e:
        print(f"CREX fetch error: {e}")

    # --- LAYER 3: ESPN Public Web CDN Fallback ---
    if not upcoming:
        try:
            espn_url = "https://site.web.api.espn.com/apis/site/v2/sports/cricket/scorepanel"
            r = requests.get(espn_url, headers=HEADERS, timeout=6).json()
            for league in r.get("leagues", []):
                for event in league.get("events", []):
                    c_title = event.get("name", "")
                    c_series = league.get("name", "T20")
                    combined = f"{c_title} {c_series}".upper()
                    if any(kw in combined for kw in t20_whitelist) and not any(ex in combined for ex in exclusions):
                        if c_title not in seen:
                            seen.add(c_title)
                            status_desc = event.get("status", {}).get("type", {}).get("detail", "Scheduled")
                            upcoming.append({
                                "title": c_title,
                                "series": c_series,
                                "status_text": status_desc
                            })
        except Exception as ee:
            print(f"ESPN web fallback error: {ee}")

    return upcoming

# ===================================================
# 3. BACKGROUND CRON ENGINE (/poll)
# ===================================================
def automated_background_engine():
    users = supabase.table("bot_users").select("*").execute().data
    if not users:
        return

    crex_matches = fetch_crex_live_data()

    for u in users:
        chat_id = u["chat_id"]
        bankroll = int(u["bankroll"])
        autopilot = u.get("auto_pilot", False)

        open_trades = supabase.table("trades").select("*").eq("chat_id", chat_id).eq("status", "OPEN").execute().data
        for trade in open_trades:
            t_id = trade["id"]
            target_lay = float(trade["target_lay_odds"])
            stop_loss = float(trade["stop_loss_odds"])
            t_name = trade["team_name"].upper()

            for cm in crex_matches:
                cur_price = None
                if t_name in cm["team1"].upper():
                    cur_price = cm["team1_odds"]
                elif t_name in cm["team2"].upper():
                    cur_price = cm["team2_odds"]

                if not cur_price or cur_price <= 1.0:
                    continue

                if cur_price <= target_lay:
                    alert_key = f"HEDGE_TRIGGER_{t_id}_{cur_price}"
                    if not is_alert_sent(alert_key):
                        lay_stake, net_profit = calculate_green_book(int(trade["back_stake"]), float(trade["back_odds"]), cur_price)
                        msg = (
                            f"🎯 *TARGET HIT (CREX LIVE): LOCK GREEN BOOK!*\n\n"
                            f"• *Match:* {trade['match_title']}\n"
                            f"• *Team:* {trade['team_name']}\n"
                            f"• *Current CREX Rate:* {cur_price} (Target: {target_lay})\n"
                            f"─────────────────────────\n"
                            f"👉 *Action:* Lay (Khai) *₹{lay_stake}* @ {cur_price}\n"
                            f"💰 *Net Profit:* ₹{net_profit} (Equalized)\n\n"
                            f"Send `/hedge {t_id} {cur_price}` to settle balance."
                        )
                        send_message(chat_id, msg)
                        mark_alert_sent(alert_key)

                elif cur_price >= stop_loss and stop_loss > 0:
                    alert_key = f"CUTLOSS_TRIGGER_{t_id}"
                    if not is_alert_sent(alert_key):
                        lay_stake, loss, salvaged = calculate_cut_loss(int(trade["back_stake"]), float(trade["back_odds"]), cur_price)
                        keyboard = {
                            "inline_keyboard": [[
                                {"text": f"⚠️ Confirm Cut-Loss (-₹{loss})", "callback_data": f"EXEC_CUTLOSS_{t_id}"}
                            ]]
                        }
                        msg = (
                            f"🛡️ *CREX RATE DRIFT: CUT-LOSS ALERT*\n\n"
                            f"• *Match:* {trade['match_title']}\n"
                            f"• *Team:* {trade['team_name']}\n"
                            f"• Odds drifted to: {cur_price} (Limit: {stop_loss})\n"
                            f"• Salvageable: ₹{salvaged} of ₹{int(trade['back_stake'])}\n\n"
                            f"Tap below or send `/cutloss {t_id} {cur_price}` to preserve capital."
                        )
                        send_message(chat_id, msg, reply_markup=keyboard)
                        mark_alert_sent(alert_key)

        if len(open_trades) < 2:
            for cm in crex_matches:
                for team_key, odds in [(cm["team1"], cm["team1_odds"]), (cm["team2"], cm["team2_odds"])]:
                    if 2.10 <= odds <= 2.50:
                        alert_key = f"CREX_ENTRY_{cm['id']}_{team_key}_{round(odds, 1)}"
                        if not is_alert_sent(alert_key):
                            stake = int(bankroll * 0.05)
                            target_lay = round(odds * 0.70, 2)
                            stop_loss = round(odds * 1.50, 2)

                            inserted = supabase.table("trades").insert({
                                "chat_id": chat_id,
                                "match_title": cm["title"],
                                "team_name": team_key,
                                "back_odds": odds,
                                "back_stake": stake,
                                "target_lay_odds": target_lay,
                                "stop_loss_odds": stop_loss,
                                "trade_type": "SWING",
                                "status": "OPEN" if autopilot else "PENDING",
                                "entry_reason": f"CREX live swing drift on {team_key} @ {odds}."
                            }).execute().data[0]

                            card = (
                                f"🚨 *AUTOMATIC VALUE ENTRY (CREX RATE)*\n\n"
                                f"• *Match:* {cm['title']}\n"
                                f"• *Action:* BACK (Lagai) *{team_key}*\n"
                                f"• *Live CREX Odds:* {odds}\n"
                                f"• *Allocated Stake:* ₹{stake} (5% Bankroll)\n"
                                f"• *Green Book Lay Target:* {target_lay}\n"
                                f"• *Cut-Loss Limit:* {stop_loss}\n\n"
                                f"📊 *Reason:* Overreaction dip detected. Mean reversion expected."
                            )

                            if autopilot:
                                send_message(chat_id, card + "\n\n⚡ *Auto-Pilot:* Position logged as OPEN.")
                            else:
                                keyboard = {
                                    "inline_keyboard": [[
                                        {"text": "✅ Took Trade", "callback_data": f"ACCEPT_{inserted['id']}"},
                                        {"text": "❌ Skip", "callback_data": f"SKIP_{inserted['id']}"}
                                    ]]
                                }
                                send_message(chat_id, card, reply_markup=keyboard)

                            mark_alert_sent(alert_key)
                            break

@app.get("/")
def health_check():
    return {"status": "alive", "engine": "CREX_Live_Engine"}

@app.get("/poll")
def cron_poll():
    automated_background_engine()
    return {"status": "polled", "timestamp": "ok"}

# ===================================================
# 4. TELEGRAM WEBHOOK COMMANDS
# ===================================================
@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    if "callback_query" in data:
        cq = data["callback_query"]
        chat_id = cq["from"]["id"]
        cb_data = cq["data"]

        if cb_data.startswith("ACCEPT_"):
            trade_id = int(cb_data.split("_")[1])
            supabase.table("trades").update({"status": "OPEN"}).eq("id", trade_id).execute()
            send_message(chat_id, f"✅ *Trade #{trade_id} activated.* Live Green Book monitoring is on.")

        elif cb_data.startswith("SKIP_"):
            trade_id = int(cb_data.split("_")[1])
            supabase.table("trades").update({"status": "CANCELLED"}).eq("id", trade_id).execute()
            send_message(chat_id, f"❌ *Trade #{trade_id} dismissed.*")

        elif cb_data.startswith("EXEC_CUTLOSS_"):
            trade_id = int(cb_data.split("_")[2])
            trade = supabase.table("trades").select("*").eq("id", trade_id).single().execute().data
            user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
            
            back_stake = int(trade["back_stake"])
            cur_odds = float(trade["stop_loss_odds"])
            lay_stake, loss_amount, salvaged = calculate_cut_loss(back_stake, float(trade["back_odds"]), cur_odds)
            
            new_bankroll = int(user["bankroll"]) - loss_amount
            supabase.table("bot_users").update({"bankroll": new_bankroll}).eq("chat_id", chat_id).execute()
            supabase.table("trades").update({"status": "CUT_LOSS"}).eq("id", trade_id).execute()
            
            supabase.table("trade_history").insert({
                "trade_id": trade_id,
                "chat_id": chat_id,
                "match_title": trade["match_title"],
                "team_name": trade["team_name"],
                "entry_odds": trade["back_odds"],
                "exit_odds": cur_odds,
                "net_pnl": -loss_amount,
                "final_bankroll": new_bankroll
            }).execute()

            msg = (
                f"🛡️ *CUT-LOSS SETTLED (Trade #{trade_id})*\n\n"
                f"• Liquidated at: {cur_odds}\n"
                f"• Loss Incurred: ₹{loss_amount}\n"
                f"• Capital Saved: ₹{salvaged} of ₹{back_stake}\n"
                f"• *Updated Bankroll:* ₹{new_bankroll}"
            )
            send_message(chat_id, msg)

        return {"ok": True}

    if "message" not in data:
        return {"ok": True}

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    if text == "/start":
        user_res = supabase.table("bot_users").select("*").eq("chat_id", chat_id).execute()
        if not user_res.data:
            supabase.table("bot_users").insert({
                "chat_id": chat_id,
                "bankroll": 10000,
                "risk_percent": 5,
                "auto_pilot": False
            }).execute()

        welcome = (
            "🏏 *Tradex CREX Automated Trading Engine*\n\n"
            "• *Source:* Live CREX Web Feed & Schedule\n"
            "• *Virtual Bankroll:* ₹10,000\n"
            "• *Standard Swing (Overs 3-15):* 5% (₹500)\n"
            "• *Death Overs Jackpot (Overs 16-20):* 1% (₹100)\n"
            "• *Max Active Trades:* 2\n\n"
            "*Commands:*\n"
            "`/upcoming` — Multi-day T20 & WBBL schedule (CREX)\n"
            "`/live` — Live CREX match odds\n"
            "`/calc <Runs> <Wkts> <Overs> <Target>` — 120-ball odds engine\n"
            "`/autopilot` — Toggle auto-trade logging\n"
            "`/trade <Team> <Odds> <Match>` — Manual swing entry\n"
            "`/jackpot <Team> <Odds> <Match>` — Manual micro-stake entry\n"
            "`/hedge <TradeID> <Odds>` — Lock Green Book\n"
            "`/cutloss <TradeID> <Odds>` — Salvage capital\n"
            "`/active` — Open positions\n"
            "`/balance` — Bankroll status"
        )
        send_message(chat_id, welcome)
        return {"ok": True}

    if text == "/upcoming":
        send_message(chat_id, "📅 *Fetching schedule from CREX...*")
        upcoming = fetch_upcoming_t20_schedule()
        if not upcoming:
            send_message(chat_id, "No scheduled T20 fixtures found.")
        else:
            report = "🏏 *Upcoming Popular T20 Matches (CREX):*\n\n"
            for m in upcoming[:8]:
                report += f"• *{m['title']}*\n  🏆 {m['series']}\n  🕒 {m['status_text']}\n\n"
            send_message(chat_id, report)
        return {"ok": True}

    if text == "/live":
        send_message(chat_id, "📡 *Scanning live CREX match odds...*")
        crex_data = fetch_crex_live_data()
        if not crex_data:
            send_message(chat_id, "No live T20 match in progress on CREX right now.\n\nUse `/upcoming` to view the full fixture list.")
            return {"ok": True}

        report = "🏏 *Live CREX Matches & Odds:*\n\n"
        for m in crex_data:
            report += (
                f"• *{m['title']}* ({m['series']})\n"
                f"  {m['team1']}: {m['team1_odds'] if m['team1_odds'] > 0 else 'N/A'}\n"
                f"  {m['team2']}: {m['team2_odds'] if m['team2_odds'] > 0 else 'N/A'}\n"
                f"  Status: {m['status']}\n\n"
            )
        send_message(chat_id, report)
        return {"ok": True}

    if text.startswith("/calc"):
        parts = text.split()
        if len(parts) != 5:
            send_message(chat_id, "⚠️ *Format:* `/calc <Runs> <Wickets> <Overs> <Target>`\n*Example:* `/calc 110 3 13.2 165`")
            return {"ok": True}

        try:
            r = int(parts[1])
            w = int(parts[2])
            ov = float(parts[3])
            target = int(parts[4])
        except ValueError:
            send_message(chat_id, "Please enter valid numbers.")
            return {"ok": True}

        prob, odds = calculate_internal_t20_odds(r, w, ov, target)
        completed_ov = int(ov)
        balls_done = completed_ov * 6 + int(round((ov - completed_ov) * 10))
        balls_left = max(1, 120 - balls_done)
        rrr = round(((target - r) / balls_left) * 6, 2)

        card = (
            f"🧠 *T20 QUANTITATIVE MODEL*\n\n"
            f"• *State:* {r}/{w} in {ov} overs ({balls_left} balls left)\n"
            f"• *Target:* {target} (Needs {target - r} runs)\n"
            f"• *Required Run Rate:* {rrr} RPO\n"
            f"─────────────────────────\n"
            f"📊 *Chasing Win Probability:* {prob}%\n"
            f"📈 *Synthetic Decimal Odds:* {odds}\n\n"
        )
        if ov >= 16.0 and odds >= 4.50:
            card += "🎰 *Signal:* Death Overs Jackpot Candidate (1% Micro-Stake)!"
        elif 2.10 <= odds <= 2.50 and ov < 16.0:
            card += "⚡ *Signal:* Standard Swing Zone (5% Stake)!"
        else:
            card += "⚪ *Signal:* Normal variance. Standing by."

        send_message(chat_id, card)
        return {"ok": True}

    if text == "/autopilot":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        new_val = not user.get("auto_pilot", False)
        supabase.table("bot_users").update({"auto_pilot": new_val}).eq("chat_id", chat_id).execute()
        status_label = "ENABLED (Auto-take signals)" if new_val else "DISABLED (Manual Yes/No buttons)"
        send_message(chat_id, f"⚡ *Auto-Pilot Mode:* {status_label}")
        return {"ok": True}

    if text == "/balance":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = int(user["bankroll"])
        send_message(chat_id, f"💰 *Current Virtual Bankroll:* ₹{bankroll}\n• Swing Stake (5%): ₹{int(bankroll * 0.05)}\n• Jackpot Stake (1%): ₹{int(bankroll * 0.01)}")
        return {"ok": True}

    if text == "/active":
        open_trades = supabase.table("trades").select("*").eq("chat_id", chat_id).eq("status", "OPEN").execute().data
        if not open_trades:
            send_message(chat_id, "No active trades running. (0/2 slots used)")
            return {"ok": True}

        report = f"*Active Positions ({len(open_trades)}/2 slots used):*\n\n"
        for t in open_trades:
            report += (
                f"• *ID #{t['id']} [{t['trade_type']}]:* {t['team_name']} ({t['match_title']})\n"
                f"  Entry: Back @ {t['back_odds']} | Stake: ₹{int(t['back_stake'])}\n"
                f"  🎯 Target Lay: {t['target_lay_odds']} (`/hedge {t['id']} <odds>`)\n"
                f"  🛡️ Cut-Loss Trigger: {t['stop_loss_odds']} (`/cutloss {t['id']} <odds>`)\n\n"
            )
        send_message(chat_id, report)
        return {"ok": True}

    if text.startswith("/trade"):
        parts = text.split(" ", 3)
        if len(parts) < 4:
            send_message(chat_id, "⚠️ *Format:* `/trade <Team> <Odds> <MatchTitle>`\n*Example:* `/trade HBH 2.24 Hurricanes vs Renegades`")
            return {"ok": True}

        team = parts[1].upper()
        try:
            back_odds = float(parts[2])
        except ValueError:
            send_message(chat_id, "Enter valid decimal odds.")
            return {"ok": True}
        match_title = parts[3]

        active_count = len(supabase.table("trades").select("id").eq("chat_id", chat_id).eq("status", "OPEN").execute().data)
        if active_count >= 2:
            send_message(chat_id, "🛑 *Limit Reached:* Max 2 concurrent trades allowed.")
            return {"ok": True}

        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = int(user["bankroll"])
        stake = int(bankroll * 0.05)
        target_lay = round(back_odds * 0.70, 2)
        stop_loss = round(back_odds * 1.50, 2)

        inserted = supabase.table("trades").insert({
            "chat_id": chat_id,
            "match_title": match_title,
            "team_name": team,
            "back_odds": back_odds,
            "back_stake": stake,
            "target_lay_odds": target_lay,
            "stop_loss_odds": stop_loss,
            "trade_type": "SWING",
            "status": "OPEN" if user["auto_pilot"] else "PENDING",
            "entry_reason": f"Manual T20 swing entry on {team} @ {back_odds}."
        }).execute().data[0]

        card = (
            f"🚨 *NEW T20 SWING TRADE (ID #{inserted['id']})*\n\n"
            f"• *Match:* {match_title}\n"
            f"• *Action:* BACK (Lagai) *{team}*\n"
            f"• *Entry Odds:* {back_odds}\n"
            f"• *Allocated Stake:* ₹{stake} (5% Bankroll)\n"
            f"• *Green Book Lay Target:* {target_lay}\n"
            f"• *Cut-Loss Limit:* {stop_loss}\n\n"
            f"📊 *Reason:* Target 30% contraction for equalized profit."
        )

        if user["auto_pilot"]:
            send_message(chat_id, card + "\n\n⚡ *Auto-Pilot:* Trade logged as OPEN.")
        else:
            keyboard = {
                "inline_keyboard": [[
                    {"text": "✅ Took Trade", "callback_data": f"ACCEPT_{inserted['id']}"},
                    {"text": "❌ Skip", "callback_data": f"SKIP_{inserted['id']}"}
                ]]
            }
            send_message(chat_id, card, reply_markup=keyboard)
        return {"ok": True}

    if text.startswith("/jackpot"):
        parts = text.split(" ", 3)
        if len(parts) < 4:
            send_message(chat_id, "⚠️ *Format:* `/jackpot <Team> <Odds> <MatchTitle>`\n*Example:* `/jackpot MR 7.50 Hurricanes vs Renegades`")
            return {"ok": True}

        team = parts[1].upper()
        try:
            back_odds = float(parts[2])
        except ValueError:
            send_message(chat_id, "Enter valid decimal odds.")
            return {"ok": True}
        match_title = parts[3]

        active_count = len(supabase.table("trades").select("id").eq("chat_id", chat_id).eq("status", "OPEN").execute().data)
        if active_count >= 2:
            send_message(chat_id, "🛑 *Limit Reached:* Max 2 concurrent trades allowed.")
            return {"ok": True}

        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = int(user["bankroll"])
        stake = int(bankroll * 0.01)
        if stake < 50:
            stake = 50

        target_lay = round(back_odds * 0.40, 2)
        stop_loss = round(back_odds * 1.60, 2)

        inserted = supabase.table("trades").insert({
            "chat_id": chat_id,
            "match_title": match_title,
            "team_name": team,
            "back_odds": back_odds,
            "back_stake": stake,
            "target_lay_odds": target_lay,
            "stop_loss_odds": stop_loss,
            "trade_type": "JACKPOT",
            "status": "OPEN" if user["auto_pilot"] else "PENDING",
            "entry_reason": f"Death overs jackpot on {team} @ {back_odds}."
        }).execute().data[0]

        card = (
            f"🎰 *T20 DEATH OVERS JACKPOT (ID #{inserted['id']})*\n\n"
            f"• *Match:* {match_title}\n"
            f"• *Action:* BACK (Lagai) *{team}*\n"
            f"• *Cheap Entry Odds:* {back_odds}\n"
            f"• *Micro Stake:* ₹{stake} (1% Bankroll)\n"
            f"• *Target Lay (Green Book):* {target_lay}\n\n"
            f"🧠 *Reason:* High R:R late-phase setup. Capped ₹{stake} risk."
        )

        if user["auto_pilot"]:
            send_message(chat_id, card + "\n\n⚡ *Auto-Pilot:* Trade logged as OPEN.")
        else:
            keyboard = {
                "inline_keyboard": [[
                    {"text": "✅ Took Trade", "callback_data": f"ACCEPT_{inserted['id']}"},
                    {"text": "❌ Skip", "callback_data": f"SKIP_{inserted['id']}"}
                ]]
            }
            send_message(chat_id, card, reply_markup=keyboard)
        return {"ok": True}

    if text.startswith("/hedge"):
        parts = text.split()
        if len(parts) != 3:
            send_message(chat_id, "⚠️ *Format:* `/hedge <TradeID> <CurrentOdds>`\n*Example:* `/hedge 1 1.55`")
            return {"ok": True}

        trade_id = int(parts[1])
        cur_lay_odds = float(parts[2])

        trade_res = supabase.table("trades").select("*").eq("id", trade_id).eq("chat_id", chat_id).execute()
        if not trade_res.data:
            send_message(chat_id, f"Trade #{trade_id} not found.")
            return {"ok": True}

        t = trade_res.data[0]
        if t["status"] != "OPEN":
            send_message(chat_id, f"Trade #{trade_id} is not OPEN.")
            return {"ok": True}

        back_stake = int(t["back_stake"])
        back_odds = float(t["back_odds"])
        lay_stake, net_profit = calculate_green_book(back_stake, back_odds, cur_lay_odds)

        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        new_bankroll = int(user["bankroll"]) + net_profit

        supabase.table("bot_users").update({"bankroll": new_bankroll}).eq("chat_id", chat_id).execute()
        supabase.table("trades").update({"status": "HEDGED", "lay_stake": lay_stake, "locked_profit": net_profit}).eq("id", trade_id).execute()

        supabase.table("trade_history").insert({
            "trade_id": trade_id,
            "chat_id": chat_id,
            "match_title": t["match_title"],
            "team_name": t["team_name"],
            "entry_odds": back_odds,
            "exit_odds": cur_lay_odds,
            "net_pnl": net_profit,
            "final_bankroll": new_bankroll
        }).execute()

        settlement_msg = (
            f"📗 *GREEN BOOK LOCKED!*\n\n"
            f"• *Match:* {t['match_title']}\n"
            f"• *Original Back:* ₹{back_stake} @ {back_odds}\n"
            f"• *Execute Lay (Khai):* ₹{lay_stake} @ {cur_lay_odds}\n"
            f"─────────────────────────\n"
            f"🎉 *Net Locked Profit:* ₹{net_profit}\n"
            f"💰 *Updated Bankroll:* ₹{new_bankroll}\n\n"
            f"Both sides risk-free. Trade slot cleared!"
        )
        send_message(chat_id, settlement_msg)
        return {"ok": True}

    if text.startswith("/cutloss"):
        parts = text.split()
        if len(parts) != 3:
            send_message(chat_id, "⚠️ *Format:* `/cutloss <TradeID> <CurrentOdds>`\n*Example:* `/cutloss 1 3.80`")
            return {"ok": True}

        trade_id = int(parts[1])
        cur_odds = float(parts[2])

        trade_res = supabase.table("trades").select("*").eq("id", trade_id).eq("chat_id", chat_id).execute()
        if not trade_res.data:
            send_message(chat_id, f"Trade #{trade_id} not found.")
            return {"ok": True}

        t = trade_res.data[0]
        if t["status"] != "OPEN":
            send_message(chat_id, f"Trade #{trade_id} is not OPEN.")
            return {"ok": True}

        back_stake = int(t["back_stake"])
        lay_stake, loss, salvaged = calculate_cut_loss(back_stake, float(t["back_odds"]), cur_odds)

        keyboard = {
            "inline_keyboard": [[
                {"text": f"⚠️ Confirm Cut-Loss (-₹{loss})", "callback_data": f"EXEC_CUTLOSS_{trade_id}"}
            ]]
        }
        prompt = (
            f"🛡️ *CUT-LOSS CALCULATION (Trade #{trade_id})*\n\n"
            f"• Original Stake: ₹{back_stake} @ {t['back_odds']}\n"
            f"• Current Market Rate: {cur_odds}\n"
            f"• Estimated Loss: ₹{loss}\n"
            f"• Capital You Save: ₹{salvaged}\n\n"
            f"Do you want to liquidate now to avoid a full ₹{back_stake} loss?"
        )
        send_message(chat_id, prompt, reply_markup=keyboard)
        return {"ok": True}

    return {"ok": True}
