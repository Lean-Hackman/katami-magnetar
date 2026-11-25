# Job Application Assistant

A modular Python assistant based on `struct.md`: it discovers jobs from public ATS
boards, drafts a cover letter from candidate-provided facts, tracks applications,
and assists with a form in a visible persistent Chromium profile. The candidate
reviews the filled form and approves the final submission click.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
cp candidate_profile.example.json candidate_profile.json
cp boards.example.json boards.json
```

Edit `candidate_profile.json`, including a valid resume PDF path. Keep the profile
and generated local data private; they are ignored by Git. Set `OPENAI_API_KEY`
and choose any model ID available to your API account using `OPENAI_MODEL` or
`--model`. There is no pinned model or built-in model allowlist. Candidate
experience and job context are sent to the configured model for LLM features;
without an API key, cover letters use a local factual template and the browser
agent is disabled. Browser-use telemetry defaults to off, and its default browser
extensions are disabled by the application.

The app’s model calls use the OpenAI API and require `OPENAI_API_KEY`. OpenAI’s
ChatGPT/Codex device-code login is a separate authentication surface; it does not
turn a ChatGPT session into an API key for `AsyncOpenAI` or browser-use. You can
start the official Codex device flow from this project:

```bash
job-agent auth --device-code
```

This delegates to `codex login --device-auth`, which prints the OpenAI verification
URL and one-time code and stores the Codex session in its normal credential store.
Use the signed-in session with `job-agent run --auth codex "your task"`.
The API backend and browser campaigns still use `OPENAI_API_KEY`.

For Telegram, set `TELEGRAM_CHAT_ID` to your private chat ID and set
`TELEGRAM_TASK_BOT_TOKEN` (or reuse `TELEGRAM_BOT_TOKEN` if the approval
poller is stopped). Start `job-agent telegram --auth codex` for device login or
`job-agent telegram --auth api` for the configured API endpoint. Send `/model`
to list the provider's model IDs. Send `/model MODEL_ID` to select one, then
send your task as plain text. API mode needs a selection or `OPENAI_MODEL`.
Use a separate task bot if running Telegram application approvals concurrently.

## Commands

Scrape job listings from configured Greenhouse, Lever, and Ashby boards:

```bash
job-agent scrape --boards boards.json
```

Prepare a factual cover-letter PDF:

```bash
job-agent prepare --company "Example Co" --role "Python Engineer" \
  --description "Build Python services and APIs." --model "your-model-id"
```

Open a visible browser-use agent using a dedicated persistent profile and fill
recognizable, blank fields on the current form:

```bash
job-agent apply --url "https://jobs.example/apply" \
  --company "Example Co" --role "Python Engineer"
```

By default, the assistant launches a dedicated persistent Chromium profile.
Known blank contact fields are filled locally through CDP using exact DOM labels
or autocomplete attributes. Existing values, hidden inputs, and ambiguous fields
are left alone. Custom controls, frames, and unmatched fields are handled by the
browser agent. Uploads use browser-use's built-in permitted-file mechanism.

The agent uses DOM text and accessibility data first. Vision is on demand
(`use_vision="auto"`, low detail); the prompt requests screenshots only after DOM
inspection is insufficient. This fallback ordering is a model instruction, not a
hardcoded retry ladder. Local field filling and ATS JSON parsing use no model tokens;
DOM observations, candidate facts, drafts, and requested images still do.

Ordinary navigation, dropdowns, and Next/Continue clicks run automatically. Before
final submission, terminal or Telegram approval permits one click on the specific
pending target. Rejection stops the session. Explicit final-submit labels are
checked in code; unusually labeled controls also rely on the agent understanding
the page and marking the click as final. If the effect remains uncertain, the agent
is instructed to request review. Review the page and approval screenshot. To
attach to a visible Chrome instance you started yourself, pass
`--cdp-url http://localhost:9222`. For example, on macOS:

```bash
open -na "Google Chrome" --args --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.job-agent/chrome-profile"
```

CDP connects to your browser and runs the local DOM field-filling pass.

Authentication challenges may require you to sign in yourself and rerun the
command. There are no routine start/end confirmation prompts. The assistant does
not create accounts or fetch verification codes.

For queued jobs, use `job-agent batch --limit 5 --delay 30`. Jobs run serially.
Optional Telegram review uses `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`; approving
a review authorizes the browser to click the pending final submission target.

An approved submission attempt is tracked as `REVIEWED`, including uncertain
outcomes, to avoid automatic duplicate submissions. Verify the employer's receipt;
this status does not claim confirmed delivery. Rejected applications are `SKIPPED`;
sessions that never reach an approved submission are `FAILED` and can be retried.
Batch mode excludes `REVIEWED`, `SUBMITTED`, and `SKIPPED` jobs. Explicit `apply`
commands can reopen a job after you verify its status.

Check tracked state:

```bash
job-agent status
```

## Modules

- `campaign.py`: runs general DOM-first campaigns with configurable approval.
- `skills.py`: loads reusable workflow knowledge and records safe observations.
- `form_fill.py`: fills unambiguous blank contact inputs directly through CDP.
- `profile.py`: loads local candidate facts and resolves known form-field values.
- `scraper.py`: fetches public job postings from supported ATS board APIs.
- `documents.py`: creates a PDF draft grounded only in supplied profile facts.
- `browser.py`: runs the browser-use agent in a visible persistent browser with
  guarded click/navigation actions and a pre-submit approval checkpoint.
