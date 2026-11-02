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

EXCHANGE_COMMISSION = 0.02  # 2% exchange commission

# Complete Global T20 & 100-Ball Tournament Whitelist (Men's & Women's)
GLOBAL_T20_LEAGUES = {
    # International
    "cricket_international_t20": "Men's International T20",
    "cricket_international_t20_women": "Women's International T20",
    
    # India
    "cricket_ipl": "IPL (Indian Premier League)",
    "cricket_wpl": "WPL (Women's Premier League)",
    
    # Australia & New Zealand
    "cricket_big_bash": "BBL (Big Bash League)",
    "cricket_wbbl": "WBBL (Women's Big Bash)",
    "cricket_super_smash": "Super Smash (Men & Women)",
    
    # South Africa, Pakistan, Americas
    "cricket_sa20": "SA20 League",
    "cricket_psl": "PSL (Pakistan Super League)",
    "cricket_caribbean_premier_league": "CPL / WCPL",
    "cricket_major_league_cricket": "MLC (Major League Cricket)",
    
    # UK
    "cricket_t20_blast": "Vitality T20 Blast",
    "cricket_the_hundred": "The Hundred (Men's)",
    "cricket_the_hundred_women": "The Hundred (Women's)"
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}

def send_message(chat_id: int, text: str, reply_markup: dict = None):
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

# ===================================================
# 1. EXPANDED FREE SCORE SCRAPER (ALL POPULAR T20S)
# ===================================================
def fetch_free_t20_scores():
    """
    Scrapes live matches and filters for all Men's and Women's T20s & 100-ball games.
    Explicitly excludes ODIs, Tests, and First-Class matches.
    """
    url = "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/current?lang=en"
    t20_matches = []
    
    # Identifiers for popular global T20 & 100-ball competitions
    t20_keywords = [
        "T20", "TWENTY20", "T20I", "WT20", "IPL", "WPL", "BBL", "WBBL",
        "PSL", "SA20", "CPL", "WCPL", "MLC", "SUPER SMASH", "BLAST", "HUNDRED"
    ]
    exclusions = ["ODI", "ONE-DAY", "TEST", "FIRST-CLASS", "LIST A", "50 OVER"]

    try:
        res = requests.get(url, headers=HEADERS, timeout=8).json()
        raw_matches = res.get("content", {}).get("matches", [])
        for m in raw_matches:
            format_name = m.get("format", "").upper()
            series_name = m.get("series", {}).get("name", "").upper()
            title = m.get("title", "").upper()
            combined_text = f"{format_name} {series_name} {title}"

            # Strict exclusion of non-T20 formats
            if any(ex in combined_text for ex in exclusions):
                continue

            # Must match at least one popular T20 keyword
            is_valid_t20 = any(kw in combined_text for kw in t20_keywords)
            if not is_valid_t20:
                continue

            status = m.get("status", "").lower()
            state = m.get("state", "").lower()
            if state in ["live", "in_progress"] or status in ["live", "in_progress"]:
                teams = m.get("teams", [])
                if len(teams) >= 2:
                    t1 = teams[0]
                    t2 = teams[1]
                    t20_matches.append({
                        "match_id": m.get("objectId"),
                        "title": f"{t1.get('team', {}).get('abbreviation', 'T1')} vs {t2.get('team', {}).get('abbreviation', 'T2')}",
                        "series": m.get("series", {}).get("name", "Global T20"),
                        "status_text": m.get("statusText", ""),
                        "team1_name": t1.get("team", {}).get("name", "Team 1"),
                        "team1_score": t1.get("score", ""),
                        "team2_name": t2.get("team", {}).get("name", "Team 2"),
                        "team2_score": t2.get("score", ""),
                        "is_hundred": "HUNDRED" in combined_text
                    })
    except Exception as e:
        print(f"Free score engine note: {e}")
    return t20_matches

# ===================================================
# 2. INTERNAL PROBABILITY & RUN-RATE ENGINE
# ===================================================
def calculate_internal_t20_odds(runs: int, wickets: int, overs_bowled: float, target: int, is_hundred: bool = False):
    """
    Calculates dynamic win probability and synthetic odds for 120-ball (T20) or 100-ball formats.
    """
    total_balls = 100 if is_hundred else 120
    wickets_in_hand = max(0, 10 - wickets)
    
    completed_overs = int(overs_bowled)
    partial_balls = int(round((overs_bowled - completed_overs) * 10))
    balls_bowled = completed_overs * 6 + partial_balls
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
    synthetic_odds = round(1.0 / win_prob, 2)
    return round(win_prob * 100, 1), synthetic_odds

