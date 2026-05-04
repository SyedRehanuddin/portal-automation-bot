# University Portal Automation

This project logs into a university portal with Selenium, lets you solve CAPTCHA manually when needed, saves cookies, reuses the session, checks attendance/marks/memo pages every 10-15 minutes, and sends Telegram alerts only when something changes.

Because every university portal uses different URLs and HTML selectors, you must fill in `config.json` with your portal's real URLs and CSS selectors before running.

## Files

- `portal_automation/main.py` - starts the monitor
- `portal_automation/browser.py` - Selenium login, cookies, session refresh
- `portal_automation/extractors.py` - attendance, marks, memo extraction
- `portal_automation/notifier.py` - Telegram notifications using `requests`
- `portal_automation/storage.py` - local JSON state
- `config.example.json` - copy to `config.json` and edit
- `.env.example` - copy to `.env` and edit
- `run.bat` - Windows launcher

## Windows Setup

1. Install Python 3.10 or newer from <https://www.python.org/downloads/windows/>.
2. Open PowerShell in this folder:

   ```powershell
   cd "C:\Users\HASSAN\Documents\New project\portal_automation"
   ```

3. Create and activate a virtual environment:

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```

4. Install dependencies:

   ```powershell
   pip install -r requirements.txt
   ```

5. Create your environment file:

   ```powershell
   copy .env.example .env
   ```

6. Edit `.env`:

   ```env
   ENROLLMENT_NUMBER=your_enrollment_number
   PORTAL_PASSWORD=your_password
   TELEGRAM_BOT_TOKEN=your_telegram_bot_token
   TELEGRAM_CHAT_ID=your_telegram_chat_id
   ```

7. A starter `config.json` is already included for SRAAP:

   ```powershell
   notepad config.json
   ```

8. Confirm or update these logged-in page URLs after your first login:

   - `attendance_url`
   - `marks_url`
   - `memo_url`

   If those URLs are wrong, the script will also scan the logged-in menu for matching `link_keywords` such as `attendance`, `cie`, `ete`, `marks`, `memo`, and `result`.

   The login selectors are already set for `https://sraap.in/student_login.php`:

   - Enrollment: `#user_id`
   - Password: `#user_password`
   - CAPTCHA: `#token`
   - Submit: `button[name='submit']`

## Telegram Setup

1. Open Telegram and message `@BotFather`.
2. Run `/newbot` and copy the bot token into `.env`.
3. Send one message to your new bot.
4. Visit this URL in a browser, replacing the token:

   ```text
   https://api.telegram.org/botYOUR_TOKEN/getUpdates
   ```

5. Find your numeric `chat.id` and put it in `.env` as `TELEGRAM_CHAT_ID`.

## First Run

Run:

```powershell
python -m portal_automation.main --config config.json
```

On the first login, Chrome opens. Type the CAPTCHA manually into the CAPTCHA field. The script will click Sign in after the CAPTCHA has text, or you can click Sign in yourself. After login succeeds, cookies are saved in `data/cookies.json`.

The script then checks:

- Attendance
- Marks
- Semester memo page or PDF link availability

Previous data is stored in `data/portal_state.json`. Telegram alerts are sent only when the new data differs from the previous data.

## Run With Windows Batch File

Double-click `run.bat`, or run:

```powershell
.\run.bat
```

## Run In Background On Windows

From PowerShell:

```powershell
Start-Process -WindowStyle Hidden -FilePath ".\run.bat"
```

To stop it later, use Task Manager and end the Python process.

## Telegram Bot Server

Run this on your PC or server to keep one Telegram bot process alive. It responds to commands from saved data and supports manual portal checks from Telegram.

Install dependencies:

```powershell
pip install -r requirements.txt
```

Start the bot:

```powershell
.\.venv\Scripts\python.exe -m portal_automation.telegram_bot --config config.json
```

Then message your Telegram bot:

```text
/attendance
/marks
/memo
/total
/last3
/all
/check
/help
```

`/check` logs into SRAAP immediately with Selenium. Other commands reply from the latest saved JSON and do not log into the portal unless they need timetable data.

Background monitoring is off by default. To enable it, set `ENABLE_BACKGROUND_MONITOR=true` or add `"background_enabled": true` inside the `monitoring` section of `config.json`.

## Render Web Service Deployment

This project can run on Render's free Web Service tier using Telegram webhooks. Render runs the bot in the cloud, and you use Telegram from your phone normally.

The Render entrypoint is:

```text
portal_automation.webhook_server:app
```

The included `Dockerfile` installs Chromium and starts:

```bash
uvicorn portal_automation.webhook_server:app --host 0.0.0.0 --port $PORT
```

Set these Render environment variables:

```env
ENROLLMENT_NUMBER=your_enrollment_number
PORTAL_PASSWORD=your_portal_password
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
TELEGRAM_WEBHOOK_SECRET=any_random_secret
CONFIG_PATH=config.json
SELENIUM_HEADLESS=true
SELENIUM_CLOUD=true
TZ=Asia/Kolkata
TIMETABLE_DISABLE_CACHE=true
```

Optional variables:

```env
TELEGRAM_WEBHOOK_URL=https://your-service.onrender.com/telegram/webhook
TELEGRAM_WEBHOOK_PATH=/telegram/webhook
PORTAL_COOKIES_JSON=[{"name":"example","value":"example"}]
ENABLE_BACKGROUND_MONITOR=false
```

If `TELEGRAM_WEBHOOK_URL` is not set, the app uses Render's `RENDER_EXTERNAL_URL` and appends `/telegram/webhook`.

Important CAPTCHA note: Render runs Chrome headless, so it cannot show the manual CAPTCHA browser. Run the bot locally once, complete CAPTCHA, then use the saved `data/cookies.json` content as `PORTAL_COOKIES_JSON` in Render if your portal session requires cookies. If cookies expire, refresh them locally and update the Render environment variable.

## Optional: Task Scheduler

1. Open Windows Task Scheduler.
2. Create Basic Task.
3. Trigger: "When I log on".
4. Action: "Start a program".
5. Program/script:

   ```text
   C:\Users\HASSAN\Documents\New project\portal_automation\run.bat
   ```

6. Finish.

## How To Find CSS Selectors

1. Open your portal in Chrome.
2. Right-click the enrollment input and choose Inspect.
3. Look for an `id`, `name`, or stable class.
4. Prefer selectors like:

   ```text
   #enrollmentNo
   input[name='enrollment']
   button[type='submit']
   ```

5. Update `config.json`.

## Notes

- CAPTCHA is intentionally manual. The script waits up to `manual_captcha_timeout_seconds`.
- If cookies expire, the script automatically falls back to login again.
- If your portal changes its HTML, update selectors in `config.json`.
- The extraction code is generic. For cleaner subject-wise messages, tune the table selectors to match your portal tables.
