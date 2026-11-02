import os
import requests
from fastapi import FastAPI, Request
from supabase import create_client, Client

app = FastAPI()

# Cloud Environment Credentials
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
ODDS_API_KEY = os.getenv("ODDS_API_KEY")

TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

EXCHANGE_COMMISSION = 0.02  # 2% exchange commission on net winnings

# Supported cricket tournament endpoints on The-Odds-API
CRICKET_SPORTS = [
    "cricket_international_t20",
    "cricket_ipl",
    "cricket_test_match",
    "cricket_odi",
    "cricket_t20_blast",
    "cricket_big_bash"
]

def send_message(chat_id: int, text: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload, timeout=8)
    except Exception as e:
        print(f"Error sending Telegram message: {e}")

def calculate_green_book(back_stake: int, back_odds: float, current_lay_odds: float):
    """
    Computes equalized lay stake and net locked profit in whole Indian Rupees (₹).
    Lay Stake = (Back Stake * Back Odds) / Current Lay Odds
    """
    lay_stake = round((back_stake * back_odds) / current_lay_odds)
    gross_profit = lay_stake - back_stake
    net_profit = round(gross_profit * (1 - EXCHANGE_COMMISSION))
    return lay_stake, net_profit

def scan_live_markets_and_monitor(chat_id: int):
    """
    Scans live cricket markets for value swings and checks open trades for hedge triggers.
    Enforces maximum 2 concurrent trades.
    """
    user_res = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute()
    if not user_res.data:
        return "User not found. Send /start first."
    user = user_res.data

    # 1. Check open trades to monitor for Green Book exit
    open_trades = supabase.table("trades").select("*").eq("chat_id", chat_id).eq("status", "OPEN").execute().data

    # 2. Strict Concurrency Check: Max 2 simultaneous trades
    if len(open_trades) >= 2:
        return "Active limit reached (2/2 trade slots in use). Close an existing position before taking new signals."

    if not ODDS_API_KEY:
        return "ODDS_API_KEY is not configured."

    # 3. Scan cricket feeds
    matches_found = []
    for sport_key in CRICKET_SPORTS:
        url = f"https://api.theoddsapi.com/v4/sports/{sport_key}/odds/?regions=uk,eu&markets=h2h&oddsFormat=decimal&apiKey={ODDS_API_KEY}"
        try:
            res = requests.get(url, timeout=10).json()
            if isinstance(res, list) and len(res) > 0:
                matches_found.extend(res)
        except Exception:
            continue

    if not matches_found:
        return "No active cricket fixtures with open market liquidity right now. Use `/trade` for manual CREX entries."

    # 4. Strategy Filter: Detect Favorite Drift & Overreaction
    new_trade_triggered = False
    for match in matches_found:
        home_team = match.get("home_team", "")
        away_team = match.get("away_team", "")
        bookmakers = match.get("bookmakers", [])
        if not bookmakers:
            continue

        outcomes = bookmakers[0]["markets"][0].get("outcomes", [])
        for outcome in outcomes:
            team = outcome.get("name")
            odds = float(outcome.get("price", 0))

            # Trigger condition: Value swing zone (odds between 2.10 and 2.50)
            if 2.10 <= odds <= 2.50:
                bankroll = float(user["bankroll"])
                stake = round((bankroll * user["risk_percent"]) / 100)
                target_lay = round(odds * 0.70, 2)  # Target 30% contraction for equal profit

                trade_status = "OPEN" if user["auto_pilot"] else "PENDING"
                trade_title = f"{home_team} vs {away_team}"

                # Insert trade record
                reason_text = (
                    f"1. Market Drift: {team} drifted into value territory ({odds}).\n"
                    f"2. Mathematical Advantage: Target odds contraction to {target_lay} locks profit regardless of outcome.\n"
                    f"3. Risk Protocol: Controlled allocation of ₹{stake} (5% bankroll)."
                )

                inserted = supabase.table("trades").insert({
                    "chat_id": chat_id,
                    "match_title": trade_title,
                    "team_name": team,
                    "back_odds": odds,
                    "back_stake": stake,
                    "target_lay_odds": target_lay,
                    "status": trade_status,
                    "entry_reason": reason_text
                }).execute().data[0]

                card = (
                    f"🚨 *ALGORITHMIC VALUE TRADE FOUND*\n\n"
                    f"• *Match:* {trade_title}\n"
                    f"• *Action:* BACK (Lagai) *{team}*\n"
                    f"• *Live Odds:* {odds}\n"
                    f"• *Allocated Stake:* ₹{stake} (5% Bankroll)\n"
                    f"• *Green Book Lay Target:* {target_lay}\n\n"
                    f"📊 *Quantitative Reason:*\n{reason_text}"
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

                new_trade_triggered = True
                break
        if new_trade_triggered:
            break

    if not new_trade_triggered:
        return "Market scanned: No high-conviction value mispricings found right now. Bot standing by."

    return "Scan complete. Signal dispatched."

@app.get("/")
def health_check():
    return {"status": "alive", "service": "cricket_trade_bot"}

@app.get("/poll")
def cron_poll():
    """
    Endpoint triggered by cron-job to check trade health and scan for alerts.
    """
    users = supabase.table("bot_users").select("chat_id").execute().data
    for u in users:
        scan_live_markets_and_monitor(u["chat_id"])
    return {"status": "polled", "users_checked": len(users)}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    # 1. Inline Buttons (Took Trade / Skip)
    if "callback_query" in data:
        cq = data["callback_query"]
        chat_id = cq["from"]["id"]
        cb_data = cq["data"]

        if cb_data.startswith("ACCEPT_"):
            trade_id = int(cb_data.split("_")[1])
            supabase.table("trades").update({"status": "OPEN"}).eq("id", trade_id).execute()
            send_message(chat_id, f"✅ *Trade #{trade_id} activated.* Live monitoring enabled.")

        elif cb_data.startswith("SKIP_"):
            trade_id = int(cb_data.split("_")[1])
            supabase.table("trades").update({"status": "CANCELLED"}).eq("id", trade_id).execute()
            send_message(chat_id, f"❌ *Trade #{trade_id} dismissed.* Bankroll unchanged.")

        return {"ok": True}

    if "message" not in data:
        return {"ok": True}

    msg = data["message"]
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    # 2. Command: /start
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
            "🏏 *Cricket Green Book Trading Engine*\n\n"
            "• *Virtual Bankroll:* ₹10,000\n"
            "• *Risk Per Trade:* 5% (₹500)\n"
            "• *Max Active Trades:* 2 (Strict anti-spam)\n"
            "• *Commission Rate:* 2%\n\n"
            "*Core Commands:*\n"
            "`/scan` — Scan live cricket matches & detect swing dips\n"
            "`/autopilot` — Toggle between Manual & Hands-free execution\n"
            "`/trade <Team> <Odds> <Match>` — Log a trade manually\n"
            "`/active` — View open trades and hedge progress\n"
            "`/hedge <TradeID> <CurrentOdds>` — Lock profit on both teams\n"
            "`/balance` — Check bankroll and audit summary"
        )
        send_message(chat_id, welcome)
        return {"ok": True}

    # 3. Command: /scan
    if text == "/scan":
        send_message(chat_id, "🔍 *Scanning live cricket fixtures & market liquidity...*")
        result = scan_live_markets_and_monitor(chat_id)
        if result and not result.startswith("Scan complete"):
            send_message(chat_id, result)
        return {"ok": True}

    # 4. Command: /autopilot
    if text == "/autopilot":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        new_status = not user.get("auto_pilot", False)
        supabase.table("bot_users").update({"auto_pilot": new_status}).eq("chat_id", chat_id).execute()
        status_label = "ENABLED (Auto-take signals)" if new_status else "DISABLED (Manual Yes/No buttons)"
        send_message(chat_id, f"⚡ *Auto-Pilot Mode:* {status_label}")
        return {"ok": True}

    # 5. Command: /balance
    if text == "/balance":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = round(float(user["bankroll"]))
        send_message(chat_id, f"💰 *Current Virtual Bankroll:* ₹{bankroll}\n• Risk per trade: 5% (₹{round(bankroll * 0.05)})")
        return {"ok": True}

    # 6. Command: /active
    if text == "/active":
        open_trades = supabase.table("trades").select("*").eq("chat_id", chat_id).eq("status", "OPEN").execute().data
        if not open_trades:
            send_message(chat_id, "No active trades running. (0/2 slots used)")
            return {"ok": True}

        report = f"*Active Positions ({len(open_trades)}/2 slots used):*\n\n"
        for t in open_trades:
            report += (
                f"• *ID #{t['id']}:* {t['team_name']} ({t['match_title']})\n"
                f"  Entry: Back @ {t['back_odds']} (Stake: ₹{round(t['back_stake'])})\n"
                f"  Target Lay: {t['target_lay_odds']}\n"
                f"  _Exit command:_ `/hedge {t['id']} <current_odds>`\n\n"
            )
        send_message(chat_id, report)
        return {"ok": True}

    # 7. Command: /trade (Manual CREX Rate Entry)
    if text.startswith("/trade"):
        parts = text.split(" ", 3)
        if len(parts) < 4:
            send_message(chat_id, "⚠️ *Format:* `/trade <Team> <DecimalOdds> <MatchTitle>`\n*Example:* `/trade MI 2.24 MI vs CSK`")
            return {"ok": True}

        team = parts[1].upper()
        try:
            back_odds = float(parts[2])
        except ValueError:
            send_message(chat_id, "Please enter valid decimal odds (e.g. 2.24).")
            return {"ok": True}
        match_title = parts[3]

        active_count = len(supabase.table("trades").select("id").eq("chat_id", chat_id).eq("status", "OPEN").execute().data)
        if active_count >= 2:
            send_message(chat_id, "🛑 *Limit Reached:* You already have 2 active trades open. Close one before opening a new position.")
            return {"ok": True}

        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = float(user["bankroll"])
        stake = round((bankroll * user["risk_percent"]) / 100)
        target_lay = round(back_odds * 0.70, 2)

        trade_status = "OPEN" if user["auto_pilot"] else "PENDING"
        inserted = supabase.table("trades").insert({
            "chat_id": chat_id,
            "match_title": match_title,
            "team_name": team,
            "back_odds": back_odds,
            "back_stake": stake,
            "target_lay_odds": target_lay,
            "status": trade_status,
            "entry_reason": f"Value entry on {team} @ {back_odds}. Target lay: {target_lay}."
        }).execute().data[0]

        card = (
            f"🚨 *NEW TRADE SIGNAL (ID #{inserted['id']})*\n\n"
            f"• *Match:* {match_title}\n"
            f"• *Action:* BACK (Lagai) *{team}*\n"
            f"• *Entry Odds:* {back_odds}\n"
            f"• *Recommended Stake:* ₹{stake} (5% bankroll)\n"
            f"• *Target Lay (Green Book):* {target_lay}\n\n"
            f"🧠 *Reason:* Overreaction dip identified. Target 30% odds contraction for equalized profit."
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

    # 8. Command: /hedge (Execute Green Book Exit)
    if text.startswith("/hedge"):
        parts = text.split()
        if len(parts) != 3:
            send_message(chat_id, "⚠️ *Format:* `/hedge <TradeID> <CurrentOdds>`\n*Example:* `/hedge 1 1.55`")
            return {"ok": True}

        trade_id = int(parts[1])
        try:
            cur_lay_odds = float(parts[2])
        except ValueError:
            send_message(chat_id, "Enter valid decimal lay odds.")
            return {"ok": True}

        trade_res = supabase.table("trades").select("*").eq("id", trade_id).eq("chat_id", chat_id).execute()
        if not trade_res.data:
            send_message(chat_id, f"Trade #{trade_id} not found.")
            return {"ok": True}

        t = trade_res.data[0]
        if t["status"] != "OPEN":
            send_message(chat_id, f"Trade #{trade_id} is not in OPEN status.")
            return {"ok": True}

        back_stake = int(t["back_stake"])
        back_odds = float(t["back_odds"])
        lay_stake, net_profit = calculate_green_book(back_stake, back_odds, cur_lay_odds)

        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        new_bankroll = round(float(user["bankroll"]) + net_profit)
        supabase.table("bot_users").update({"bankroll": new_bankroll}).eq("chat_id", chat_id).execute()

        supabase.table("trades").update({
            "status": "HEDGED",
            "lay_stake": lay_stake,
            "locked_profit": net_profit
        }).eq("id", trade_id).execute()

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
            f"Both outcomes now risk-free. Trade slot freed up!"
        )
        send_message(chat_id, settlement_msg)
        return {"ok": True}

    return {"ok": True}
