# NavvyaSignal Daily Briefing — Automated Pipeline

This replaces the Claude Dispatch scheduler entirely. It runs on GitHub's servers
(via GitHub Actions), not your laptop — so it works even if your computer is off.

## What it does, automatically, 3x/day (00:00, 12:00, 18:00 GST):
1. Researches current developments via Claude + web search
2. Checks existing Notion Signal Feed entries and decides update-vs-new
3. Pushes entries to Notion with "Ready to Post" = YES (goes live on Framer automatically)
4. Sends the email broadcast via Kit
5. Sends the WhatsApp Channel post via Whapi

No approval step — this is fully automatic per your instruction. If anything errors,
the run fails and logs the error in GitHub Actions rather than sending broken/partial content.

## One-time setup (15-20 minutes)

### 1. Create the repository
- Go to github.com → New repository → name it e.g. `navvyasignal-automation` → set to **Private** → Create.

### 2. Upload these files
Upload `main.py`, `requirements.txt`, and the `.github/workflows/daily-briefing.yml`
file (keep the folder structure — `.github/workflows/` must stay nested exactly like that).
Easiest way: use GitHub's web "Add file → Upload files" for main.py and requirements.txt,
then separately create the `.github/workflows/daily-briefing.yml` file via "Add file → Create new file"
and paste in the workflow content, typing the full path `.github/workflows/daily-briefing.yml`
as the filename (GitHub will auto-create the folders).

### 3. Add your API keys as GitHub Secrets
Go to your repo → **Settings → Secrets and variables → Actions → New repository secret**.
Add each of these one at a time:

| Secret name | Value |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key (from console.anthropic.com) |
| `NOTION_API_KEY` | Your Notion integration token |
| `NOTION_DATABASE_ID` | Your Signal Feed database ID |
| `KIT_API_KEY` | Your Kit V4 API key |
| `KIT_FROM_EMAIL` | e.g. hello@navvyasignal.com |
| `WHAPI_TOKEN` | Your Whapi.Cloud token |
| `WHAPI_CHANNEL_ID` | Your WhatsApp channel ID, e.g. 120363429569627256@newsletter |

**Important:** you've shared several of these keys in plaintext during testing today —
regenerate all of them fresh before adding here, so the ones in this pipeline are
ones that have never been exposed anywhere else.

### 4. Test it manually before trusting the schedule
Go to your repo → **Actions** tab → click "NavvyaSignal Daily Briefing" → **Run workflow**
(this uses the `workflow_dispatch` trigger, so you can test anytime without waiting for
the schedule). Watch the log output — it will show exactly what happened at each step.

### 5. Let it run
Once a manual test succeeds cleanly, the three daily schedules will fire automatically
from that point on — no further action needed.

## Notes on the "GST" schedule times
GitHub Actions cron only runs in UTC. GST is UTC+4 with no daylight saving, so the
UTC times in the workflow file (20:00, 08:00, 14:00 UTC) will always correctly
correspond to 00:00, 12:00, and 18:00 GST — no seasonal adjustment ever needed.

## Notion API note
`main.py` currently pushes body content as a single paragraph block. If you want
richer formatting (headers, bullet lists) in the Notion page body rather than one
paragraph, this can be extended — flag it and it can be expanded to use Notion's
full block types (headings, bulleted_list_item, etc.) instead of a single paragraph block.

## If something needs changing later
Just edit `main.py` (the `SYSTEM_PROMPT` string controls almost all the editorial
behavior — desk rules, formatting, dedup logic) directly in GitHub's web editor,
commit the change, and the next scheduled run will use the updated version automatically.
