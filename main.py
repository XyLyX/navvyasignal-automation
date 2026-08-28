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

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
NOTION_API_KEY = os.environ.get("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID", "")
KIT_API_KEY = os.environ.get("KIT_API_KEY", "")
KIT_FROM_EMAIL = os.environ.get("KIT_FROM_EMAIL", "hello@navvyasignal.com")
WHAPI_TOKEN = os.environ.get("WHAPI_TOKEN", "")
WHAPI_CHANNEL_ID = os.environ.get("WHAPI_CHANNEL_ID", "")
OPS_NOTIFY_NUMBER = os.environ.get("OPS_NOTIFY_NUMBER", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Which run this is: "group_a" / "group_b" / "group_c" (research + Notion push for that
# group's 3 desks only, no send) or "compile_send" (no research — compiles today's already-
# researched Notion entries into the single daily email + WhatsApp send).
RUN_TYPE = os.environ.get("RUN_TYPE", "compile_send")
# Whether this run should actually send email/WhatsApp. Only ever true for compile_send —
# group runs never send regardless of this flag (enforced in main(), not just here).
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

# Desks are split into 3 balanced groups (each mixing a heavier desk with lighter ones) so a
# single research call never has to split its attention across all 9 desks at once — that
# split-attention pattern was the root cause of desks being silently skipped under the old
# single-call "general" run. Each group gets its own dedicated run, staggered through the day;
# a final compile_send run assembles everything into one daily email + WhatsApp send.
GROUPS = {
    "group_a": ["West Asia Desk", "UAE Desk", "Trends & Forecasting Desk"],
    "group_b": ["Maritime & Energy Desk", "India Desk", "Real Estate & Infrastructure Desk"],
    "group_c": ["Markets & Capital Desk", "Global Politics Desk", "Sports Desk"],
}

# How far back compile_send looks in Notion for "today's" entries to compile. Group A starts
# at 13:30 GST and compile_send runs at 18:30 GST — a 5 hour span — so 6 hours gives buffer
# for a group run that started slightly late without pulling in yesterday's entries.
COMPILE_WINDOW_HOURS = 6

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


def fetch_todays_entries_for_compile():
    """Pull today's already-researched entries (from the group_a/group_b/group_c runs earlier
    today) for compile_send to assemble into the final digest. Unlike fetch_existing_entries
    (which only needs a short snippet for dedup matching), this needs the FULL body text since
    it's the actual source material for the compiled email/WhatsApp send — no re-research or
    re-writing of facts happens at compile time, only formatting/assembly of what's already
    validated and in Notion."""
    url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    payload = {
        "page_size": 100,
        "sorts": [{"timestamp": "last_edited_time", "direction": "descending"}],
    }
    resp = requests.post(url, headers=NOTION_HEADERS, json=payload, timeout=30)
    if resp.status_code != 200:
        fail_hard(f"Notion query failed: {resp.status_code} {resp.text}")
    results = resp.json().get("results", [])

    cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=COMPILE_WINDOW_HOURS)
    entries = []
    for page in results:
        edited_time_str = page.get("last_edited_time", "")
        try:
            edited_dt = datetime.datetime.strptime(edited_time_str[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            edited_dt = None
        if edited_dt and edited_dt < cutoff:
            continue

        props = page.get("properties", {})
        title = ""
        if "Name" in props and props["Name"].get("title"):
            title = "".join([t.get("plain_text", "") for t in props["Name"]["title"]])
        desk = ""
        if "Category" in props and props["Category"].get("select"):
            desk = props["Category"]["select"].get("name", "")
        body = ""
        if "Signal Brief" in props and props["Signal Brief"].get("rich_text"):
            body = "".join([t.get("plain_text", "") for t in props["Signal Brief"]["rich_text"]])
        sources = ""
        if "Text 1" in props and props["Text 1"].get("rich_text"):
            sources = "".join([t.get("plain_text", "") for t in props["Text 1"]["rich_text"]])

        entries.append({
            "title": title,
            "desk": desk,
            "body": body,
            "sources": sources,
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
- SCOPE RULE: this run covers ONLY the specific desks listed under "Desks in scope for this \
run" in the user message below — a subset of the 9 desks, never all of them. Apply the DAILY \
ROUNDUP RULE and BREAKING-NEWS RULE (below) IN FULL to each in-scope desk — give them the same \
thorough, real search effort you would give a single desk on its own. Do NOT research, write, \
or return any entry for a desk that is not in scope this run, even if you're aware of breaking \
news there — a separate dedicated run covers that desk on its own schedule later. This scoping \
exists specifically so each run gives its 2-3 desks real, complete attention instead of \
splitting effort thin across all 9 at once.
- This run does NOT send email or WhatsApp. Leave "email_subject", "email_html", and \
"whatsapp_text" as empty strings — a separate later run compiles everything into the actual \
send. Only "edition_label", "editor_note", and "notion_entries" matter for this run.
- DAILY ROUNDUP RULE: "your usual macro searches" must include at least one genuinely broad, \
outlet-level roundup search per desk, using the current date — not just topic-specific queries \
tied to whatever conflict thread or index you already expect to update. A desk-level search for \
"oil prices" or "Iran Hormuz" will find those threads but will NOT surface unrelated developments \
like a diplomatic statement, an aid package, an infrastructure announcement, a regulatory change, \
or a human-interest story that has nothing to do with the thread you were already tracking — those \
need their own broad sweep. Run at least one query like these per desk, adapted to the desk's beat:
  * UAE Desk: "UAE news today", "Khaleej Times today", "Gulf News UAE today" — covering diplomacy, \
government announcements, infrastructure, aid, regulation, and human-interest stories, not just \
conflict-adjacent or incident news.
  * India Desk: "India news today", "Reuters India today", "Indian Express today", "Times of \
India today" — covering diplomacy, government announcements, infrastructure, economic policy, \
regulation, and human-interest stories, not just accidents/disasters or the specific threads \
already tracked. Prioritize Reuters for low-noise business/economy/markets/policy signal, Indian \
Express for politics/government/courts, The Hindu when depth/context matters more than speed, and \
Times of India/Hindustan Times for breadth. When multiple India stories compete for space, weight \
toward what matters to a UAE-based reader with India business, trade, or investment exposure — \
markets, RBI/policy moves, trade ties, infrastructure, and major national events — over purely \
domestic political or celebrity/crime stories with no external relevance.
  * West Asia Desk: "Middle East news today" beyond the primary conflict thread already tracked.
  * Markets & Capital Desk: broad market roundup beyond the specific indices already tracked.
  * Real Estate & Infrastructure Desk: "infrastructure news today", project announcements beyond \
collapses/accidents.
  * Sports Desk: general sports roundup beyond the leagues/injuries already tracked.
  * Global Politics Desk: general political roundup beyond elections/coups already tracked.
  Apply the same principle to Maritime & Energy and Trends & Forecasting Desks: a topic-specific \
search finds what you already expect — a broad roundup search finds what you don't.
- BREAKING-NEWS RULE: macro/desk-level topic searches (oil prices, market indices, ongoing conflict \
threads, policy analysis, etc.) will NOT reliably surface acute breaking incidents on their own — \
those need their own dedicated search pass per desk, in addition to your usual macro searches. Run \
at least one incident-focused search for each in-scope desk below, using the current date in the \
query. Scope depends on RUN_TYPE:
  Run a dedicated breaking-news pass for every desk in scope for this run (see SCOPE RULE \
above) — typically 2-3 desks per run, never all 9. Do not run breaking-news passes for \
out-of-scope desks; they get their own dedicated run.
  * UAE Desk: "Dubai Media Office statement today", "UAE Civil Defence incident today", "Abu Dhabi \
incident today" — explosions, fires, industrial/transport accidents, structural/building issues, \
deaths or casualties (falls, drownings, road accidents), severe weather.
  * India Desk: "India accident today", "India disaster today", "PTI breaking news" — accidents, \
natural disasters, industrial/transport incidents, deaths or casualties, major political events \
(resignations, unrest, sudden policy action) inside India.
  * West Asia Desk: breaking regional incidents (attacks, strikes, political \
upheaval, protests, sudden military movements) beyond whatever is already tracked in ongoing \
conflict threads.
  * Maritime & Energy Desk: tanker/vessel incidents, port or refinery accidents, \
pipeline disruptions, shipping lane closures — not just price/index movements.
  * Markets & Capital Desk: flash crashes, circuit breakers, emergency central bank \
action, major unscheduled earnings or guidance shocks.
  * Real Estate & Infrastructure Desk: building collapses, major project \
cancellations/approvals, construction accidents.
  * Sports Desk: breaking results, serious injuries, disciplinary or scandal news.
  * Global Politics Desk: breaking political events — resignations, elections, \
coups, sudden policy reversals — beyond scheduled/expected developments.
  * Trends & Forecasting Desk: breaking data releases or reports that shift an \
existing forecast, if any surface.
An acute incident with real-world impact (injuries, fatalities, market/operational disruption) is \
newsworthy on its own and belongs on its desk even without further analytical framing — do not \
skip a desk's incident pass just because other desks already have enough material for this run.
- Each Notion entry body must include full "What Happened" and "Why It Matters" sections \
with real figures, attributions, and analysis — not a one-line summary.
- Subject lines and headers must use proper case ("Navvya Signal - Daily Briefing"), never \
all-caps.
- If nothing meaningful changed since the last run, it is correct to return zero entries \
for that section rather than padding with a no-op update. This is a per-STORY judgment, not \
a per-DESK one: a desk having already published an entry earlier today does NOT mean that \
desk is "done" for the day. Run the full roundup and incident searches for every desk on every \
run regardless of what that desk already covered — if they surface a genuinely distinct new \
development (different topic, different specific event), it gets its own entry even if the \
same desk already has other entries today. Only skip when the search results contain nothing \
that is both new (within the recency window) and distinct from what's already covered.
- Never carry forward a stale/outdated figure without flagging or correcting it.
- RECENCY RULE — applies regardless of how long it has been since the last successful run: \
only include developments from the last 24-48 hours relative to the "Current UTC time" given \
below. Do NOT sweep in or summarize older backlog just because it turned up in search or \
because there was a gap since the last run (e.g. an outage). If the pipeline missed several \
days, this run covers only the most recent 24-48 hours of developments — it does not attempt \
to catch subscribers up on everything that happened in between. Discard any search result \
outside that window rather than including it, even if it seems significant. Use the specific \
dates in search results to judge this, not vague recency language in the source itself.
- Background context (older than 48h) may be referenced briefly, in a single clause, ONLY to \
explain why a fresh-in-window development matters (e.g. "...continuing the six-session Brent \
retreat that began August 20") — it must never be the subject of its own entry or paragraph.

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


def generate_briefing(existing_entries, scope_desks):
    scope_line = "Desks in scope for this run: " + ", ".join(scope_desks)
    user_prompt = f"""Run type: {RUN_TYPE}
{scope_line}
Current UTC time: {datetime.datetime.utcnow().isoformat()}Z

Existing recent Signal Feed entries (id | title | desk) for dedup reference:
{json.dumps(existing_entries, indent=2)}

Research today's developments for ONLY the desks listed above and produce the JSON output \
per your instructions."""

    with client.messages.stream(
        model="claude-sonnet-4-5",
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

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


COMPILE_SYSTEM_PROMPT = """You are the compilation editor for NavvyaSignal, a daily intelligence \
publication. You do NOT research or write new facts — you assemble the final daily email and \
WhatsApp send from already-researched, already-validated entries provided to you below. Every \
fact in the provided entries has already been fact-checked; do not add, remove, or alter any \
factual claim, figure, or attribution — only format and organize.

Rules:
- Group entries by desk in this order where present: West Asia Desk, Maritime & Energy Desk, \
Markets & Capital Desk, India Desk, UAE Desk, Real Estate & Infrastructure Desk, Sports Desk, \
Trends & Forecasting Desk, Global Politics Desk.
- Subject lines and headers use proper case ("Navvya Signal - Daily Briefing"), never all-caps.
- email_html: full HTML body, clean sections per desk, using the provided title/body/sources \
for each entry verbatim (light formatting only — do not rewrite the prose).
- whatsapp_text: staccato style summarizing the day's key entries, no markdown, ends with a \
navvyasignal.com invite.
- If the provided entries list is empty, still produce a short, honest edition noting that no \
qualifying developments were found across desks today, rather than fabricating content.
- editor_note: 1-2 sentences noting anything worth flagging (e.g. a desk with no update today), \
or empty string.

Output ONLY valid JSON matching this schema — no preamble, no markdown code fences:
{
  "edition_label": "string, e.g. '2026-08-01, 18:30 GST edition'",
  "editor_note": "string, or empty string",
  "email_subject": "string",
  "email_html": "string, full HTML body for the email",
  "whatsapp_text": "string, staccato style, no markdown, ends with navvyasignal.com invite"
}
"""


def compile_briefing(todays_entries):
    user_prompt = f"""Current UTC time: {datetime.datetime.utcnow().isoformat()}Z

Today's researched entries to compile into the daily send:
{json.dumps(todays_entries, indent=2)}

Assemble the final email and WhatsApp content per your instructions."""

    with client.messages.stream(
        model="claude-sonnet-4-5",
        max_tokens=16000,
        system=COMPILE_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    ) as stream:
        response = stream.get_final_message()

    text_parts = [block.text for block in response.content if block.type == "text"]
    full_text = "\n".join(text_parts).strip()

    import re
    fence_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", full_text, re.DOTALL)
    if fence_match:
        json_str = fence_match.group(1)
    else:
        start = full_text.find("{")
        end = full_text.rfind("}")
        if start == -1 or end == -1 or end < start:
            fail_hard(f"Could not locate a JSON object in compile output.\nRaw output:\n{full_text[:2000]}")
        json_str = full_text[start:end + 1]

    try:
        data = json.loads(json_str)
    except json.JSONDecodeError as e:
        fail_hard(f"Compile output was not valid JSON: {e}\nExtracted text:\n{json_str[:2000]}")

    for k in ["edition_label", "email_subject", "email_html", "whatsapp_text"]:
        if k not in data:
            fail_hard(f"Compile output missing required key: {k}")

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


def claude_respond_to_flags(briefing_data, gemini_flags_text, is_repeat_concern=False):
    """Ask Claude to address Gemini's specific concerns: confirm with better sourcing,
    revise, or explain — using web search to re-check if needed."""
    repeat_warning = ""
    if is_repeat_concern:
        repeat_warning = """
IMPORTANT: this same concern (or a closely related one) was already raised in a previous \
round and your prior response did not resolve it — Gemini is flagging it again. Do NOT \
reconfirm the claim as accurate a second time unless you can name one specific, checkable \
source (outlet + article) you searched THIS round that directly supports it. A vague or \
blanket claim of confirmation ("confirmed via multiple sources") without a specific, named \
source is not acceptable and must not be used. If you cannot produce a specific source this \
round, you MUST either revise the claim to remove the disputed specific detail entirely, or \
drop that sentence/clause — do not restate it with invented-sounding verification language.
"""

    prompt = f"""Gemini raised the following concerns about your draft briefing:

{gemini_flags_text}
{repeat_warning}
For each concern, either:
1. Re-verify via web search and confirm the claim stands — ONLY if you can cite a specific, \
named source (outlet + article) found in an actual search this round, not a general assertion \
of confidence, or
2. Revise the specific claim to be accurate, or
3. If genuinely uncertain after re-checking, soften the claim with appropriate hedging \
language (e.g. "single-source, unconfirmed" or "disputed") rather than stating it flatly \
or dropping it — per NavvyaSignal's credibility protocol of labeled inference over fabrication.

Never fabricate or imply verification you did not actually perform this round. If you did not \
run a new search for a specific claim, you may not describe it as "confirmed."

Current draft JSON:
{json.dumps(briefing_data)}

Output the FULL corrected JSON (same schema as before), with fixes applied. Output ONLY \
the JSON, no other text."""

    with client.messages.stream(
        model="claude-sonnet-4-5",
        max_tokens=32000,
        system=SYSTEM_PROMPT,
        tools=[{"type": "web_search_20250305", "name": "web_search"}],
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        response = stream.get_final_message()
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


def _flag_lines(review_text):
    """Extract just the '- ' concern lines from a Gemini review, for repeat-detection."""
    return [l.strip().lower() for l in review_text.splitlines() if l.strip().startswith("-")]


def _concern_overlaps(prev_flags, current_flags, threshold=0.5):
    """Rough repeat-detection: does a current flag share enough words with any previous
    flag to be considered 'the same concern raised again'? Word-overlap is crude but
    good enough to catch a Gemini re-flag of the same underlying issue."""
    for cur in current_flags:
        cur_words = set(w for w in cur.split() if len(w) > 4)
        for prev in prev_flags:
            prev_words = set(w for w in prev.split() if len(w) > 4)
            if not cur_words or not prev_words:
                continue
            overlap = len(cur_words & prev_words) / min(len(cur_words), len(prev_words))
            if overlap >= threshold:
                return True
    return False


def verify_with_gemini_loop(briefing_data, max_rounds=2):
    """Cross-verification loop: Gemini reviews, Claude responds to flags, Gemini re-reviews.
    If the SAME concern persists across rounds, Claude is forced to hedge/strip rather than
    reconfirm. On the final round, any remaining concern is treated as unresolved and forced
    into a hedge/strip response rather than shipped as flatly stated fact."""
    prev_flags = []
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
        current_flags = _flag_lines(review)
        is_repeat = _concern_overlaps(prev_flags, current_flags)
        is_final_round = round_num == max_rounds
        force_hedge = is_repeat or is_final_round
        if is_repeat:
            log("WARNING: at least one concern appears to be a repeat from the prior round — "
                "forcing hedge/strip instead of allowing reconfirmation.")
        if is_final_round and flags_count > 0:
            log("Final verification round still has open concerns — forcing hedge/strip "
                "rather than shipping the disputed claim(s) as flatly stated.")
        briefing_data = claude_respond_to_flags(briefing_data, review, is_repeat_concern=force_hedge)
        prev_flags = current_flags

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
    log(f"DEBUG: Whapi payload length = {len(text)} chars. First 150 chars: {text[:150]!r}")
    log(f"DEBUG: Whapi payload last 150 chars: {text[-150:]!r}")
    resp = requests.post(url, headers=headers, json=payload, timeout=30)
    log(f"DEBUG: Whapi raw response status={resp.status_code} body={resp.text}")
    if resp.status_code != 200 or not resp.json().get("sent"):
        fail_hard(f"Whapi send failed: {resp.status_code} {resp.text}")
    log("Whapi message sent successfully.")


# ---------- MAIN ----------

def main():
    log(f"Starting NavvyaSignal automated run (type={RUN_TYPE})")

    required = {
        "ANTHROPIC_API_KEY": ANTHROPIC_API_KEY,
        "NOTION_API_KEY": NOTION_API_KEY,
        "NOTION_DATABASE_ID": NOTION_DATABASE_ID,
        "KIT_API_KEY": KIT_API_KEY,
        "GEMINI_API_KEY": GEMINI_API_KEY,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        fail_hard(f"Missing required secret(s): {', '.join(missing)}. Check GitHub Actions secrets.")

    if RUN_TYPE in GROUPS:
        return run_group(RUN_TYPE)
    elif RUN_TYPE == "compile_send":
        return run_compile_send()
    else:
        fail_hard(f"Unrecognized RUN_TYPE '{RUN_TYPE}' — expected one of {list(GROUPS.keys())} "
                   f"or 'compile_send'.")


def run_group(run_type):
    """Research + Notion push for exactly this group's 2-3 desks. Never sends email/WhatsApp,
    regardless of SEND_OUTPUT — sending only ever happens from compile_send, once per day,
    after all groups have run."""
    scope_desks = GROUPS[run_type]
    log(f"Group run scoped to: {', '.join(scope_desks)}")

    existing = fetch_existing_entries()
    log(f"Fetched {len(existing)} existing Notion entries for dedup reference.")

    briefing = generate_briefing(existing, scope_desks)
    log(f"Generated briefing: {briefing['edition_label']}, {len(briefing['notion_entries'])} entries")

    # Code-level enforcement of scope, not just prompt compliance — if the model slips and
    # returns an entry for a desk outside this group, drop it here rather than letting it push
    # to Notion from the wrong run (a later run for that desk's own group will cover it properly).
    in_scope_entries = []
    for entry in briefing["notion_entries"]:
        if entry.get("desk") in scope_desks:
            in_scope_entries.append(entry)
        else:
            log(f"WARNING: dropping out-of-scope entry '{entry.get('title')}' for desk "
                f"'{entry.get('desk')}' — not in this run's scope ({', '.join(scope_desks)}).")
    briefing["notion_entries"] = in_scope_entries

    briefing = verify_with_gemini_loop(briefing)

    valid_existing_ids = {e["id"] for e in existing}
    notion_summary = push_to_notion(briefing["notion_entries"], valid_existing_ids)

    log("Group run complete (no send — compile_send handles that later today). Summary:")
    for line in notion_summary:
        log(f"  {line}")

    return {
        "edition_label": briefing["edition_label"],
        "entry_count": len(briefing["notion_entries"]),
        "notion_summary": notion_summary,
        "sent_output": False,
    }


def run_compile_send():
    """Compile today's already-researched entries (from group_a/b/c) into the single daily
    email + WhatsApp send. Does no research of its own — if the groups found nothing, this
    step has nothing new to say either, by design."""
    todays_entries = fetch_todays_entries_for_compile()
    log(f"Fetched {len(todays_entries)} entries from today's group runs to compile.")

    if not todays_entries:
        log("WARNING: no entries found from today's group runs within the compile window — "
            "this likely means one or more group runs failed or didn't produce anything. "
            "Proceeding with an honest 'quiet day' edition rather than failing silently.")

    briefing = compile_briefing(todays_entries)
    log(f"Compiled: {briefing['edition_label']}")

    broadcast_id = send_kit(briefing["email_subject"], briefing["email_html"])
    verify_kit_sent(broadcast_id)
    send_whapi(briefing["whatsapp_text"])

    log("Compile & send complete.")

    return {
        "edition_label": briefing["edition_label"],
        "entry_count": len(todays_entries),
        "notion_summary": [f"{e['title']} ({e['desk']})" for e in todays_entries],
        "sent_output": True,
    }


def send_ops_notification(text):
    """Best-effort WhatsApp DM to the operator with a run status update.
    Never raises — a notification failure must not mask the real run result."""
    if not OPS_NOTIFY_NUMBER or not WHAPI_TOKEN:
        log("WARNING: OPS_NOTIFY_NUMBER or WHAPI_TOKEN not configured — skipping ops notification.")
        return
    try:
        digits = "".join(ch for ch in OPS_NOTIFY_NUMBER if ch.isdigit())
        url = "https://gate.whapi.cloud/messages/text"
        headers = {"Authorization": f"Bearer {WHAPI_TOKEN}", "Content-Type": "application/json"}
        payload = {"to": f"{digits}@s.whatsapp.net", "body": text}
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        if resp.status_code != 200 or not resp.json().get("sent"):
            log(f"WARNING: Ops notification failed to send: {resp.status_code} {resp.text}")
        else:
            log("Ops notification sent.")
    except Exception as e:
        log(f"WARNING: Ops notification raised an exception (ignored): {e}")


if __name__ == "__main__":
    try:
        result = main()
        notion_lines = "\n".join(f"- {line}" for line in result["notion_summary"]) or "(no changes)"
        send_ops_notification(
            f"✅ NavvyaSignal run OK\n"
            f"Type: {RUN_TYPE} | Sent email/WhatsApp: {result['sent_output']}\n"
            f"{result['edition_label']} — {result['entry_count']} entries\n"
            f"{notion_lines}"
        )
    except SystemExit:
        # fail_hard() already logged a FATAL line above this.
        send_ops_notification(f"❌ NavvyaSignal run FAILED (type={RUN_TYPE})\nSee GitHub Actions log for the FATAL line.")
        raise
    except Exception as e:
        send_ops_notification(f"❌ NavvyaSignal run CRASHED (type={RUN_TYPE})\n{type(e).__name__}: {e}")
        raise