- `tracker.py`: stores application state in a local SQLite database.
- `cli.py`: command-line interface.

Run tests with `python -m unittest discover -s tests`.

## General browser campaigns

Use `job-agent run "Find the latest Python release and cite the release notes"`
for a native-first task. Set `OPENAI_API_KEY` and `OPENAI_MODEL` to use a Responses
API model supporting hosted web search. Add `--file notes.txt` to permit reading
a UTF-8 document (64 KiB maximum) or `--browser` to permit one interactive campaign.
Files supplied for reading are sent to the model when requested.

The browser engine can also run tasks outside job applications. Campaigns use
structured DOM and accessibility data first, with low-detail vision available on
demand. Approval can be disabled, required for the final action, or required for
every click.

```bash
job-agent campaign \
  --name "update settings" \
  --url "https://example.com/settings" \
  --instructions "Enable weekly summaries and verify the saved state." \
  --mode general \
  --approval final
```

Use `--file` to permit specific uploads, `--cdp-url` to attach to Chrome, and
`--skill-dir` to load personal workflow skills. Campaign observations store only
the campaign name, mode, hostname, success state, and matched skill names. They
exclude DOM contents, entered values, URL paths, and query strings.

Workflow skills are versioned JSON containing stable host matching, instructions,
field hints, final-action labels, and confirmation markers. They deliberately do
not store click indexes or personal form values.

```bash
job-agent skill save \
  --name "example settings" \
  --mode general \
  --host example.com \
  --instructions "Open Notifications, update the requested values, then verify Saved." \
  --confirmation "saved"

job-agent skill list --skill-dir workflow_skills
```

Job mode automatically loads the built-in `generic-application` skill while
retaining candidate profiles, document generation, tracking, and approval before
the final application submission.
Telegram keeps up to 24 completed exchanges per private chat and sends the newest exchanges that fit a 32,768-character history budget. The latest successful attachment batch stays available for follow-up questions until another batch replaces it or `/new` clears the chat. Both history and attachments are stored in
`output/telegram.sqlite3` (override with `TELEGRAM_HISTORY_DB`). Switch models
with `/model MODEL_ID` and continue the conversation, or send `/new` to clear it.
Campaigns and job runs discover saved skills in the working directory's
`workflow_skills/`. Run receipts count observed web searches, browser sessions,
images included in browser model calls, and screenshots saved for approval.

### Live browser handoff in Telegram

Run the bot and Chromium on the same host. Linux can run the browser headlessly.
Install Chrome or Chromium for browser-use and put the Codex CLI on PATH for
device login mode. Set `CODEX_BIN` to a specific executable if needed.

For a temporary HTTPS endpoint, install and authenticate ngrok on the bot host. The bot starts a loopback-only viewer and an ngrok tunnel for each campaign, then closes both. For a fixed endpoint, set `BROWSER_PUBLIC_URL` to an HTTPS origin you control. Route its `/live/*`
requests, including WebSocket upgrades, to the bot's loopback listener on port
8765. For example, a Caddy site for your domain can include:

```caddy
reverse_proxy /live/* 127.0.0.1:8765
```

Override the listener with `BROWSER_BIND_HOST` and `BROWSER_PORT` if needed.
Start the Telegram bot normally and send `/browse https://example.org your task`.
Tap **Open live browser** to watch, then **Take control** to pause after the
current step. Tap or scroll the live page and use the text field to type.
**Resume agent** hands the same tab back. Leaving the viewer during takeover
keeps the campaign paused until you reconnect and resume. Final-action requests
appear in the viewer and expire after two minutes. Each session link expires
when the campaign ends. One campaign runs at a time per bot process.

Telegram edits a progress message as model tools and browser steps run. When a
browser task ends a run without completing the goal, the tab stays open. Reply
with revised instructions, use the live viewer and resume, or send `/stop`.
An incomplete task's goal, last URL, and latest result are checkpointed in the
chat database. After a bot restart, `/resume` or a text correction reopens the
URL using the browser profile. Unsaved form values require the original live tab.

**Phone takeover.** Tap **Control browser** on the handoff message to open the
live tab inside Telegram. Tap a field, swipe to scroll, use the keyboard to
enter text, then tap **Resume agent**. You can also reply to the handoff in chat.

Application campaigns require newly observed site acknowledgements before
reporting completion. An unspecified batch stays paused until its size is known.
When a supplied résumé is available, job campaigns pass it into the browser and
attempt its upload before manual field entry once the application form is open.
The agent then waits for parsing or autofill and preserves populated values.

### Observe-only workflow learning

After a browser campaign completes successfully, the harness records a compact
candidate path in `output/workflow_hints.sqlite3`. Candidates contain the host,
normalized URL path, campaign mode, semantic action names, and success count.
Queries, entered values, files, screenshots, and manual takeover paths are not
stored. Repeated paths increment the same candidate.

This phase is observation only. Candidates are not added to model prompts and
cannot change browser behavior or latency beyond the local database write at
the end of a successful campaign. Set `WORKFLOW_HINT_DB` to move the database.
