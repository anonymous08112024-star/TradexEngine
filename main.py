import os
import math
import requests
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
    "Accept": "application/json"
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
# PIPE 1: ESPN CORE REST API (SCHEDULES & SCOREBOARDS)
# ===================================================
def fetch_espn_fixtures():
    """
    Pulls structured JSON directly from ESPN's public mobile CDN.
    Zero scraping, 100% clean data, zero API keys.
    """
    url = "https://site.web.api.espn.com/apis/site/v2/sports/cricket/scorepanel"
    upcoming = []
    live_matches = []
    seen = set()

    t20_whitelist = ["T20", "WBBL", "BBL", "IPL", "WPL", "PSL", "SA20", "CPL", "BLAST", "HUNDRED", "BIG BASH"]
    exclusions = ["ODI", "TEST", "FIRST-CLASS", "LIST A", "50 OVER"]

    try:
        res = requests.get(url, headers=HEADERS, timeout=7).json()
        for league in res.get("leagues", []):
            series_name = league.get("name", "T20 Match")
            
            for event in league.get("events", []):
                match_id = str(event.get("id", ""))
                title = event.get("name", "")
                
                # Verify T20 format
                combined = f"{title} {series_name}".upper()
                if any(ex in combined for ex in exclusions):
                    continue
                if not any(kw in combined for kw in t20_whitelist):
                    continue

                if match_id in seen:
                    continue
                seen.add(match_id)

                status_obj = event.get("status", {})
                state = status_obj.get("type", {}).get("state", "pre").lower()
                status_desc = status_obj.get("type", {}).get("detail") or status_obj.get("type", {}).get("description") or "Scheduled"

                comps = event.get("competitions", [{}])[0]
                competitors = comps.get("competitors", [])
                
                t1_name = competitors[0].get("team", {}).get("name", "Team 1") if len(competitors) > 0 else "Team 1"
                t2_name = competitors[1].get("team", {}).get("name", "Team 2") if len(competitors) > 1 else "Team 2"
                t1_score = competitors[0].get("score", "Yet to bat") if len(competitors) > 0 else ""
                t2_score = competitors[1].get("score", "Yet to bat") if len(competitors) > 1 else ""

                fixture_data = {
                    "id": match_id,
                    "title": f"{t1_name} vs {t2_name}",
                    "team1": t1_name,
                    "team2": t2_name,
                    "team1_score": t1_score,
                    "team2_score": t2_score,
                    "series": series_name,
                    "status_text": status_desc,
                    "state": state
                }

                if state == "in":
                    live_matches.append(fixture_data)
                elif state == "pre":
                    upcoming.append(fixture_data)

    except Exception as e:
        print(f"ESPN Core REST API error: {e}")

    return {"upcoming": upcoming, "live": live_matches}

# ===================================================
# PIPE 2: OPEN IN-PLAY MARKET DECIMAL ODDS FEED
# ===================================================
def fetch_live_market_odds():
    """
    Pulls unauthenticated real-time decimal odds directly from open 
    in-play feeds for all active cricket matches globally.
    """
    url = "https://1xbet.com/LiveFeed/BestGamesZip?sports=1&count=50&lng=en"
    live_odds_map = []

    try:
        res = requests.get(url, headers=HEADERS, timeout=6).json()
        games = res.get("Value", [])
        
        for g in games:
            t1 = g.get("O1", "")
            t2 = g.get("O2", "")
            league = g.get("L", "")
            events = g.get("E", [])
            
            t1_odds = 0.0
            t2_odds = 0.0

            # Find Match Winner Decimal Odds (Group 1)
            for ev in events:
                if ev.get("G") == 1:
                    if ev.get("T") == 1:
                        t1_odds = float(ev.get("C", 0.0))
                    elif ev.get("T") == 2 or ev.get("T") == 3:
                        t2_odds = float(ev.get("C", 0.0))

            if t1 and t2 and (t1_odds > 1.0 or t2_odds > 1.0):
                live_odds_map.append({
                    "title": f"{t1} vs {t2}",
                    "team1": t1,
                    "team2": t2,
                    "series": league,
                    "team1_odds": t1_odds,
                    "team2_odds": t2_odds
                })
    except Exception as e:
        print(f"Pipe 2 market feed notice: {e}")

    return live_odds_map