# ===================================================
# 3. BACKGROUND MULTI-LEAGUE MONITOR
# ===================================================
def run_t20_two_stage_monitoring():
    open_trades = supabase.table("trades").select("*").eq("status", "OPEN").execute().data
    if not open_trades:
        return

    for trade in open_trades:
        chat_id = trade["chat_id"]
        trade_id = trade["id"]
        target_lay = float(trade["target_lay_odds"])

        for sport_key in GLOBAL_T20_LEAGUES.keys():
            url = f"https://api.theoddsapi.com/v4/sports/{sport_key}/odds/?regions=uk,eu&markets=h2h&oddsFormat=decimal&apiKey={ODDS_API_KEY}"
            try:
                res = requests.get(url, timeout=6).json()
                if isinstance(res, list):
                    for match in res:
                        bookies = match.get("bookmakers", [])
                        if not bookies:
                            continue
                        outcomes = bookies[0]["markets"][0].get("outcomes", [])
                        for outcome in outcomes:
                            if outcome["name"].upper() == trade["team_name"].upper():
                                cur_price = float(outcome["price"])
                                if cur_price <= target_lay:
                                    back_stake = int(trade["back_stake"])
                                    lay_stake, net_profit = calculate_green_book(back_stake, float(trade["back_odds"]), cur_price)
                                    alert = (
                                        f"🎯 *TARGET HIT: LOCK GREEN BOOK NOW!*\n\n"
                                        f"• *Match:* {trade['match_title']}\n"
                                        f"• *Team:* {trade['team_name']}\n"
                                        f"• *Current Odds:* {cur_price} (Target: {target_lay})\n"
                                        f"• *Execute Lay (Khai):* ₹{lay_stake}\n"
                                        f"• *Locked Net Profit:* ₹{net_profit}\n\n"
                                        f"Send `/hedge {trade_id} {cur_price}` to settle balance."
                                    )
                                    send_message(chat_id, alert)
            except Exception:
                continue

@app.get("/")
def health_check():
    return {"status": "alive", "scope": "All_Popular_Global_T20s"}

