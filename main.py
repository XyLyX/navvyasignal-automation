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
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]

# Which run this is: "general" (comprehensive, all desks) or "uae_refresh" (UAE-focused refresh)
RUN_TYPE = os.environ.get("RUN_TYPE", "general")
# Whether this run should actually send email/WhatsApp, or just refresh Notion content
SEND_OUTPUT = os.environ.get("SEND_OUTPUT", "true").lower() == "true"

DESKS = [
    "West Asia Desk",
    "Maritime & Energy Desk",
    "Markets & Capital Desk",
    "India Desk",
    "UAE Desk",
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
    """Pull recent Signal Feed entries so the model can decide update vs. new.
    Includes a content snippet and creation time so matching isn't based on
    title text alone — this is what lets same-day stories with slightly
    different figures get recognized as updates rather than duplicates."""
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    payload = {
        "page_size": 100,
        "sorts": [{"timestamp": "created_time", "direction": "descending"}],
    }
    resp = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=30)
    if resp.status_code != 200:
        fail_hard(f"Notion query failed: {resp.status_code} {resp.text}")
    results = resp.json().get("results", [])

    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=48)
    entries = []
    for page in results:
        created_time_str = page.get("created_time", "")
        try:
            created_dt = datetime.datetime.strptime(created_time_str[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            created_dt = None
        # Only include entries from the last 48h in the dedup context — older
        # entries are very unlikely to be the "same story" as today's news,
        # and keeping the list short/recent makes matching far more reliable.
        if created_dt and created_dt < cutoff:
            continue

        props = page.get("properties", {})
        title = ""
        if "Name" in props and props["Name"].get("title"):
            title = "".join([t.get("plain_text", "") for t in props["Name"]["title"]])
        category = ""
        if "Category" in props and props["Category"].get("select"):
            category = props["Category"]["select"].get("name", "")
        brief_snippet = ""
        if "Signal Brief" in props and props["Signal Brief"].get("rich_text"):
            brief_snippet = "".join([t.get("plain_text", "") for t in props["Signal Brief"]["rich_text"]])[:300]

        entries.append({
            "id": page["id"],
            "title": title,
            "category": category,
            "created_time": created_time_str,
            "content_snippet": brief_snippet,
        })
    return entries


# ---------- STEP 2: Generate briefing via Claude ----------

SYSTEM_PROMPT = """You are the editorial engine for NavvyaSignal, a daily intelligence \
publication covering West Asia, Maritime & Energy, Markets & Capital, India, UAE, Real Estate \
& Infrastructure, Sports, Trends & Forecasting, and Global Politics.

Rules you must follow strictly:
- Research current developments using web search. Never fabricate facts, figures, or quotes.
- Only include information you can verify from search results in this run.
- For each validated development, decide whether it UPDATES an existing Notion entry \
(provided below, with title, category, creation time, and a content snippet) or is genuinely NEW.
- CRITICAL: when setting "existing_id" for an update, copy the id string EXACTLY character-for-character \
from the provided existing entries list below. Do not retype or paraphrase it from memory — a single \
wrong character will cause the update to fail. If you are not fully certain of the exact id, treat \
the entry as "create" instead of guessing at an id.
- CRITICAL DEDUP RULE: if an existing entry was created within the last 48 hours, is in the \
same desk, and covers the same underlying subject (same commodity, same index, same conflict, \
same company, same event thread) — you MUST treat it as an UPDATE, even if the specific \
figures differ (e.g. an oil price from 15 minutes ago vs. now, a slightly different \
percentage). Do NOT create a new entry just because the exact numbers moved. Fast-moving \
stories (oil prices, market indices, an unfolding conflict) are expected to have updated \
figures on every run — that is exactly what should trigger an update, not a duplicate. \
Only create a new entry when the underlying subject itself is genuinely different from \
everything in the provided list.
- Assign each entry to exactly one of these desks: West Asia Desk, Maritime & Energy Desk, \
Markets & Capital Desk, India Desk, UAE Desk, Real Estate & Infrastructure Desk, Sports Desk, \
Trends & Forecasting Desk, Global Politics Desk. If genuinely ambiguous, pick the closest \
fit and note the ambiguity in a "notes" field — do not leave it blank.
- UAE DESK RULE: any story that is specifically about the UAE (Dubai, Abu Dhabi, Sharjah, or \
UAE federal policy/economy/markets) goes to UAE Desk as its primary desk, even if it would \
otherwise fit Real Estate & Infrastructure, Markets & Capital, or another desk. When a UAE \
story also has a clear secondary angle in another desk (e.g. a UAE real estate story with \
national market implications), mention that secondary desk naturally within the body prose \
(e.g. "This also carries implications for the broader Markets & Capital picture...") rather \
than using a separate field or splitting it into two entries.
- If RUN_TYPE is "uae_refresh": focus primarily on fresh UAE Desk developments since the last \
check. You may also update other desks' entries if something has changed significantly, but \
UAE Desk coverage is the priority for this run — this run happens every 6 hours specifically \
to keep UAE content current between the once-daily full briefing.
- If RUN_TYPE is "general": do a comprehensive sweep across all desks, since this is the once-daily \
full briefing that covers everything, including UAE Desk, India Desk, and all other desks together.
- BREAKING-NEWS RULE (ALL DESKS): on every run, regardless of RUN_TYPE, macro/desk-level topic \
searches (oil prices, market indices, ongoing conflict threads, policy analysis, etc.) will NOT \
reliably surface acute breaking incidents on their own — those need their own dedicated search \
pass per desk, in addition to your usual macro searches. Run at least one incident-focused search \
for EACH of the following, using the current date in the query:
  * UAE Desk: "Dubai Media Office statement today", "UAE Civil Defence incident today", "Abu Dhabi \
incident today" — explosions, fires, industrial/transport accidents, building issues, severe \
weather.
  * West Asia Desk: breaking regional incidents (attacks, strikes, political upheaval, protests, \
sudden military movements) beyond whatever is already tracked in ongoing conflict threads.
  * Maritime & Energy Desk: tanker/vessel incidents, port or refinery accidents, pipeline \
disruptions, shipping lane closures — not just price/index movements.
  * Markets & Capital Desk: flash crashes, circuit breakers, emergency central bank action, major \
unscheduled earnings or guidance shocks.
  * India Desk: breaking incidents (accidents, disasters, major political events) inside India.
  * Real Estate & Infrastructure Desk: building collapses, major project cancellations/approvals, \
construction accidents.
  * Sports Desk: breaking results, serious injuries, disciplinary or scandal news.
  * Global Politics Desk: breaking political events — resignations, elections, coups, sudden \
policy reversals — beyond scheduled/expected developments.
  * Trends & Forecasting Desk: breaking data releases or reports that shift an existing forecast, \
if any surface.
An acute incident with real-world impact (injuries, fatalities, market/operational disruption) is \
newsworthy on its own and belongs on its desk even without further analytical framing — do not \
skip a desk's incident pass just because other desks already have enough material for this run.
- Each Notion entry body must include full "What Happened" and "Why It Matters" sections \
with real figures, attributions, and analysis — not a one-line summary.
- Subject lines and headers must use proper case ("Navvya Signal - Daily Briefing"), never \
all-caps.
- If nothing meaningful changed since the last run, it is correct to return zero entries \
for that section rather than padding with a no-op update.
- Never carry forward a stale/outdated figure without flagging or correcting it.

Output ONLY valid JSON matching this schema — no preamble, no narration of your research process, \
no explanation before or after, and no markdown code fences. Do not use <cite> tags or any citation \
markup in the JSON string values — write plain prose with sources named inline in the "sources_text" \
field instead. Your entire response must be parseable as JSON from the first character.

Schema:
{
  "edition_label": "string, e.g. '2026-08-01, 12:00 GST edition'",
  "editor_note": "string, 1-3 sentences on corrections/context, or empty string",
  "notion_entries": [
    {
      "action": "update" or "create",
      "existing_id": "notion page id if action=update, else null",
      "title": "string",
      "desk": "one of the 8 desk names exactly as listed above",
      "body_markdown": "string, max 1800 chars, flowing prose covering what happened and why it \
matters — do NOT use markdown syntax like ## headers or ** bold **, since this is stored in a \
Notion rich-text property that displays plain text literally, not rendered markdown. Structure \
it as clear paragraphs instead: one or two paragraphs on what happened, then a paragraph on why \
it matters — no visible section labels or markdown symbols of any kind.",
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
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user_prompt}],
    )

    # Collect all text blocks (model may interleave search calls and text)
    text_parts = [block.text for block in response.content if block.type == "text"]
    full_text = "\n".join(text_parts).strip()

    # Find the JSON object regardless of any preamble text or code fences
    import re
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", full_text, re.DOTALL)
    if fence_match:
        json_str = fence_match.group(1)
    else:
        # No fence — find the first '{' and the matching last '}'
        start = full_text.find("{")
        end = full_text.rfind("}")
        if start == -1 or end == -1 or end < start:
            fail_hard(f"Could not locate a JSON object in model output.\nRaw output:\n{full_text[:2000]}")
        json_str = full_text[start:end + 1]

    # Strip citation tags like <cite index="...">...</cite> the model may have
    # carried over from search-result formatting — these aren't valid in our schema.
    json_str = re.sub(r"</?cite[^>]*>", "", json_str)

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        fail_hard(f"Model output was not valid JSON: {e}\nExtracted text:\n{json_str[:2000]}")

    required_keys = ["edition_label", "notion_entries", "email_subject", "email_html", "whatsapp_text"]
    for k in required_keys:
        if k not in data:
            fail_hard(f"Model output missing required key: {k}")

    return data


# ---------- STEP 2.5: Gemini cross-verification ----------

def call_gemini(prompt_text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-3.5-flash-lite:generateContent?key={GEMINI_API_KEY}"
    payload = {"contents": [{"parts": [{"text": prompt_text}]}]}
    resp = requests.post(url, json=payload, timeout=60)
    if resp.status_code != 200:
        log(f"WARNING: Gemini call failed ({resp.status_code}): {resp.text[:500]}")
        return None
    try:
        return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError):
        log(f"WARNING: Unexpected Gemini response shape: {resp.text[:500]}")
        return None


def gemini_review(briefing_json_str):
    prompt = f"""You are fact-checking a draft news briefing before publication for NavvyaSignal, \
a credibility-focused intelligence publication. Review the JSON draft below.

Flag ONLY genuine concerns: factual claims that seem implausible, internally contradictory, \
unsupported by the stated sources, or that you have reason to believe are outdated or wrong. \
Do not flag stylistic choices or things you simply cannot verify either way — only flag \
things you have an actual, specific reason to doubt.

Respond in this exact format:
FLAGS: <number of concerns, 0 if none>
If FLAGS > 0, list each concern on its own line starting with "- ", specific enough to act on.

Draft to review:
{briefing_json_str}"""
    return call_gemini(prompt)


def claude_respond_to_flags(briefing_data, gemini_flags_text):
    """Ask Claude to address Gemini's specific concerns: confirm with better sourcing,
    revise, or explain — using web search to re-check if needed."""
    prompt = f"""Gemini raised the following concerns about your draft briefing:

{gemini_flags_text}

For each concern, either:
1. Re-verify via web search and confirm the claim stands (explain why), or
2. Revise the specific claim to be accurate, or
3. If genuinely uncertain after re-checking, soften the claim with appropriate hedging \
language (e.g. "single-source, unconfirmed" or "disputed") rather than stating it flatly \
or dropping it — per NavvyaSignal's credibility protocol of labeled inference over fabrication.

Current draft JSON:
{json.dumps(briefing_data)}

Output the FULL corrected JSON (same schema as before), with fixes applied. Output ONLY \
the JSON, no other text."""

    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    )
    text_parts = [block.text for block in response.content if block.type == "text"]
    full_text = "\n".join(text_parts).strip()

    import re
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", full_text, re.DOTALL)
    json_str = fence_match.group(1) if fence_match else full_text[full_text.find("{"):full_text.rfind("}") + 1]
    json_str = re.sub(r"</?cite[^>]*>", "", json_str)

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        log("WARNING: Claude's revision after Gemini flags was not valid JSON — keeping prior draft.")
        return briefing_data


def verify_with_gemini_loop(briefing_data, max_rounds=2):
    """Cross-verification loop: Gemini reviews, Claude responds to flags, Gemini re-reviews.
    If flags persist after max_rounds, proceed with Claude's best (hedged) version rather
    than blocking the run indefinitely."""
    for round_num in range(1, max_rounds + 1):
        log(f"Gemini verification round {round_num}...")
        review = gemini_review(json.dumps(briefing_data))
        if review is None:
            log("Gemini review unavailable this round — proceeding without cross-verification.")
            return briefing_data

        flags_count = 0
        for line in review.splitlines():
            if line.strip().upper().startswith("FLAGS:"):
                try:
                    flags_count = int("".join(c for c in line.split(":")[1] if c.isdigit()) or "0")
                except ValueError:
                    flags_count = 0
                break

        if flags_count == 0:
            log("Gemini review: no concerns raised.")
            return briefing_data

        log(f"Gemini raised {flags_count} concern(s):\n{review}")
        briefing_data = claude_respond_to_flags(briefing_data, review)

    log(f"Concerns persisted after {max_rounds} rounds — proceeding with Claude's hedged/revised version.")
    return briefing_data




def push_to_notion(entries, valid_existing_ids):
    summary = []
    for entry in entries:
        desk = entry["desk"]
        if desk not in DESKS:
            fail_hard(f"Model returned invalid desk category: {desk}")

        # Guard against the model slightly mis-copying a long Notion page ID
        # (a known LLM failure mode) — if the claimed existing_id doesn't match
        # anything we actually fetched, fall back to creating a new entry
        # instead of crashing the whole run on a 404.
        if entry["action"] == "update" and entry.get("existing_id") not in valid_existing_ids:
            log(f"WARNING: existing_id '{entry.get('existing_id')}' for '{entry['title']}' "
                f"doesn't match any fetched entry — treating as create instead of update.")
            entry["action"] = "create"
            entry["existing_id"] = None

        # Notion rich_text properties display markdown literally (not rendered) — strip any
        # that slipped through despite the prompt instruction, so it never shows as "## " on the live site.
        import re as _re
        clean_body = _re.sub(r"^#{1,6}\s*", "", entry["body_markdown"], flags=_re.MULTILINE)
        clean_body = _re.sub(r"\*\*(.+?)\*\*", r"\1", clean_body)

        # Notion has a 2000-char limit per rich_text content block
        signal_brief = clean_body[:2000]
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
            log(f"WARNING: Notion write failed for '{entry['title']}' — skipping this entry, "
                f"continuing with the rest of the run. {resp.status_code} {resp.text[:500]}")
            continue

        summary.append(f"{action_label} — {entry['title']} ({desk})" + (f" [NOTE: {entry['notes']}]" if entry.get("notes") else ""))
        log(summary[-1])

    return summary


# ---------- STEP 4: Send via Kit ----------

def send_kit(subject, html_content):
    now = datetime.datetime.utcnow()
    send_at = (now + datetime.timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%SZ")

    url = "https://api.kit.com/v4/broadcasts"
    headers = {"X-Kit-Api-Key": KIT_API_KEY, "Content-Type": "application/json"}
    payload = {
        "subject": subject,
        "content": html_content,
        "public": False,
        "published_at": send_at,
        "send_at": send_at,
        "email_address": KIT_FROM_EMAIL,
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    if resp.status_code != 201:
        fail_hard(f"Kit send failed: {resp.status_code} {resp.text}")
    broadcast_id = resp.json()["broadcast"]["id"]
    log(f"Kit broadcast created: id={broadcast_id}, send_at={send_at}")
    return broadcast_id


def verify_kit_sent(broadcast_id, wait_seconds=420):
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

    briefing = verify_with_gemini_loop(briefing)

    valid_existing_ids = {e["id"] for e in existing}
    notion_summary = push_to_notion(briefing["notion_entries"], valid_existing_ids)

    if SEND_OUTPUT:
        broadcast_id = send_kit(briefing["email_subject"], briefing["email_html"])
        verify_kit_sent(broadcast_id)
        send_whapi(briefing["whatsapp_text"])
    else:
        log("SEND_OUTPUT is false — this is a Notion-refresh-only run, skipping Kit/Whapi sends.")

    log("Run complete. Summary:")
    for line in notion_summary:
        log(f"  {line}")


if __name__ == "__main__":
    main()