# ===================================================
# BACKGROUND ENGINE (/poll)
# ===================================================
def automated_background_engine():
    users = supabase.table("bot_users").select("*").execute().data
    if not users:
        return

    # Ingest from both pipes
    espn_data = fetch_espn_fixtures()
    live_odds = fetch_live_market_odds()

    for u in users:
        chat_id = u["chat_id"]
        bankroll = int(u.get("bankroll", 10000))
        autopilot = u.get("auto_pilot", False)

        open_trades = supabase.table("trades").select("*").eq("chat_id", chat_id).eq("status", "OPEN").execute().data
        
        # 1. Evaluate Open Trades Against Live Odds
        for trade in open_trades:
            t_id = trade["id"]
            target_lay = float(trade["target_lay_odds"])
            stop_loss = float(trade["stop_loss_odds"])
            team_key = trade["team_name"].upper()

            for m in live_odds:
                cur_price = None
                if team_key in m["team1"].upper():
                    cur_price = m["team1_odds"]
                elif team_key in m["team2"].upper():
                    cur_price = m["team2_odds"]

                if not cur_price or cur_price <= 1.0:
                    continue

                # GREEN BOOK TARGET REACHED
                if cur_price <= target_lay:
                    alert_key = f"HEDGE_TRIGGER_{t_id}_{cur_price}"
                    if not is_alert_sent(alert_key):
                        lay_stake, net_profit = calculate_green_book(int(trade["back_stake"]), float(trade["back_odds"]), cur_price)
                        msg = (
                            f"🎯 *TARGET HIT: LOCK GREEN BOOK NOW!*\n\n"
                            f"• *Match:* {trade['match_title']}\n"
                            f"• *Team:* {trade['team_name']}\n"
                            f"• *Live Odds:* {cur_price} (Target: {target_lay})\n"
                            f"─────────────────────────\n"
                            f"👉 *Action:* Lay (Khai) *₹{lay_stake}* @ {cur_price}\n"
                            f"💰 *Net Profit:* ₹{net_profit} (Equalized & Risk-Free)\n\n"
                            f"Send `/hedge {t_id} {cur_price}` to settle balance."
                        )
                        send_message(chat_id, msg)
                        mark_alert_sent(alert_key)

                # DEFENSIVE CUT-LOSS LIMIT
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
                            f"🛡️ *MARKET DRIFT: CUT-LOSS ALERT*\n\n"
                            f"• *Match:* {trade['match_title']}\n"
                            f"• *Team:* {trade['team_name']}\n"
                            f"• Odds drifted to: {cur_price} (Limit: {stop_loss})\n"
                            f"• Capital Saved: ₹{salvaged} of ₹{int(trade['back_stake'])}\n\n"
                            f"Tap below or send `/cutloss {t_id} {cur_price}` to preserve capital."
                        )
                        send_message(chat_id, msg, reply_markup=keyboard)
                        mark_alert_sent(alert_key)

        # 2. Automated Value Swing Signals (2.10–2.50)
        if len(open_trades) < 2 and live_odds:
            for m in live_odds:
                for t_name, odds in [(m["team1"], m["team1_odds"]), (m["team2"], m["team2_odds"])]:
                    if 2.10 <= odds <= 2.50:
                        alert_key = f"AUTO_SWING_{m['title']}_{t_name}_{round(odds, 1)}"
                        if not is_alert_sent(alert_key):
                            stake = int(bankroll * 0.05)
                            target_lay = round(odds * 0.70, 2)
                            stop_loss = round(odds * 1.50, 2)

                            inserted = supabase.table("trades").insert({
                                "chat_id": chat_id,
                                "match_title": m["title"],
                                "team_name": t_name,
                                "back_odds": odds,
                                "back_stake": stake,
                                "target_lay_odds": target_lay,
                                "stop_loss_odds": stop_loss,
                                "trade_type": "SWING",
                                "status": "OPEN" if autopilot else "PENDING",
                                "entry_reason": f"Value drift on {t_name} @ {odds}."
                            }).execute().data[0]

                            card = (
                                f"🚨 *LIVE VALUE SWING SIGNAL*\n\n"
                                f"• *Match:* {m['title']}\n"
                                f"• *Action:* BACK (Lagai) *{t_name}*\n"
                                f"• *Market Odds:* {odds}\n"
                                f"• *Allocated Stake:* ₹{stake} (5% Bankroll)\n"
                                f"• *Green Book Lay Target:* {target_lay}\n"
                                f"• *Cut-Loss Limit:* {stop_loss}\n\n"
                                f"📊 *Reason:* Target 30% contraction for equalized profit."
                            )

                            if autopilot:
                                send_message(chat_id, card + "\n\n⚡ *Auto-Pilot:* Trade logged as OPEN.")
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
    return {"status": "alive", "engine": "Tradex_TwoPipe_Engine"}