@app.get("/poll")
def cron_poll():
    run_t20_two_stage_monitoring()
    return {"status": "polled", "scope": "all_t20s"}

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
            send_message(chat_id, f"✅ *T20 Trade #{trade_id} activated.* Live monitoring active.")

        elif cb_data.startswith("SKIP_"):
            trade_id = int(cb_data.split("_")[1])
            supabase.table("trades").update({"status": "CANCELLED"}).eq("id", trade_id).execute()
            send_message(chat_id, f"❌ *Trade #{trade_id} skipped.* Bankroll unchanged.")

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
                f"🛡️ *CUT-LOSS EXECUTED (Trade #{trade_id})*\n\n"
                f"• Liquidated at: {cur_odds}\n"
                f"• Loss Incurred: ₹{loss_amount}\n"
                f"• Capital Saved: ₹{salvaged} of ₹{back_stake}\n"
                f"• *Current Bankroll:* ₹{new_bankroll}\n\n"
                f"Slot cleared. Capital protected for next setup."
            )
            send_message(chat_id, msg)

        return {"ok": True}

    if "message" not in data:
        return {"ok": True}

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    # /start
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
            "🏏 *Tradex All-T20 Engine*\n\n"
            "• *Coverage:* Men's & Women's T20Is, IPL, WPL, BBL, WBBL, PSL, MLC, CPL, SA20, Super Smash, The Hundred, Blast\n"
            "• *Virtual Bankroll:* ₹10,000\n"
            "• *Standard Swing (Overs 3-15):* 5% (₹500)\n"
            "• *Death Overs Jackpot (Overs 16-20):* 1% (₹100)\n"
            "• *Max Active Trades:* 2 (Strict anti-spam)\n\n"
            "*Core Commands:*\n"
            "`/upcoming` — View scheduled T20 fixtures across all leagues\n"
            "`/live` — Check in-progress T20 scores & synthetic rates\n"
            "`/calc <Runs> <Wkts> <Overs> <Target>` — T20 odds calculator\n"
            "`/trade <Team> <Odds> <Match>` — Log swing trade\n"
            "`/jackpot <Team> <Odds> <Match>` — Log 1% micro-stake trade\n"
            "`/hedge <TradeID> <Odds>` — Lock Green Book\n"
            "`/cutloss <TradeID> <Odds>` — Capital protection exit\n"
            "`/active` — Check open slots\n"
            "`/balance` — View bankroll audit"
        )
        send_message(chat_id, welcome)
        return {"ok": True}

    # /upcoming (Checks all 13 popular T20 tournaments)
    if text == "/upcoming":
        send_message(chat_id, "📅 *Scanning upcoming fixtures across all popular Men's & Women's T20s...*")
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

    # /live (Strictly T20s & 100-Ball)
    if text == "/live":
        send_message(chat_id, "📡 *Scanning live T20 scores across all popular leagues...*")
        t20_matches = fetch_free_t20_scores()
        if not t20_matches:
            send_message(chat_id, "No live T20 matches in progress right now.\n(ODIs/Tests are excluded). Use `/calc` to test scenarios!")
            return {"ok": True}

        report = "🏏 *Live Global T20 Fixtures:*\n\n"
        for m in t20_matches[:4]:
            report += (
                f"• *{m['title']}* ({m['series']})\n"
                f"  {m['team1_name']}: {m['team1_score']}\n"
                f"  {m['team2_name']}: {m['team2_score']}\n"
                f"  Status: {m['status_text']}\n\n"
            )
        send_message(chat_id, report)
        return {"ok": True}

    # /calc
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

    # /balance
    if text == "/balance":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = int(user["bankroll"])
        send_message(chat_id, f"💰 *Current Virtual Bankroll:* ₹{bankroll}\n• Standard Swing Stake (5%): ₹{int(bankroll * 0.05)}\n• Death Overs Jackpot Stake (1%): ₹{int(bankroll * 0.01)}")
        return {"ok": True}

    # /active
    if text == "/active":
        open_trades = supabase.table("trades").select("*").eq("chat_id", chat_id).eq("status", "OPEN").execute().data
        if not open_trades:
            send_message(chat_id, "No active trades running. (0/2 slots used)")
            return {"ok": True}

        report = f"*Active T20 Trades ({len(open_trades)}/2 slots used):*\n\n"
        for t in open_trades:
            report += (
                f"• *ID #{t['id']} [{t['trade_type']}]:* {t['team_name']} ({t['match_title']})\n"
                f"  Entry: Back @ {t['back_odds']} | Stake: ₹{int(t['back_stake'])}\n"
                f"  🎯 Target Lay: {t['target_lay_odds']} (`/hedge {t['id']} <odds>`)\n"
                f"  🛡️ Cut-Loss Trigger: {t['stop_loss_odds']} (`/cutloss {t['id']} <odds>`)\n\n"
            )
        send_message(chat_id, report)
        return {"ok": True}

    # /trade (Standard Swing Entry)
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
            "entry_reason": f"T20 swing entry on {team} @ {back_odds}. Target lay: {target_lay}."
        }).execute().data[0]

        card = (
            f"🚨 *NEW T20 SWING TRADE (ID #{inserted['id']})*\n\n"
            f"• *Match:* {match_title}\n"
            f"• *Action:* BACK (Lagai) *{team}*\n"
            f"• *Entry Odds:* {back_odds}\n"
            f"• *Allocated Stake:* ₹{stake} (5% Bankroll)\n"
            f"• *Green Book Lay Target:* {target_lay}\n"
            f"• *Cut-Loss Boundary:* {stop_loss}\n\n"
            f"📊 *Reason:* Overs 3-15 overreaction dip. Target 30% contraction for equalized profit."
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

    # /jackpot (Death Overs 1% Entry)
    if text.startswith("/jackpot"):
        parts = text.split(" ", 3)
        if len(parts) < 4:
            send_message(chat_id, "⚠️ *Format:* `/jackpot <Team> <Odds> <MatchTitle>`\n*Example:* `/jackpot KKR 7.50 KKR vs SRH`")
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
            "entry_reason": f"Death overs jackpot on {team} @ {back_odds}. 1% micro-stake."
        }).execute().data[0]

        card = (
            f"🎰 *T20 DEATH OVERS JACKPOT (ID #{inserted['id']})*\n\n"
            f"• *Match:* {match_title}\n"
            f"• *Action:* BACK (Lagai) *{team}*\n"
            f"• *Cheap Entry Odds:* {back_odds}\n"
            f"• *Micro Stake:* ₹{stake} (Only 1% Bankroll)\n"
            f"• *Target Lay (Green Book):* {target_lay}\n\n"
            f"🧠 *Reason:* High R:R late-phase volatility. Capped ₹{stake} risk with outsized payout."
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
