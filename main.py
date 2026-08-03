#!/usr/bin/env python3
"""
NavvyaSignal Daily Briefing Automation
Runs on a schedule (via GitHub Actions), generates a fact-checked briefing,
pushes validated entries to Notion, and sends via Kit (email) and Whapi (WhatsApp).

Fully automatic: no human approval step. Notion 'Ready to Post' is set to True
on every entry. Kit and Whapi sends fire immediately after generation.
"""

import os
import sys
import json
import time
import datetime
import requests
import anthropic

# ---------- CONFIG ----------

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
NOTION_API_KEY = os.environ["NOTION_API_KEY"]
NOTION_DATABASE_ID = os.environ["NOTION_DATABASE_ID"]
KIT_API_KEY = os.environ["KIT_API_KEY"]
KIT_FROM_EMAIL = os.environ.get("KIT_FROM_EMAIL", "hello@navvyasignal.com")
WHAPI_TOKEN = os.environ.get("WHAPI_TOKEN", "")
WHAPI_CHANNEL_ID = os.environ.get("WHAPI_CHANNEL_ID", "")

# Which run this is: "general" (00:00/12:00 GST) or "india" (18:00 GST)
RUN_TYPE = os.environ.get("RUN_TYPE", "general")

DESKS = [
    "West Asia Desk",
    "Maritime & Energy Desk",
    "Markets & Capital Desk",
    "India Desk",
    "Real Estate & Infrastructure Desk",
    "Sports Desk",
    "Trends & Forecasting Desk",
    "Global Politics Desk",
]

NOTION_VERSION = "2022-06-28"
NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_API_KEY}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def log(msg):
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{ts}] {msg}", flush=True)


def fail_hard(msg):
    """Log an error and exit non-zero so GitHub Actions marks the run as failed.
    We deliberately do NOT send partial/broken content."""
    log(f"FATAL: {msg}")
    sys.exit(1)


# ---------- STEP 1: Fetch existing Notion entries (for dedup) ----------

def fetch_existing_entries():
    """Pull recent Signal Feed entries so the model can decide update vs. new."""
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    payload = {
        "page_size": 50,
        "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
    }
    resp = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=30)
    if resp.status_code != 200:
        fail_hard(f"Notion query failed: {resp.status_code} {resp.text}")
    results = resp.json().get("results", [])
    entries = []
    for page in results:
        props = page.get("properties", {})
        title = ""
        if "Name" in props and props["Name"].get("title"):
            title = "".join([t.get("plain_text", "") for t in props["Name"]["title"]])
        category = ""
        if "Category" in props and props["Category"].get("select"):
            category = props["Category"]["select"].get("name", "")
        entries.append({"id": page["id"], "title": title, "category": category})
    return entries


# ---------- STEP 2: Generate briefing via Claude ----------

SYSTEM_PROMPT = """You are the editorial engine for NavvyaSignal, a daily intelligence \
publication covering West Asia, Maritime & Energy, Markets & Capital, India, Real Estate \
& Infrastructure, Sports, Trends & Forecasting, and Global Politics.

Rules you must follow strictly:
- Research current developments using web search. Never fabricate facts, figures, or quotes.
- Only include information you can verify from search results in this run.
- For each validated development, decide whether it UPDATES an existing Notion entry \
(provided below) or is genuinely NEW. Prefer updating over duplicating when the story \
is a continuation of the same underlying event/trend.
- Assign each entry to exactly one of these desks: West Asia Desk, Maritime & Energy Desk, \
Markets & Capital Desk, India Desk, Real Estate & Infrastructure Desk, Sports Desk, \
Trends & Forecasting Desk, Global Politics Desk. If genuinely ambiguous, pick the closest \
fit and note the ambiguity in a "notes" field — do not leave it blank.
- Each Notion entry body must include full "What Happened" and "Why It Matters" sections \
with real figures, attributions, and analysis — not a one-line summary.
- If this is the India Desk run, focus primarily on India Desk signals, and include \
other-desk signals ONLY if they have a direct, material India angle.
- Subject lines and headers must use proper case ("Navvya Signal - Daily Briefing"), never \
all-caps.
- If nothing meaningful changed since the last run, it is correct to return zero entries \
for that section rather than padding with a no-op update.
- Never carry forward a stale/outdated figure without flagging or correcting it.

Output ONLY valid JSON matching this schema, no other text:
{
  "edition_label": "string, e.g. '2026-08-01, 12:00 GST edition'",
  "editor_note": "string, 1-3 sentences on corrections/context, or empty string",
  "notion_entries": [
    {
      "action": "update" or "create",
      "existing_id": "notion page id if action=update, else null",
      "title": "string",
      "desk": "one of the 8 desk names exactly as listed above",
      "body_markdown": "string, max 1800 chars, with What Happened / Why It Matters sections",
      "sources_text": "string, max 1800 chars, e.g. 'Sources: Reuters, AP. Quotes verified across outlets.'",
      "notes": "string, e.g. ambiguity flag, or empty string"
    }
  ],
  "email_subject": "string",
  "email_html": "string, full HTML body for the email",
  "whatsapp_text": "string, staccato style, no markdown, ends with navvyasignal.com invite"
}
"""


