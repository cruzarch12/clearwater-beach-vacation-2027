# Clearwater Beach 2027 — Rental Map

A self-contained interactive map of candidate vacation rentals, with prices
kept fresh by a scheduled GitHub Action instead of hand-editing the file.

## Setup (one time)

1. **Create a GitHub account** (skip if you already have one): https://github.com/signup

2. **Create a new repository**
   - Click the **+** in the top-right of github.com → **New repository**
   - Name it anything, e.g. `clearwater-2027` — it can be **Public** (GitHub
     Pages' free tier requires a public repo unless you're on a paid plan)
   - Leave "Add a README" unchecked, click **Create repository**

3. **Upload these files**, keeping the folder structure intact
   - On the new repo's page, click **Add file → Upload files**
   - Drag in `index.html`, the `data` folder, the `scripts` folder, and the
     `.github` folder all at once (GitHub preserves folder structure on
     upload)
   - Scroll down, click **Commit changes**

4. **Turn on GitHub Pages**
   - In the repo, go to **Settings → Pages** (left sidebar)
   - Under **Build and deployment → Source**, choose **Deploy from a
     branch**
   - Under **Branch**, choose **main** and folder **/ (root)**, click
     **Save**
   - Wait ~1 minute, then refresh the page — GitHub shows your live link at
     the top: `https://<your-username>.github.io/<repo-name>/`

5. **Run the price check once manually** (so prices are fresh before anyone
   opens the link, instead of waiting for the first scheduled run)
   - Go to the **Actions** tab → **Refresh rental prices** (left sidebar)
   - Click **Run workflow** → **Run workflow** (green button)
   - Wait 2–5 minutes for it to finish (green checkmark)

6. **Test the link yourself** — open the `github.io` URL from step 4 on
   your own phone and laptop before sending it to anyone.

That's it — the Action now runs automatically every 3 hours from here on,
with no further action needed from you. See the comments in
`.github/workflows/refresh-prices.yml` if you want to change the schedule.

### A real limitation worth knowing about

Airbnb, Vrbo, Booking.com, and similar sites actively try to detect and
block automated browsing — that's true of any unofficial checker, not
just this one. In practice:

- **Direct-booking sites (like Sunny Orange Stays)** and **Airbnb** tend to
  refresh reliably.
- **Vrbo, Booking.com, and Vista Del Mar's Guesty booking page** block the
  automated check more often than not. When that happens, the script
  doesn't guess — it keeps the last price it was able to read and flags
  that platform as blocked for that run.

When a specific row was affected by this on the most recent refresh, that
row itself (not a banner at the top) shows **"*** Click 'View' for live
price"** in red next to the platform name, so it's always obvious which
number to double-check. This is a structural limit of those
sites, not something a script fix can fully solve — the honest fallback is
always the listing link itself.

To cut down on how often that happens, `scripts/scrape_prices.py` runs
every page through [`playwright-stealth`](https://pypi.org/project/playwright-stealth/),
a free library that hides the usual tells of an automated browser
(`navigator.webdriver`, headless-only JavaScript properties, the
headless user-agent string, and a handful of others these sites check
for). It's not a guarantee against something as defended as Vrbo's
Akamai protection — nothing free is — but it's a real improvement over
an untouched headless browser, which is what most of these sites flag
almost immediately. No setup needed; it's already wired into the
scheduled check.

### Does the page update itself, or does someone need to refresh it?

It updates itself — there's no button to click. The page quietly re-fetches
`data/prices.json` as soon as it's opened, and then again every 15 minutes
for as long as that browser tab stays open, so anyone who leaves it up
all day still catches each of the GitHub Action's 3-hour price checks
without doing anything. The "Prices last refreshed" line always shows
exactly how fresh the numbers on screen are.

This does **not** make the page trigger a brand-new scrape on demand — it
only re-reads whatever `data/prices.json` the scheduled GitHub Action most
recently committed. That's a deliberate choice, not a missing feature:
making the page fire a new GitHub Actions run itself would require an
authenticated request to GitHub's API, and the only way to authenticate
from inside a public page's JavaScript is to put a secret token in the
page itself — which anyone opening the link or its page source could then
read and use. The real checking happens safely on its own schedule, off to
the side, with no exposed credentials. If you ever want a true on-demand
trigger, the safe way is a small serverless proxy (e.g. a free Cloudflare
Worker) that holds the token server-side instead — that's extra
infrastructure to maintain, so it's worth asking for only if the 3-hour
schedule genuinely isn't often enough.

## Collecting everyone's picks

The page lets each person **Select** a property + date range, then confirm
with their name, adults, and children (newborn–17) before it's recorded.
Submissions need somewhere free to land — this uses a tiny Google Apps
Script "web app" that writes one row per submission straight into a Google
Sheet you own. One-time setup:

1. **Create a new Google Sheet** (sheets.new) — name it anything, e.g.
   "Clearwater 2027 Responses."
2. In the Sheet, go to **Extensions → Apps Script**. Delete whatever's in
   the editor and paste in the full contents of `apps-script/collect-responses.gs`
   from this folder.
3. Click **Deploy → New deployment**. Next to "Select type," click the gear
   icon and choose **Web app**.
4. Set **Execute as: Me**, and **Who has access: Anyone**, then click
   **Deploy**. (Google will ask you to authorize the script — that's
   expected, it's your own script running on your own Sheet.)
5. Copy the **Web app URL** it gives you (ends in `/exec`).
6. In `index.html`, find the line:
   `const RESPONSES_URL = "PASTE_YOUR_GOOGLE_APPS_SCRIPT_WEB_APP_URL_HERE";`
   and replace the placeholder with that URL (keep the quotes), then
   re-upload/commit `index.html` to GitHub as usual.

That's it — every confirmed selection now appears as a new row in your
Sheet's "Responses" tab in real time, ready to filter, sort, or pivot. This
is entirely free (well within Google's free quota for something this
small) and needs no server of your own.

**Heads up on privacy:** the "Anyone" access setting means the web app URL
itself could accept a POST from anywhere if someone had that exact link —
it's not protected by a login. That's a reasonable tradeoff for a link
you're only sharing with family for a trip poll, but don't publish the
Apps Script URL anywhere public.

## Sending it to people

Just send the `github.io` link — text message, email, whatever. It opens
directly in any phone or desktop browser, no app or download required.

## Making future edits (add/remove a property, fix an address, etc.)

Edit `index.html`'s `properties` array directly in GitHub (click the file →
pencil icon → edit → **Commit changes**), or come back here and ask me to
make the change and re-send the file for you to re-upload.
