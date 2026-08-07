# Setup

CampOn has two halves: the scanner (this repo, runs on GitHub Actions
every 5 minutes) and the intake (a Google Form + Sheet you set up once,
since a form/spreadsheet isn't something that lives in a git repo).

## 1. Create the Sheet

Create a new Google Sheet with two tabs:

### `Campgrounds` tab (you maintain this by hand)

| state | campground_name | facility_id | url |
|---|---|---|---|
| California | Upper Pines (Yosemite) | 232447 | https://www.recreation.gov/camping/campgrounds/232447 |

`facility_id` is the numeric ID from a campground's recreation.gov URL
(`/camping/campgrounds/<id>`). Add one row per campground you want
selectable. Keep the state names consistent -- they must match the Form's
state dropdown exactly.

### `Signups` tab (the Form writes here)

Columns, in this exact order:

`timestamp | name | email | state | campgrounds | start_date | end_date | alert_mode | unsubscribe_token | active`

The first 8 come from the Form response. The last 2 (`unsubscribe_token`,
`active`) are filled in automatically by the Apps Script below -- leave
them blank when you create the sheet.

## 2. Build the Form

Create a Google Form with these questions, in order:

1. **Name** -- short answer
2. **Email** -- short answer, with response validation set to "Email address"
3. **State** -- multiple choice (one option per state you support). For
   each choice, use "Go to section based on answer" to branch to that
   state's section.
4. **One section per state**, each containing a **checkboxes** question
   listing that state's campgrounds (from the `Campgrounds` tab). Open
   the question's response validation and set "Limit number of
   selections" -> Maximum -> **2**.
5. **Start date** / **End date** -- date questions.
6. **Alert mode** -- multiple choice: "Keep watching after my dates pass
   (rolling)" vs. "Stop automatically after my end date". Make sure the
   option text contains the word "rolling" for the first choice, since
   both the scanner and the Apps Script auto-expire logic key off that
   word.

Link the Form's responses to the Sheet you made in step 1 (Responses tab
-> the green Sheets icon -> "Select existing spreadsheet"). Rename the
resulting response tab to `Signups` and reorder/rename its columns to
match the layout above if Forms didn't lay them out that way.

Note: this confines each signup to campgrounds within a single state. If
someone wants to watch campgrounds in two different states, they submit
the form twice -- the scanner's 2-campground-per-email cap still applies
across both submissions, so this doesn't let anyone exceed the cap.

## 3. Deploy the Apps Script

From the Sheet: **Extensions -> Apps Script**. Paste the contents of
[`apps-script/Code.gs`](apps-script/Code.gs) in as `Code.gs`.

1. **Triggers** (clock icon on the left) -> **Add Trigger** ->
   function `onFormSubmit`, event source "From spreadsheet", event type
   "On form submit".
2. Add a second trigger -> function `expireOldWatches`, event source
   "Time-driven", type "Day timer" -- pick any time.
3. **Deploy -> New deployment -> Web app**. Execute as "Me", who has
   access "Anyone". Copy the deployment URL -- that's your
   `UNSUBSCRIBE_BASE_URL`.

## 4. Publish the two tabs as CSV

For each tab (`Campgrounds` and `Signups`): **File -> Share -> Publish to
web**, select the specific sheet, format **CSV**, publish. Copy the URL
it gives you -- that's `CAMPGROUNDS_CSV_URL` / `SIGNUPS_CSV_URL`. These
are read-only public links to just that tab's data, no login required.

## 5. Set up Resend

Sign up at resend.com (free, no card required), create an API key. If
you're only ever emailing yourself, the default `onboarding@resend.dev`
sender works out of the box. To send to other people's signups, verify a
domain you own under Resend's Domains page and send from an address at
that domain instead.

## 6. Add GitHub secrets

Repo -> Settings -> Secrets and variables -> Actions -> New repository
secret, one each for:

- `CAMPGROUNDS_CSV_URL`
- `SIGNUPS_CSV_URL`
- `RESEND_API_KEY`
- `FROM_EMAIL` (e.g. `alerts@yourdomain.com` or `onboarding@resend.dev`)
- `UNSUBSCRIBE_BASE_URL`

Once those are set, the existing `scan.yml` workflow (every 5 minutes)
will read the Sheet, check availability, and send alerts.