def generate_briefing(existing_entries):
    user_prompt = f"""Run type: {RUN_TYPE}
Current UTC time: {datetime.datetime.utcnow().isoformat()}Z

Existing recent Signal Feed entries (id | title | desk) for dedup reference:
{json.dumps(existing_entries, indent=2)}

Research today's developments and produce the JSON output per your instructions."""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Collect all text blocks (model may interleave search calls and text)
    text_parts = [block.text for block in response.content if block.type == "text"]
    full_text = "\n".join(text_parts).strip()

    # Strip markdown code fences if present
    if full_text.startswith("```"):
        full_text = full_text.split("```")[1]
        if full_text.startswith("json"):
            full_text = full_text[4:]

    try:
        data = json.loads(full_text)
    except json.JSONDecodeError as e:
        fail_hard(f"Model output was not valid JSON: {e}\nRaw output:\n{full_text[:2000]}")

    required_keys = ["edition_label", "notion_entries", "email_subject", "email_html", "whatsapp_text"]
    for k in required_keys:
        if k not in data:
            fail_hard(f"Model output missing required key: {k}")

    return data


# ---------- STEP 3: Push to Notion ----------

def push_to_notion(entries):
    summary = []
    for entry in entries:
        desk = entry["desk"]
        if desk not in DESKS:
            fail_hard(f"Model returned invalid desk category: {desk}")

        # Notion has a 2000-char limit per rich_text content block
        signal_brief = entry["body_markdown"][:2000]
        sources_text = entry.get("sources_text", "")[:2000]

        properties = {
            "Name": {"title": [{"text": {"content": entry["title"]}}]},
            "Category": {"select": {"name": desk}},
            "Signal Brief": {"rich_text": [{"text": {"content": signal_brief}}]},
            "Text 1": {"rich_text": [{"text": {"content": sources_text}}]},
            "Long Read": {"checkbox": False},
            "Ready to Post": {"checkbox": True},  # fully automatic, per instruction
        }
        if entry.get("notes"):
            properties["Internal Note"] = {"rich_text": [{"text": {"content": entry["notes"][:2000]}}]}

        if entry["action"] == "update" and entry.get("existing_id"):
            url = f"https://api.notion.com/v1/pages/{entry['existing_id']}"
            resp = requests.patch(url, headers=NOTION_HEADERS, json={"properties": properties}, timeout=30)
            action_label = "Updated"
        else:
            url = "https://api.notion.com/v1/pages"
            payload = {
                "parent": {"database_id": NOTION_DATABASE_ID},
                "properties": properties,
            }
            resp = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=30)
            action_label = "Created"

        if resp.status_code not in (200, 201):
            fail_hard(f"Notion write failed for '{entry['title']}': {resp.status_code} {resp.text}")

        summary.append(f"{action_label} — {entry['title']} ({desk})" + (f" [NOTE: {entry['notes']}]" if entry.get("notes") else ""))
        log(summary[-1])

    return summary


# ---------- STEP 4: Send via Kit ----------

def send_kit(subject, html_content):
    now = datetime.datetime.utcnow()
    send_at = (now + datetime.timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = "https://api.kit.com/v4/broadcasts"
    headers = {"X-Kit-Api-Key": KIT_API_KEY, "Content-Type": "application/json"}
    payload = {
        "subject": subject,
        "content": html_content,
        "public": False,
        "published_at": send_at,
        "send_at": send_at,
        "email_address": KIT_FROM_EMAIL,
        "subscriber_filter": [{"all": [{"type": "all_subscribers"}]}],
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code != 201:
        fail_hard(f"Kit send failed: {resp.status_code} {resp.text}")
    broadcast_id = resp.json()["broadcast"]["id"]
    log(f"Kit broadcast created: id={broadcast_id}, send_at={send_at}")
    return broadcast_id


def verify_kit_sent(broadcast_id, wait_seconds=180):
    """Poll the broadcast stats endpoint until it reports completed, or timeout."""
    url = f"https://api.kit.com/v4/broadcasts/{broadcast_id}/stats"
    headers = {"X-Kit-Api-Key": KIT_API_KEY}
    waited = 0
    while waited < wait_seconds:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            status = resp.json().get("broadcast", {}).get("stats", {}).get("status")
            if status == "completed":
                log(f"Kit broadcast {broadcast_id} confirmed completed.")
                return True
        time.sleep(15)
        waited += 15
    log(f"WARNING: Kit broadcast {broadcast_id} did not confirm 'completed' within {wait_seconds}s.")
    return False


# ---------- STEP 5: Send via Whapi ----------

def send_whapi(text):
    if not WHAPI_TOKEN or not WHAPI_CHANNEL_ID:
        log("WARNING: Whapi credentials not configured — skipping WhatsApp send for this run.")
        return
    url = "https://gate.whapi.cloud/messages/text"
    headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
    payload = {"to": WHAPI_CHANNEL_ID, "body": text}
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code != 200 or not resp.json().get("sent"):
        fail_hard(f"Whapi send failed: {resp.status_code} {resp.text}")
    log("Whapi message sent successfully.")


# ---------- MAIN ----------

def main():
    log(f"Starting NavvyaSignal automated run (type={RUN_TYPE})")

    existing = fetch_existing_entries()
    log(f"Fetched {len(existing)} existing Notion entries for dedup reference.")

    briefing = generate_briefing(existing)
    log(f"Generated briefing: {briefing['edition_label']}, {len(briefing['notion_entries'])} entries")

    # Basic staleness/sanity guard: refuse to proceed if the model returned zero
    # entries AND empty email content — that's a sign generation failed silently.
    if not briefing["notion_entries"] and not briefing["email_html"].strip():
        fail_hard("Generation produced no entries and no email content — refusing to send.")

    notion_summary = push_to_notion(briefing["notion_entries"])

    broadcast_id = send_kit(briefing["email_subject"], briefing["email_html"])
    verify_kit_sent(broadcast_id)

    send_whapi(briefing["whatsapp_text"])

    log("Run complete. Summary:")
    for line in notion_summary:
        log(f"  {line}")


if __name__ == "__main__":
    main()
