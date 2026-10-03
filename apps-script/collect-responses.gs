/**
 * Paste this whole file into a Google Sheet's Apps Script editor
 * (Extensions → Apps Script), then deploy it as a Web App (see README.md's
 * "Collecting everyone's picks" section for the exact steps).
 *
 * What it does: every time the vacation map page's "Confirm Selection"
 * form is submitted, it POSTs a small JSON payload here. This script
 * appends one row to a sheet named "Responses" in whichever Google Sheet
 * it's bound to — creating that sheet (and its header row) the first time
 * it runs. That sheet is the "spreadsheet view" of everyone's picks.
 *
 * This is 100% free (Apps Script's free quota is far more than a family
 * trip poll will ever use) and requires no server of your own.
 */

function doPost(e) {
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName("Responses");
  if (!sheet) {
    sheet = SpreadsheetApp.getActiveSpreadsheet().insertSheet("Responses");
  }
  if (sheet.getLastRow() === 0) {
    sheet.appendRow([
      "Timestamp (server)", "First Name", "Last Name",
      "Property", "Date Range", "Price Shown",
      "Adults", "Children (0-17)"
    ]);
  }

  var data = {};
  try {
    data = JSON.parse(e.postData.contents);
  } catch (err) {
    data = {};
  }

  sheet.appendRow([
    new Date(),
    data.firstName || "",
    data.lastName || "",
    data.property || "",
    data.dateRange || "",
    data.price || "",
    data.adults != null ? data.adults : "",
    data.children != null ? data.children : ""
  ]);

  return ContentService
    .createTextOutput(JSON.stringify({ status: "ok" }))
    .setMimeType(ContentService.MimeType.JSON);
}

// Lets you sanity-check the deployed URL in a browser — visiting the URL
// directly (a GET request) just confirms the endpoint is alive; it never
// writes a row.
function doGet(e) {
  return ContentService.createTextOutput("This endpoint only accepts POST requests from the vacation map page.");
}
