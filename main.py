import os
import math
import requests
from fastapi import FastAPI, Request
from supabase import create_client, Client

app = FastAPI()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

EXCHANGE_COMMISSION = 0.02

# Popular T20 Tournament Matrix
GLOBAL_T20_LEAGUES = {
    "cricket_international_t20": "Men's International T20",
    "cricket_international_t20_women": "Women's International T20",
    "cricket_ipl": "IPL",
    "cricket_wpl": "WPL",
    "cricket_big_bash": "BBL",
    "cricket_wbbl": "WBBL",
    "cricket_super_smash": "Super Smash",
    "cricket_sa20": "SA20",
    "cricket_psl": "PSL",
    "cricket_caribbean_premier_league": "CPL / WCPL",
    "cricket_major_league_cricket": "MLC",
    "cricket_t20_blast": "Vitality Blast",
    "cricket_the_hundred": "The Hundred (Men)",
    "cricket_the_hundred_women": "The Hundred (Women)"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def send_message(chat_id: int, text: str, reply_markup: dict = None):
    """Always delivers as a new push notification message"""
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        res = requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=8)
        return res.json().get("result", {})
    except Exception as e:
        print(f"Error sending message: {e}")
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

# ===================================================
# 1. FREE LIVE SCORE ENGINE (ZERO API COST)
# ===================================================
def fetch_free_t20_scores():
    url = "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/current?lang=en"
    t20_matches = []
    t20_keywords = ["T20", "TWENTY20", "T20I", "WT20", "IPL", "WPL", "BBL", "WBBL", "PSL", "SA20", "CPL", "WCPL", "MLC", "SUPER SMASH", "BLAST", "HUNDRED"]
    exclusions = ["ODI", "ONE-DAY", "TEST", "FIRST-CLASS", "LIST A", "50 OVER"]

    try:
        res = requests.get(url, headers=HEADERS, timeout=8).json()
        raw_matches = res.get("content", {}).get("matches", [])
        for m in raw_matches:
            format_name = m.get("format", "").upper()
            series_name = m.get("series", {}).get("name", "").upper()
            title = m.get("title", "").upper()
            combined = f"{format_name} {series_name} {title}"

            if any(ex in combined for ex in exclusions) or not any(kw in combined for kw in t20_keywords):
                continue

            status = m.get("status", "").lower()
            state = m.get("state", "").lower()
            teams = m.get("teams", [])

            # Check for Upcoming matches starting soon
            if state in ["pre", "scheduled"] or status in ["pre", "scheduled"]:
                if len(teams) >= 2:
                    t20_matches.append({
                        "type": "UPCOMING",
                        "id": str(m.get("objectId")),
                        "title": f"{teams[0].get('team', {}).get('name')} vs {teams[1].get('team', {}).get('name')}",
                        "series": m.get("series", {}).get("name", "T20 Match"),
                        "status_text": m.get("statusText", "Scheduled")
                    })

            # Check for Live In-Progress matches
            elif state in ["live", "in_progress"] or status in ["live", "in_progress"]:
                if len(teams) >= 2:
                    t1, t2 = teams[0], teams[1]
                    t20_matches.append({
                        "type": "LIVE",
                        "id": str(m.get("objectId")),
                        "title": f"{t1.get('team', {}).get('abbreviation', 'T1')} vs {t2.get('team', {}).get('abbreviation', 'T2')}",
                        "series": m.get("series", {}).get("name", "T20 Match"),
                        "team1_name": t1.get("team", {}).get("name", "Team 1"),
                        "team1_score": t1.get("score", ""),
                        "team2_name": t2.get("team", {}).get("name", "Team 2"),
                        "team2_score": t2.get("score", ""),
                        "status_text": m.get("statusText", "")
                    })
    except Exception as e:
        print(f"Score fetch error: {e}")
    return t20_matches

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
# 2. AUTOMATED BACKGROUND ENGINE (CALLED BY CRON)
# ===================================================
def automated_background_engine():
    users = supabase.table("bot_users").select("*").execute().data
    if not users:
        return

    # A. Scan Free Score Feed (Zero Credits)
    feed_matches = fetch_free_t20_scores()

    for u in users:
        chat_id = u["chat_id"]
        bankroll = int(u["bankroll"])
        autopilot = u.get("auto_pilot", False)

        # 1. NOTIFY UPCOMING MATCHES
        for match in [m for m in feed_matches if m["type"] == "UPCOMING"]:
            alert_key = f"UPCOMING_{match['id']}_{chat_id}"
            if not is_alert_sent(alert_key):
                msg = (
                    f"🏏 *UPCOMING T20 FIXTURE ALERT*\n\n"
                    f"• *Match:* {match['title']}\n"
                    f"• *Tournament:* {match['series']}\n"
                    f"• *Status:* {match['status_text']}\n\n"
                    f"Engine will track live ball-by-ball dynamics for entry signals."
                )
                send_message(chat_id, msg)
                mark_alert_sent(alert_key)

        # 2. MONITOR ACTIVE OPEN TRADES (HEDGE & CUT-LOSS ALERTS)
        open_trades = supabase.table("trades").select("*").eq("chat_id", chat_id).eq("status", "OPEN").execute().data

        for trade in open_trades:
            trade_id = trade["id"]
            target_lay = float(trade["target_lay_odds"])
            stop_loss = float(trade["stop_loss_odds"])

            # Verify against Odds API (1 credit)
            for sport_key in GLOBAL_T20_LEAGUES.keys():
                url = f"https://api.theoddsapi.com/v4/sports/{sport_key}/odds/?regions=uk,eu&markets=h2h&oddsFormat=decimal&apiKey={ODDS_API_KEY}"
                try:
                    res = requests.get(url, timeout=5).json()
                    if isinstance(res, list):
                        for m in res:
                            bookies = m.get("bookmakers", [])
                            if not bookies:
                                continue
                            for oc in bookies[0]["markets"][0].get("outcomes", []):
                                if oc["name"].upper() == trade["team_name"].upper():
                                    cur_price = float(oc["price"])

                                    # TARGET REACHED: DISPATCH GREEN BOOK NOTIFICATION
                                    if cur_price <= target_lay:
                                        lay_stake, net_profit = calculate_green_book(int(trade["back_stake"]), float(trade["back_odds"]), cur_price)
                                        alert_key = f"HEDGE_TRIGGER_{trade_id}_{cur_price}"
                                        if not is_alert_sent(alert_key):
                                            alert = (
                                                f"🎯 *TARGET HIT: LOCK GREEN BOOK NOW!*\n\n"
                                                f"• *Match:* {trade['match_title']}\n"
                                                f"• *Team:* {trade['team_name']}\n"
                                                f"• *Entry:* ₹{int(trade['back_stake'])} @ {trade['back_odds']}\n"
                                                f"• *Current Odds:* {cur_price} (Target: {target_lay})\n"
                                                f"─────────────────────────\n"
                                                f"👉 *Action:* Lay (Khai) *₹{lay_stake}* @ {cur_price}\n"
                                                f"💰 *Locked Profit:* ₹{net_profit} (Risk-Free)\n\n"
                                                f"Send `/hedge {trade_id} {cur_price}` to settle balance."
                                            )
                                            send_message(chat_id, alert)
                                            mark_alert_sent(alert_key)

                                    # STOP LOSS TRIGGERED: DISPATCH CUT-LOSS NOTIFICATION
                                    elif cur_price >= stop_loss and stop_loss > 0:
                                        alert_key = f"CUTLOSS_TRIGGER_{trade_id}"
                                        if not is_alert_sent(alert_key):
                                            lay_stake, loss, salvaged = calculate_cut_loss(int(trade["back_stake"]), float(trade["back_odds"]), cur_price)
                                            keyboard = {
                                                "inline_keyboard": [[
                                                    {"text": f"⚠️ Execute Cut-Loss (-₹{loss})", "callback_data": f"EXEC_CUTLOSS_{trade_id}"}
                                                ]]
                                            }
                                            cut_alert = (
                                                f"🛡️ *DEFENSIVE CUT-LOSS ALERT*\n\n"
                                                f"• *Match:* {trade['match_title']}\n"
                                                f"• *Team:* {trade['team_name']}\n"
                                                f"• Odds drifted to: {cur_price} (Limit: {stop_loss})\n"
                                                f"• Salvageable Capital: ₹{salvaged} of ₹{int(trade['back_stake'])}\n\n"
                                                f"Tap below or send `/cutloss {trade_id} {cur_price}` to preserve capital."
                                            )
                                            send_message(chat_id, cut_alert, reply_markup=keyboard)
                                            mark_alert_sent(alert_key)
                except Exception:
                    continue

        # 3. AUTOMATIC VALUE SWING ENTRY DETECTION
        # Check concurrency guard: only search for entries if less than 2 trades active
        if len(open_trades) < 2:
            for sport_key in GLOBAL_T20_LEAGUES.keys():
                url = f"https://api.theoddsapi.com/v4/sports/{sport_key}/odds/?regions=uk,eu&markets=h2h&oddsFormat=decimal&apiKey={ODDS_API_KEY}"
                try:
                    res = requests.get(url, timeout=5).json()
                    if isinstance(res, list) and len(res) > 0:
                        for m in res:
                            home = m.get("home_team")
                            away = m.get("away_team")
                            match_title = f"{home} vs {away}"
                            bookies = m.get("bookmakers", [])
                            if not bookies:
                                continue

                            for oc in bookies[0]["markets"][0].get("outcomes", []):
                                team = oc.get("name")
                                odds = float(oc.get("price", 0))

                                # Standard Swing Detection: Odds in 2.10 - 2.50
                                if 2.10 <= odds <= 2.50:
                                    alert_key = f"ENTRY_{match_title}_{team}_{round(odds, 1)}"
                                    if not is_alert_sent(alert_key):
                                        stake = int(bankroll * 0.05)
                                        target_lay = round(odds * 0.70, 2)
                                        stop_loss = round(odds * 1.50, 2)

                                        inserted = supabase.table("trades").insert({
                                            "chat_id": chat_id,
                                            "match_title": match_title,
                                            "team_name": team,
                                            "back_odds": odds,
                                            "back_stake": stake,
                                            "target_lay_odds": target_lay,
                                            "stop_loss_odds": stop_loss,
                                            "trade_type": "SWING",
                                            "status": "OPEN" if autopilot else "PENDING",
                                            "entry_reason": f"Automatic drift entry on {team} @ {odds}."
                                        }).execute().data[0]

                                        card = (
                                            f"🚨 *AUTOMATIC VALUE ENTRY FOUND*\n\n"
                                            f"• *Match:* {match_title}\n"
                                            f"• *Action:* BACK (Lagai) *{team}*\n"
                                            f"• *Live Odds:* {odds}\n"
                                            f"• *Allocated Stake:* ₹{stake} (5% Bankroll)\n"
                                            f"• *Target Lay (Green Book):* {target_lay}\n"
                                            f"• *Cut-Loss Limit:* {stop_loss}\n\n"
                                            f"📊 *Analysis:* Middle-overs overreaction dip detected. Mean reversion expected."
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
                except Exception:
                    continue

@app.get("/")
def health_check():
    return {"status": "alive", "engine": "Automated_T20_Engine"}

@app.get("/poll")
def cron_poll():
    """Triggered every 5-10 minutes by cron-job.org"""
    automated_background_engine()
    return {"status": "polled", "timestamp": "ok"}

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
                f"• Liquidated at odds: {cur_odds}\n"
                f"• Loss Incurred: ₹{loss_amount}\n"
                f"• Capital Saved: ₹{salvaged} of ₹{back_stake}\n"
                f"• *Updated Bankroll:* ₹{new_bankroll}\n\n"
                f"Trade slot cleared. Capital preserved for next setup."
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
            "🏏 *Tradex Fully Automated T20 Engine*\n\n"
            "• *Status:* 24/7 Background Push Notifications Active\n"
            "• *Virtual Bankroll:* ₹10,000\n"
            "• *Standard Swing (Overs 3-15):* 5% (₹500)\n"
            "• *Death Overs Jackpot (Overs 16-20):* 1% (₹100)\n"
            "• *Max Active Trades:* 2 (Strict Concurrency Guard)\n\n"
            "*Commands:*\n"
            "`/upcoming` — View scheduled T20 matches\n"
            "`/autopilot` — Toggle hands-free trade logging\n"
            "`/active` — View open trades & targets\n"
            "`/trade <Team> <Odds> <Match>` — Manual swing entry\n"
            "`/jackpot <Team> <Odds> <Match>` — Manual micro-stake entry\n"
            "`/hedge <TradeID> <Odds>` — Lock Green Book\n"
            "`/cutloss <TradeID> <Odds>` — Salvage capital\n"
            "`/balance` — View bankroll audit"
        )
        send_message(chat_id, welcome)
        return {"ok": True}

    if text == "/autopilot":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        new_val = not user.get("auto_pilot", False)
        supabase.table("bot_users").update({"auto_pilot": new_val}).eq("chat_id", chat_id).execute()
        status_label = "ENABLED (Auto-take signals)" if new_val else "DISABLED (Manual Yes/No buttons)"
        send_message(chat_id, f"⚡ *Auto-Pilot Mode:* {status_label}")
        return {"ok": True}

    if text == "/upcoming":
        send_message(chat_id, "📅 *Scanning upcoming fixtures across all popular T20s...*")
        upcoming_list = []
        for sport_key, league_name in GLOBAL_T20_LEAGUES.items():
            url = f"https://api.theoddsapi.com/v4/sports/{sport_key}/events?apiKey={ODDS_API_KEY}"
            try:
                res = requests.get(url, timeout=5).json()
                if isinstance(res, list) and len(res) > 0:
                    for ev in res[:2]:
                        home = ev.get("home_team")
                        away = ev.get("away_team")
                        commence = ev.get("commence_time", "").replace("T", " ")[:16]
                        upcoming_list.append(f"• *{home} vs {away}*\n  🏆 {league_name}\n  🕒 {commence} UTC")
            except Exception:
                continue

        if not upcoming_list:
            send_message(chat_id, "No popular T20 fixtures starting in the next 24 hours.")
        else:
            report = "🏏 *Upcoming Popular T20 Matches:*\n\n" + "\n\n".join(upcoming_list[:8])
            send_message(chat_id, report)
        return {"ok": True}

    if text == "/balance":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = int(user["bankroll"])
        send_message(chat_id, f"💰 *Current Virtual Bankroll:* ₹{bankroll}\n• Standard Swing Stake (5%): ₹{int(bankroll * 0.05)}\n• Death Overs Jackpot Stake (1%): ₹{int(bankroll * 0.01)}")
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
            send_message(chat_id, "⚠️ *Format:* `/trade <Team> <Odds> <MatchTitle>`\n*Example:* `/trade MI 2.24 MI vs CSK`")
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
            f"🎉 *Net Locked Profit (after 2% commission):* ₹{net_profit}\n"
            f"💰 *Updated Bankroll:* ₹{new_bankroll}\n\n"
            f"Both sides risk-free. Trade slot cleared!"
        )
        send_message(chat_id, settlement_msg)
        return {"ok": True}

    return {"ok": True}
