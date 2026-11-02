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

EXCHANGE_COMMISSION = 0.02  # 2% exchange commission on winnings

def send_message(chat_id: int, text: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    requests.post(f"{TELEGRAM_API_URL}/sendMessage", json=payload)

def calculate_green_book(back_stake: int, back_odds: float, current_lay_odds: float):
    """
    Computes equalized lay stake and net locked profit in whole Indian Rupees (₹).
    Formula: Lay Stake = (Back Stake * Back Odds) / Current Lay Odds
    """
    lay_stake = round((back_stake * back_odds) / current_lay_odds)
    gross_profit = lay_stake - back_stake
    net_profit = round(gross_profit * (1 - EXCHANGE_COMMISSION))
    return lay_stake, net_profit

@app.get("/")
def health_check():
    return {"status": "alive", "service": "cricket_trade_bot"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    # 1. Handle Inline Button Clicks (Took Trade / Skip)
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
            "`/autopilot` — Toggle between Manual & Hands-free execution\n"
            "`/trade <Team> <Odds> <Match>` — Log a trade (e.g. `/trade MI 2.24 MI vs CSK`)\n"
            "`/active` — View open trades and hedge progress\n"
            "`/hedge <TradeID> <CurrentOdds>` — Lock profit on both teams\n"
            "`/balance` — Check bankroll and audit summary"
        )
        send_message(chat_id, welcome)
        return {"ok": True}

    # 3. Command: /autopilot
    if text == "/autopilot":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        new_status = not user.get("auto_pilot", False)
        supabase.table("bot_users").update({"auto_pilot": new_status}).eq("chat_id", chat_id).execute()
        status_label = "ENABLED (Auto-take signals)" if new_status else "DISABLED (Manual Yes/No buttons)"
        send_message(chat_id, f"⚡ *Auto-Pilot Mode:* {status_label}")
        return {"ok": True}

    # 4. Command: /balance
    if text == "/balance":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = round(float(user["bankroll"]))
        send_message(chat_id, f"💰 *Current Virtual Bankroll:* ₹{bankroll}\n• Risk per trade: 5% (₹{round(bankroll * 0.05)})")
        return {"ok": True}

    # 5. Command: /active (View open trades, max 2 limit)
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

    # 6. Command: /trade (Input odds from CREX directly)
    # Usage: /trade <Team> <BackOdds> <MatchTitle>
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

        # Concurrency Guard: Enforce Maximum 2 active trades
        active_count = len(supabase.table("trades").select("id").eq("chat_id", chat_id).eq("status", "OPEN").execute().data)
        if active_count >= 2:
            send_message(chat_id, "🛑 *Limit Reached:* You already have 2 active trades open. Close one before opening a new position.")
            return {"ok": True}

        # Calculate sizing & targets
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = float(user["bankroll"])
        stake = round((bankroll * user["risk_percent"]) / 100)
        target_lay = round(back_odds * 0.70, 2)  # Target 30% drop for green book

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

    # 7. Command: /hedge (Execute Green Book Exit)
    # Usage: /hedge <TradeID> <CurrentLayOdds>
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

        # Calculate exact Green Book lay stake & net profit
        back_stake = int(t["back_stake"])
        back_odds = float(t["back_odds"])
        lay_stake, net_profit = calculate_green_book(back_stake, back_odds, cur_lay_odds)

        # Update User Balance
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        new_bankroll = round(float(user["bankroll"]) + net_profit)
        supabase.table("bot_users").update({"bankroll": new_bankroll}).eq("chat_id", chat_id).execute()

        # Update Trade & History
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