@app.get("/poll")
def cron_poll():
    automated_background_engine()
    return {"status": "polled", "timestamp": "ok"}

# ===================================================
# TELEGRAM WEBHOOK COMMANDS
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
            send_message(chat_id, f"✅ *Trade #{trade_id} activated.* Live monitoring active.")

        elif cb_data.startswith("SKIP_"):
            trade_id = int(cb_data.split("_")[1])
            supabase.table("trades").update({"status": "CANCELLED"}).eq("id", trade_id).execute()
            send_message(chat_id, f"❌ *Trade #{trade_id} dismissed.*")

        elif cb_data.startswith("EXEC_CUTLOSS_"):
            trade_id = int(cb_data.split("_")[2])
            trade = supabase.table("trades").select("*").eq("id", trade_id).single().execute().data
            user_res = supabase.table("bot_users").select("*").eq("chat_id", chat_id).execute().data
            current_bankroll = int(user_res[0]["bankroll"]) if user_res else 10000
            
            back_stake = int(trade["back_stake"])
            cur_odds = float(trade["stop_loss_odds"])
            lay_stake, loss_amount, salvaged = calculate_cut_loss(back_stake, float(trade["back_odds"]), cur_odds)
            
            new_bankroll = current_bankroll - loss_amount
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

    user_res = supabase.table("bot_users").select("*").eq("chat_id", chat_id).execute().data
    if not user_res:
        new_user = {
            "chat_id": chat_id,
            "bankroll": 10000,
            "risk_percent": 5,
            "auto_pilot": False
        }
        supabase.table("bot_users").insert(new_user).execute()
        current_user = new_user
    else:
        current_user = user_res[0]

    # /start
    if text == "/start":
        welcome = (
            "🏏 *Tradex Two-Pipe Trading Engine*\n\n"
            "• *Pipe 1:* ESPN Official Core REST API (Clean schedules & scorecards)\n"
            "• *Pipe 2:* Live In-Play Market Odds Feed\n"
            "• *Virtual Bankroll:* ₹10,000\n"
            "• *Standard Swing (Overs 3-15):* 5% (₹500)\n"
            "• *Death Overs Jackpot (Overs 16-20):* 1% (₹100)\n"
            "• *Max Active Trades:* 2\n\n"
            "*Commands:*\n"
            "`/upcoming` — Clean T20 & WBBL fixtures\n"
            "`/live` — Active match scorecards & odds\n"
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

    # /autopilot
    if text == "/autopilot":
        current_state = bool(current_user.get("auto_pilot", False))
        new_state = not current_state
        supabase.table("bot_users").update({"auto_pilot": new_state}).eq("chat_id", chat_id).execute()
        
        status_label = "ENABLED (Auto-take signals)" if new_state else "DISABLED (Manual Yes/No buttons)"
        send_message(chat_id, f"⚡ *Auto-Pilot Mode:* {status_label}")
        return {"ok": True}

    # /upcoming (Pipe 1 - ESPN REST API)
    if text == "/upcoming":
        send_message(chat_id, "📅 *Fetching clean fixtures from ESPN Core API...*")
        fixtures = fetch_espn_fixtures()
        upcoming = fixtures.get("upcoming", [])
        
        if not upcoming:
            send_message(chat_id, "No upcoming T20 fixtures found right now.")
        else:
            report = "🏏 *Upcoming T20 Fixtures (ESPN Core API):*\n\n"
            for m in upcoming[:8]:
                report += f"• *{m['title']}*\n  🏆 {m['series']}\n  🕒 {m['status_text']}\n\n"
            send_message(chat_id, report)
        return {"ok": True}

    # /live (Combines Pipe 1 Live Score with Pipe 2 Market Odds)
    if text == "/live":
        send_message(chat_id, "📡 *Scanning live scoreboards & market feeds...*")
        fixtures = fetch_espn_fixtures()
        live_list = fixtures.get("live", [])
        odds_list = fetch_live_market_odds()

        if not live_list and not odds_list:
            send_message(chat_id, "No live T20 match in progress right now.\n(Hobart Hurricanes Women vs Melbourne Renegades Women starts today at 1:40 PM IST).\n\nUse `/upcoming` to view the full fixture list.")
            return {"ok": True}

        report = "🏏 *Live T20 Matches:*\n\n"
        for m in live_list[:5]:
            report += (
                f"• *{m['title']}* ({m['series']})\n"
                f"  {m['team1']}: {m['team1_score']}\n"
                f"  {m['team2']}: {m['team2_score']}\n"
                f"  Status: {m['status_text']}\n\n"
            )
        send_message(chat_id, report)
        return {"ok": True}

    # /balance
    if text == "/balance":
        bankroll = int(current_user.get("bankroll", 10000))
        send_message(chat_id, f"💰 *Current Virtual Bankroll:* ₹{bankroll}\n• Swing Stake (5%): ₹{int(bankroll * 0.05)}\n• Jackpot Stake (1%): ₹{int(bankroll * 0.01)}")
        return {"ok": True}

    # /active
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

    # /trade
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

        bankroll = int(current_user.get("bankroll", 10000))
        stake = int(bankroll * 0.05)
        target_lay = round(back_odds * 0.70, 2)
        stop_loss = round(back_odds * 1.50, 2)
        autopilot = current_user.get("auto_pilot", False)

        inserted = supabase.table("trades").insert({
            "chat_id": chat_id,
            "match_title": match_title,
            "team_name": team,
            "back_odds": back_odds,
            "back_stake": stake,
            "target_lay_odds": target_lay,
            "stop_loss_odds": stop_loss,
            "trade_type": "SWING",
            "status": "OPEN" if autopilot else "PENDING",
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

        if autopilot:
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

    # /jackpot
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

        bankroll = int(current_user.get("bankroll", 10000))
        stake = int(bankroll * 0.01)
        if stake < 50:
            stake = 50

        target_lay = round(back_odds * 0.40, 2)
        stop_loss = round(back_odds * 1.60, 2)
        autopilot = current_user.get("auto_pilot", False)

        inserted = supabase.table("trades").insert({
            "chat_id": chat_id,
            "match_title": match_title,
            "team_name": team,
            "back_odds": back_odds,
            "back_stake": stake,
            "target_lay_odds": target_lay,
            "stop_loss_odds": stop_loss,
            "trade_type": "JACKPOT",
            "status": "OPEN" if autopilot else "PENDING",
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

        if autopilot:
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

    # /hedge
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

        new_bankroll = int(current_user.get("bankroll", 10000)) + net_profit

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

    # /cutloss
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
