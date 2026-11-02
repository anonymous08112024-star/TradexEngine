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

# Tier-1 Tournament Whitelist
TIER1_TOURNAMENTS = {
    "cricket_ipl": "IPL (Indian Premier League)",
    "cricket_international_t20": "International T20",
    "cricket_odi": "One Day Internationals (ODI)",
    "cricket_test_match": "Test Matches",
    "cricket_big_bash": "Big Bash League (BBL)",
    "cricket_psl": "Pakistan Super League (PSL)",
    "cricket_caribbean_premier_league": "CPL",
    "cricket_t20_blast": "Vitality T20 Blast"
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

# ==========================================
# 1. FREE LIVE SCORE ENGINE (ZERO API COST)
# ==========================================
def fetch_free_live_scores():
    """
    Fetches real-time scores directly from free public JSON endpoints without using API credits.
    """
    url = "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/current?lang=en"
    matches = []
    try:
        res = requests.get(url, headers=HEADERS, timeout=8).json()
        raw_matches = res.get("content", {}).get("matches", [])
        for m in raw_matches:
            status = m.get("status", "").lower()
            state = m.get("state", "").lower()
            # Look for live in-progress matches
            if state in ["live", "in_progress"] or status in ["live", "in_progress"]:
                teams = m.get("teams", [])
                if len(teams) >= 2:
                    t1 = teams[0]
                    t2 = teams[1]
                    matches.append({
                        "match_id": m.get("objectId"),
                        "title": f"{t1.get('team', {}).get('abbreviation', 'T1')} vs {t2.get('team', {}).get('abbreviation', 'T2')}",
                        "series": m.get("series", {}).get("name", "Cricket"),
                        "status_text": m.get("statusText", ""),
                        "team1_name": t1.get("team", {}).get("name", "Team 1"),
                        "team1_score": t1.get("score", ""),
                        "team2_name": t2.get("team", {}).get("name", "Team 2"),
                        "team2_score": t2.get("score", ""),
                        "is_t1_batting": t1.get("isLive", False),
                        "is_t2_batting": t2.get("isLive", False)
                    })
    except Exception as e:
        print(f"Free score fetcher note: {e}")
    return matches

# ==========================================
# 2. INTERNAL ODDS & WIN PROBABILITY MODEL
# ==========================================
def calculate_internal_odds(runs: int, wickets: int, overs_bowled: float, target: int, total_overs: int = 20):
    """
    Calculates dynamic win probability and synthetic decimal odds for a 2nd innings chase.
    """
    wickets_in_hand = max(0, 10 - wickets)
    balls_bowled = int(overs_bowled) * 6 + int(round((overs_bowled - int(overs_bowled)) * 10))
    total_balls = total_overs * 6
    balls_remaining = max(1, total_balls - balls_bowled)
    runs_needed = max(0, target - runs)

    if runs_needed <= 0:
        return 0.99, 1.01  # Already won

    if wickets_in_hand == 0 or balls_remaining <= 0:
        return 0.01, 50.0  # Already lost

    # Run Rates
    crr = (runs / balls_bowled) * 6 if balls_bowled > 0 else 6.0
    rrr = (runs_needed / balls_remaining) * 6

    # Par RRR model calibrated to T20 match averages
    par_rrr = 4.0 + (0.75 * wickets_in_hand)
    delta = par_rrr - rrr

    # Match phase volatility scaler (higher leverage late in innings)
    phase_scale = 0.35 + 0.65 * (1.0 - (balls_remaining / total_balls))
    z = delta * phase_scale

    # Logistic Win Probability Curve
    try:
        win_prob = 1.0 / (1.0 + math.exp(-z))
    except OverflowError:
        win_prob = 0.01 if z < 0 else 0.99

    win_prob = max(0.02, min(0.98, win_prob))
    synthetic_back_odds = round(1.0 / win_prob, 2)
    return round(win_prob * 100, 1), synthetic_back_odds

# ==========================================
# 3. BACKGROUND TWO-STAGE AUTO-POLLER
# ==========================================
def run_two_stage_monitoring():
    """
    Stage 1: Checks free score engine.
    Stage 2: Pings Odds API only when internal model detects a valid entry or hedge exit.
    """
    open_trades = supabase.table("trades").select("*").eq("status", "OPEN").execute().data

    # Check open trades for Green Book exit
    for trade in open_trades:
        chat_id = trade["chat_id"]
        trade_id = trade["id"]
        target_lay = float(trade["target_lay_odds"])

        # Check real market rates (1 API credit) only when trade is live
        url = f"https://api.theoddsapi.com/v4/sports/cricket_ipl/odds/?regions=uk,eu&markets=h2h&oddsFormat=decimal&apiKey={ODDS_API_KEY}"
        try:
            res = requests.get(url, timeout=8).json()
            if isinstance(res, list):
                for match in res:
                    bookies = match.get("bookmakers", [])
                    if not bookies:
                        continue
                    outcomes = bookies[0]["markets"][0].get("outcomes", [])
                    for outcome in outcomes:
                        if outcome["name"].upper() == trade["team_name"].upper():
                            cur_price = float(outcome["price"])
                            # EXIT TRIGGER: Target Lay Odds reached!
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
        except Exception as e:
            print(f"Poller check error: {e}")

@app.get("/")
def health_check():
    return {"status": "alive", "service": "TradexEngine"}

@app.get("/poll")
def cron_poll():
    run_two_stage_monitoring()
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
                f"• Position liquidated at odds: {cur_odds}\n"
                f"• Loss Incurred: ₹{loss_amount}\n"
                f"• Capital Salvaged: ₹{salvaged} of ₹{back_stake}\n"
                f"• *Current Bankroll:* ₹{new_bankroll}\n\n"
                f"Trade slot cleared. Capital preserved."
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
            "🏏 *Tradex Quantitative Cricket Engine*\n\n"
            "• *Virtual Bankroll:* ₹10,000\n"
            "• *Standard Swing Risk:* 5% (₹500)\n"
            "• *Jackpot Risk:* 1% (₹100)\n"
            "• *Free Score Engine:* Active (0 API credits used)\n\n"
            "*Core Commands:*\n"
            "`/live` — Check free score feed & synthetic odds\n"
            "`/calc <Runs> <Wkts> <Overs> <Target>` — Test odds calculator\n"
            "`/trade <Team> <Odds> <Match>` — Log standard swing trade\n"
            "`/jackpot <Team> <Odds> <Match>` — Log micro-stake jackpot (1%)\n"
            "`/hedge <TradeID> <Odds>` — Lock Green Book profit\n"
            "`/cutloss <TradeID> <Odds>` — Defensive capital preservation\n"
            "`/active` — Check open trade slots\n"
            "`/balance` — View bankroll & ledger summary"
        )
        send_message(chat_id, welcome)
        return {"ok": True}

    # /live (Free Score Engine Status)
    if text == "/live":
        send_message(chat_id, "📡 *Polling free live match engine...*")
        live_matches = fetch_free_live_scores()
        if not live_matches:
            send_message(chat_id, "No live fixtures currently in progress on the free feed.\nUse `/calc` to test the internal odds math on any scenario!")
            return {"ok": True}

        report = "🏏 *Live Fixtures (Free Score Feed):*\n\n"
        for m in live_matches[:3]:
            report += (
                f"• *{m['title']}* ({m['series']})\n"
                f"  {m['team1_name']}: {m['team1_score']}\n"
                f"  {m['team2_name']}: {m['team2_score']}\n"
                f"  Status: {m['status_text']}\n\n"
            )
        send_message(chat_id, report)
        return {"ok": True}

    # /calc (Manual Situation Odds Calculator)
    # Usage: /calc <Runs> <Wickets> <Overs> <Target>
    # Example: /calc 95 3 12.0 180
    if text.startswith("/calc"):
        parts = text.split()
        if len(parts) != 5:
            send_message(chat_id, "⚠️ *Format:* `/calc <Runs> <Wickets> <Overs> <Target>`\n*Example:* `/calc 95 3 12.0 180`")
            return {"ok": True}

        try:
            r = int(parts[1])
            w = int(parts[2])
            ov = float(parts[3])
            target = int(parts[4])
        except ValueError:
            send_message(chat_id, "Please enter valid numbers.")
            return {"ok": True}

        prob, odds = calculate_internal_odds(r, w, ov, target)
        rrr = round(((target - r) / max(1, (120 - int(ov * 6)))) * 6, 2)

        card = (
            f"🧠 *INTERNAL QUANTITATIVE ODDS ENGINE*\n\n"
            f"• *Current State:* {r}/{w} in {ov} overs\n"
            f"• *Target:* {target} (Needs {target - r} runs)\n"
            f"• *Required Run Rate:* {rrr} RPO\n"
            f"─────────────────────────\n"
            f"📊 *Chasing Win Probability:* {prob}%\n"
            f"📈 *Synthetic Decimal Odds:* {odds}\n\n"
        )
        if 2.10 <= odds <= 2.50:
            card += "⚡ *Signal:* Optimal Swing Back Zone! (High value mean-reversion)"
        elif odds >= 5.00:
            card += "🎰 *Signal:* High R:R Jackpot Territory! (Micro-stake 1% candidate)"
        else:
            card += "⚪ *Signal:* Normal market range. Standing by."

        send_message(chat_id, card)
        return {"ok": True}

    # /balance
    if text == "/balance":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = int(user["bankroll"])
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
                f"  🛡️ Stop-Loss Trigger: {t['stop_loss_odds']} (`/cutloss {t['id']} <odds>`)\n\n"
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
            "entry_reason": f"Standard swing entry on {team} @ {back_odds}. Target lay: {target_lay}."
        }).execute().data[0]

        card = (
            f"🚨 *NEW SWING TRADE (ID #{inserted['id']})*\n\n"
            f"• *Match:* {match_title}\n"
            f"• *Action:* BACK (Lagai) *{team}*\n"
            f"• *Entry Odds:* {back_odds}\n"
            f"• *Allocated Stake:* ₹{stake} (5% Bankroll)\n"
            f"• *Green Book Lay Target:* {target_lay}\n"
            f"• *Cut-Loss Boundary:* {stop_loss}\n\n"
            f"📊 *Reason:* 2nd innings / middle overs dip. Target 30% contraction for equalized profit."
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

    # /jackpot (Micro-Stake Entry)
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
            "entry_reason": f"Death overs jackpot on {team} @ {back_odds}. Micro-stake asymmetric reward."
        }).execute().data[0]

        card = (
            f"🎰 *JACKPOT MICRO-TRADE (ID #{inserted['id']})*\n\n"
            f"• *Match:* {match_title}\n"
            f"• *Action:* BACK (Lagai) *{team}*\n"
            f"• *Cheap Entry Odds:* {back_odds}\n"
            f"• *Micro Stake:* ₹{stake} (1% Bankroll)\n"
            f"• *Target Lay (Green Book):* {target_lay}\n\n"
            f"🧠 *Reason:* High R:R asymmetric setup. Minimal risk (₹{stake}) with large upside."
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
            f"Both outcomes risk-free. Trade slot cleared!"
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
            f"• Capital Salvaged: ₹{salvaged}\n\n"
            f"Do you want to liquidate now to avoid a full ₹{back_stake} loss?"
        )
        send_message(chat_id, prompt, reply_markup=keyboard)
        return {"ok": True}

    return {"ok": True}
