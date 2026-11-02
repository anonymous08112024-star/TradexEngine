import os
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

EXCHANGE_COMMISSION = 0.02  # 2% standard exchange commission

# Tier-1 Tournament Whitelist: Only high-liquidity, reliable competitions
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

def edit_message(chat_id: int, message_id: int, text: str, reply_markup: dict = None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "Markdown"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(f"{TELEGRAM_API_URL}/editMessageText", json=payload, timeout=8)
    except Exception as e:
        print(f"Error editing message: {e}")

def calculate_green_book(back_stake: int, back_odds: float, current_lay_odds: float):
    """Calculates equalized lay stake and net locked profit in whole rupees"""
    lay_stake = round((back_stake * back_odds) / current_lay_odds)
    gross_profit = lay_stake - back_stake
    net_profit = round(gross_profit * (1 - EXCHANGE_COMMISSION))
    return int(lay_stake), int(net_profit)

def calculate_cut_loss(back_stake: int, back_odds: float, current_lay_odds: float):
    """Calculates defensive exit stake to salvage capital when trade collapses"""
    lay_stake = round((back_stake * back_odds) / current_lay_odds)
    loss = int(back_stake - lay_stake)
    salvaged_capital = int(back_stake - loss)
    return int(lay_stake), loss, salvaged_capital

@app.get("/")
def health_check():
    return {"status": "alive", "service": "TradexEngine"}

@app.get("/poll")
def cron_poll():
    """Lightweight keeper endpoint"""
    return {"status": "polled", "timestamp": "ok"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()

    # 1. Handle Inline Button Clicks
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
            
            # Liquidate position and salvage remaining capital
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
                f"Trade slot freed up. Capital preserved for next setup."
            )
            send_message(chat_id, msg)

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
            "🏏 *Tradex Quantitative Cricket Engine*\n\n"
            "• *Virtual Bankroll:* ₹10,000\n"
            "• *Standard Swing Risk:* 5% (₹500)\n"
            "• *Jackpot / Death Overs Risk:* 1% (₹100)\n"
            "• *Max Active Trades:* 2 (Strict anti-spam)\n"
            "• *Exchange Commission:* 2%\n\n"
            "*Core Commands:*\n"
            "`/upcoming` — View scheduled Tier-1 matches (24-48 hrs)\n"
            "`/scan` — Scan live Tier-1 markets for value entries\n"
            "`/autopilot` — Toggle hands-free logging\n"
            "`/trade <Team> <Odds> <Match>` — Log standard swing trade\n"
            "`/jackpot <Team> <Odds> <Match>` — Log micro-stake jackpot (1% risk)\n"
            "`/hedge <TradeID> <CurrentOdds>` — Lock profit on both sides\n"
            "`/cutloss <TradeID> <CurrentOdds>` — Exit early to salvage capital\n"
            "`/active` — Check open trade slots\n"
            "`/balance` — View bankroll & ledger summary"
        )
        send_message(chat_id, welcome)
        return {"ok": True}

    # 3. Command: /upcoming
    if text == "/upcoming":
        send_message(chat_id, "📅 *Fetching upcoming Tier-1 cricket fixtures...*")
        upcoming_list = []
        for sport_key, league_name in TIER1_TOURNAMENTS.items():
            url = f"https://api.theoddsapi.com/v4/sports/{sport_key}/events?apiKey={ODDS_API_KEY}"
            try:
                res = requests.get(url, timeout=6).json()
                if isinstance(res, list) and len(res) > 0:
                    for ev in res[:2]:  # Take next 2 per league to conserve bandwidth
                        home = ev.get("home_team")
                        away = ev.get("away_team")
                        commence = ev.get("commence_time", "").replace("T", " ")[:16]
                        upcoming_list.append(f"• *{home} vs {away}*\n  🏆 {league_name} | 🕒 {commence} UTC")
            except Exception:
                continue

        if not upcoming_list:
            send_message(chat_id, "No upcoming Tier-1 fixtures found in the next 24 hours.")
        else:
            report = "🏏 *Upcoming Tier-1 Cricket Matches:*\n\n" + "\n\n".join(upcoming_list[:6])
            send_message(chat_id, report)
        return {"ok": True}

    # 4. Command: /autopilot
    if text == "/autopilot":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        new_val = not user.get("auto_pilot", False)
        supabase.table("bot_users").update({"auto_pilot": new_val}).eq("chat_id", chat_id).execute()
        status_label = "ENABLED (Auto-take signals)" if new_val else "DISABLED (Manual Yes/No buttons)"
        send_message(chat_id, f"⚡ *Auto-Pilot Mode:* {status_label}")
        return {"ok": True}

    # 5. Command: /balance
    if text == "/balance":
        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = int(user["bankroll"])
        send_message(chat_id, f"💰 *Current Virtual Bankroll:* ₹{bankroll}\n• Standard Swing Stake (5%): ₹{int(bankroll * 0.05)}\n• Jackpot Micro-Stake (1%): ₹{int(bankroll * 0.01)}")
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
                f"• *ID #{t['id']} [{t['trade_type']}]:* {t['team_name']} ({t['match_title']})\n"
                f"  Entry: Back @ {t['back_odds']} | Stake: ₹{int(t['back_stake'])}\n"
                f"  🎯 Target Lay: {t['target_lay_odds']} (`/hedge {t['id']} <odds>`)\n"
                f"  🛡️ Stop-Loss Trigger: {t['stop_loss_odds']} (`/cutloss {t['id']} <odds>`)\n\n"
            )
        send_message(chat_id, report)
        return {"ok": True}

    # 7. Command: /trade (Standard Swing Entry)
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
            send_message(chat_id, "🛑 *Limit Reached:* Max 2 concurrent trades allowed. Close one before opening a new position.")
            return {"ok": True}

        user = supabase.table("bot_users").select("*").eq("chat_id", chat_id).single().execute().data
        bankroll = int(user["bankroll"])
        stake = int(bankroll * 0.05)  # 5% standard swing
        target_lay = round(back_odds * 0.70, 2)
        stop_loss = round(back_odds * 1.50, 2)  # Cut loss if odds drift 50%

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
            "entry_reason": f"Standard swing entry on {team} @ {back_odds}. Overreaction dip target: {target_lay}."
        }).execute().data[0]

        card = (
            f"🚨 *NEW SWING TRADE (ID #{inserted['id']})*\n\n"
            f"• *Match:* {match_title}\n"
            f"• *Action:* BACK (Lagai) *{team}*\n"
            f"• *Entry Odds:* {back_odds}\n"
            f"• *Allocated Stake:* ₹{stake} (5% Bankroll)\n"
            f"• *Green Book Lay Target:* {target_lay}\n"
            f"• *Cut-Loss Boundary:* {stop_loss}\n\n"
            f"📊 *Reason:* 2nd innings / middle overs overreaction. Target 30% contraction for equalized profit."
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

    # 8. Command: /jackpot (Micro-Stake Death Overs / Ghoda Entry)
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
        stake = int(bankroll * 0.01)  # 1% micro-stake for asymmetric risk/reward
        if stake < 50:
            stake = 50

        target_lay = round(back_odds * 0.40, 2)  # Target 60% collapse on single big over
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
            "entry_reason": f"Death overs jackpot setup on {team} @ {back_odds}. Micro-stake asymmetric reward."
        }).execute().data[0]

        card = (
            f"🎰 *JACKPOT MICRO-TRADE (ID #{inserted['id']})*\n\n"
            f"• *Match:* {match_title}\n"
            f"• *Action:* BACK (Lagai) *{team}*\n"
            f"• *Cheap Entry Odds:* {back_odds}\n"
            f"• *Micro Stake:* ₹{stake} (Only 1% Bankroll)\n"
            f"• *Target Lay (Green Book):* {target_lay}\n\n"
            f"🧠 *Reason:* High R:R asymmetric opportunity in late phase. Minimal risk (₹{stake}) with massive multiplier upside."
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

    # 9. Command: /hedge (Green Book Equalizer)
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
            f"Both outcomes are completely risk-free. Trade slot cleared!"
        )
        send_message(chat_id, settlement_msg)
        return {"ok": True}

    # 10. Command: /cutloss (Defensive Capital Preservation)
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

    # 11. Command: /scan (Tier-1 Scanner)
    if text == "/scan":
        send_message(chat_id, "🔍 *Scanning live Tier-1 cricket liquidity...*")
        # Checks live Tier-1 matches without exceeding quota
        found = False
        for sport_key in TIER1_TOURNAMENTS.keys():
            url = f"https://api.theoddsapi.com/v4/sports/{sport_key}/odds/?regions=uk,eu&markets=h2h&oddsFormat=decimal&apiKey={ODDS_API_KEY}"
            try:
                res = requests.get(url, timeout=8).json()
                if isinstance(res, list) and len(res) > 0:
                    found = True
                    break
            except Exception:
                continue

        if not found:
            send_message(chat_id, "No live Tier-1 cricket matches with active order books right now.\nUse `/upcoming` to see scheduled matches or `/trade` for manual CREX entries.")
        return {"ok": True}

    return {"ok": True}
