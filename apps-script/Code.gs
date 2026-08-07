// Google Apps Script bound to the Google Sheet that collects Form
// signups. Handles three things the Python scanner (which only ever
// reads the Sheet, never writes to it) can't do on its own:
//
//   1. onFormSubmit  - stamps every new signup with a random unsubscribe
//      token and marks it active.
//   2. doGet         - serves the actual unsubscribe link. Deploy this
//      project as a Web App (execute as: Me, access: Anyone) and put the
//      deployment URL in the UNSUBSCRIBE_BASE_URL GitHub secret.
//   3. expireOldWatches - marks "limited window" rows inactive once their
//      end_date has passed. Wire this to a daily time-driven trigger
//      (Apps Script editor -> Triggers -> Add Trigger).
//
// The Signups sheet's columns, in order, must be exactly:
//   timestamp | name | email | state | campgrounds | start_date |
//   end_date | alert_mode | unsubscribe_token | active
// The first 8 come from the Form; this script owns the last 2.

const SHEET_NAME = "Signups";

const COLUMNS = [
  "timestamp", "name", "email", "state", "campgrounds",
  "start_date", "end_date", "alert_mode",
  "unsubscribe_token", "active",
];

function onFormSubmit(e) {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  const row = e.range.getRow();
  sheet.getRange(row, COLUMNS.indexOf("unsubscribe_token") + 1).setValue(Utilities.getUuid());
  sheet.getRange(row, COLUMNS.indexOf("active") + 1).setValue("TRUE");
}

function doGet(e) {
  const token = e.parameter.token;
  if (!token) {
    return HtmlService.createHtmlOutput("Missing unsubscribe link token.");
  }

  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  const data = sheet.getDataRange().getValues();
  const tokenCol = COLUMNS.indexOf("unsubscribe_token");
  const activeCol = COLUMNS.indexOf("active");

  let found = false;
  for (let i = 1; i < data.length; i++) {
    if (data[i][tokenCol] === token) {
      sheet.getRange(i + 1, activeCol + 1).setValue("FALSE");
      found = true;
    }
  }

  return HtmlService.createHtmlOutput(
    found
      ? "You've been unsubscribed from campsite alerts."
      : "That unsubscribe link is invalid or already used."
  );
}

function expireOldWatches() {
  const sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  const data = sheet.getDataRange().getValues();
  const today = Utilities.formatDate(new Date(), Session.getScriptTimeZone(), "yyyy-MM-dd");

  const endCol = COLUMNS.indexOf("end_date");
  const modeCol = COLUMNS.indexOf("alert_mode");
  const activeCol = COLUMNS.indexOf("active");

  for (let i = 1; i < data.length; i++) {
    const mode = String(data[i][modeCol] || "").toLowerCase();
    if (mode.indexOf("rolling") !== -1) continue; // rolling watches never auto-expire

    const endDateValue = data[i][endCol];
    if (!endDateValue) continue;
    const endStr = Utilities.formatDate(new Date(endDateValue), Session.getScriptTimeZone(), "yyyy-MM-dd");
    if (endStr < today) {
      sheet.getRange(i + 1, activeCol + 1).setValue("FALSE");
    }
  }
}
