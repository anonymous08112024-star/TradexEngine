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

EXCHANGE_COMMISSION = 0.02

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
        print(f"Telegram error: {e}")
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
# 1. AUTOMATED CREX SCRAPER & NEXT_DATA EXTRACTOR
# ===================================================
def fetch_crex_live_data():
    """
    Auto-discovers live T20 matches and extracts their real-time 
    decimal odds directly from CREX Next.js payload.
    """
    url = "https://crex.com/live-score"
    matches = []
    
    t20_whitelist = ["T20", "WBBL", "BBL", "IPL", "WPL", "PSL", "SA20", "CPL", "BLAST", "HUNDRED"]
    exclusions = ["ODI", "TEST", "FIRST-CLASS", "LIST A"]

    try:
        resp = requests.get(url, headers=HEADERS, timeout=8)
        if resp.status_code != 200:
            return matches

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
                
                # Format check
                if any(ex in f"{s_name} {m_format}" for ex in exclusions):
                    continue
                if not any(kw in f"{s_name} {m_format} {m_title}" for kw in t20_whitelist):
                    continue

                # Extract live odds
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
        print(f"CREX engine note: {e}")

    return matches

# ===================================================
# 2. MULTI-DAY T20 SCHEDULE ENGINE (WBBL / GLOBAL)
# ===================================================
def fetch_upcoming_t20_schedule():
    """
    Pulls multi-day schedules from ESPN Cricinfo JSON feed without API cost.
    """
    endpoints = [
        "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/current?lang=en&latest=true",
        "https://hs-consumer-api.espncricinfo.com/v1/pages/matches/schedule?lang=en"
    ]
    t20_keywords = ["T20", "TWENTY20", "T20I", "WT20", "IPL", "WPL", "BBL", "WBBL", "BIG BASH", "WOMEN'S BIG BASH", "PSL", "SA20", "CPL", "WCPL", "MLC", "BLAST", "HUNDRED"]
    exclusions = ["ODI", "ONE-DAY", "TEST", "FIRST-CLASS", "LIST A", "50 OVER"]

    upcoming = []
    seen = set()

    for u in endpoints:
        try:
            res = requests.get(u, headers=HEADERS, timeout=8).json()
            content = res.get("content", {})
            raw = content.get("matches", []) or res.get("matches", [])

            if not raw and "days" in content:
                for d in content.get("days", []):
                    raw.extend(d.get("matches", []))

            for m in raw:
                if not isinstance(m, dict):
                    continue
                mid = str(m.get("objectId", ""))
                if mid in seen:
                    continue

                combined = f"{m.get('format', '')} {m.get('series', {}).get('name', '')} {m.get('title', '')}".upper()
                if any(ex in combined for ex in exclusions) or not any(kw in combined for kw in t20_keywords):
                    continue

                teams = m.get("teams", [])
                if len(teams) < 2:
                    continue

                t1_name = teams[0].get("team", {}).get("name", "Team 1")
                t2_name = teams[1].get("team", {}).get("name", "Team 2")
                status_text = m.get("statusText") or "Scheduled"

                upcoming.append({
                    "id": mid,
                    "title": f"{t1_name} vs {t2_name}",
                    "series": m.get("series", {}).get("name", "T20 League"),
                    "status_text": status_text
                })
                seen.add(mid)
        except Exception:
            continue

    return upcoming

# ===================================================
# 3. BACKGROUND CRON ENGINE
# ===================================================
def automated_background_engine():
    users = supabase.table("bot_users").select("*").execute().data
    if not users:
        return

    # Check CREX live odds
    crex_matches = fetch_crex_live_data()

    for u in users:
        chat_id = u["chat_id"]
        bankroll = int(u["bankroll"])
        autopilot = u.get("auto_pilot", False)

        # 1. Check open trades against live odds
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

                # GREEN BOOK TARGET REACHED
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
                            f"🛡️ *CREX RATE DRIFT: CUT-LOSS ALERT*\n\n"
                            f"• *Match:* {trade['match_title']}\n"
                            f"• *Team:* {trade['team_name']}\n"
                            f"• Odds drifted to: {cur_price} (Limit: {stop_loss})\n"
                            f"• Salvageable: ₹{salvaged} of ₹{int(trade['back_stake'])}\n\n"
                            f"Tap below or send `/cutloss {t_id} {cur_price}` to preserve capital."
                        )
                        send_message(chat_id, msg, reply_markup=keyboard)
                        mark_alert_sent(alert_key)

        # 2. Automated value swing entries (Overs 3-15: 2.10 to 2.50)
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
    return {"status": "alive", "engine": "CREX_Automated_Engine"}

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
            "• *Data Source:* Live CREX Next.js Odds & Multi-day schedule\n"
            "• *Virtual Bankroll:* ₹10,000\n"
            "• *Standard Swing (Overs 3-15):* 5% (₹500)\n"
            "• *Death Overs Jackpot (Overs 16-20):* 1% (₹100)\n"
            "• *Max Active Trades:* 2 (Strict concurrency guard)\n\n"
            "*Commands:*\n"
            "`/upcoming` — Multi-day T20 & WBBL schedule\n"
            "`/live` — Live CREX match odds\n"
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
        send_message(chat_id, "📅 *Scanning multi-day schedule for WBBL & global T20s...*")
        upcoming = fetch_upcoming_t20_schedule()
        if not upcoming:
            send_message(chat_id, "No scheduled T20 fixtures found.")
        else:
            report = "🏏 *Upcoming T20 Matches:*\n\n"
            for m in upcoming[:8]:
                report += f"• *{m['title']}*\n  🏆 {m['series']}\n  🕒 {m['status_text']}\n\n"
            send_message(chat_id, report)
        return {"ok": True}

    if text == "/live":
        send_message(chat_id, "📡 *Scanning live CREX match odds...*")
        crex_data = fetch_crex_live_data()
        if not crex_data:
            send_message(chat_id, "No live T20 match in progress on CREX right now.\n(WBBL Match starts tomorrow at 1:40 pm IST).\n\nUse `/upcoming` to view the multi-day schedule.")
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
