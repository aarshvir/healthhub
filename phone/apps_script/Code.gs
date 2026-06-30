/**
 * HealthHub — Telegram logger (Google Apps Script).
 *
 * A Telegram bot posts messages here (webhook); each message becomes one row in the "logs"
 * worksheet, in the exact Health Log schema consumed by journal.py:
 *
 *   entry_id,date,time,day,type,item,food,calories_kcal,net_carbs_g,protein_g,fat_g,fiber_g,
 *   mood_1to5,energy_1to5,symptom,symptom_sev_1to5,supplements,glucose_mgdl,sleep_h,tags,note
 *
 * Setup:
 *   1. Create a bot with @BotFather -> get TOKEN.
 *   2. Script Properties: set TELEGRAM_TOKEN and (optional) ALLOWED_CHAT_ID, SHEET_ID.
 *   3. Deploy > New deployment > Web app (execute as me, access: anyone).
 *   4. Point the webhook at the /exec URL:
 *        https://api.telegram.org/bot<TOKEN>/setWebhook?url=<WEB_APP_URL>
 *
 * Message grammar (one per line; key=value pairs, free text becomes the note):
 *   /meal Paneer carbs=38 protein=40 fat=47 fiber=4 kcal=746 tags=+walk,+vegfirst
 *   /intervention +acv tags=+acv          (also: +walk +methi +vegfirst)
 *   /supplement Triphala
 *   /symptom feverish sev=2
 *   /mood 3
 *   /energy 2
 *   /glucose 150
 *   /note anything free text
 * Lines with no leading /type are stored as a free-text note.
 */

var HEADERS = ['entry_id', 'date', 'time', 'day', 'type', 'item', 'food', 'calories_kcal',
  'net_carbs_g', 'protein_g', 'fat_g', 'fiber_g', 'mood_1to5', 'energy_1to5', 'symptom',
  'symptom_sev_1to5', 'supplements', 'glucose_mgdl', 'sleep_h', 'tags', 'note'];
var TZ = 'Asia/Dubai';

function doPost(e) {
  try {
    var update = JSON.parse(e.postData.contents);
    var msg = update.message || update.edited_message;
    if (!msg || !msg.text) return _ok();
    var allowed = _prop('ALLOWED_CHAT_ID');
    if (allowed && String(msg.chat.id) !== String(allowed)) return _ok();

    var row = parseMessage(msg.text, new Date());
    appendRow(row);
    reply(msg.chat.id, '✅ logged: ' + row.type + (row.item ? ' — ' + row.item : ''));
    return _ok();
  } catch (err) {
    return ContentService.createTextOutput('error: ' + err).setMimeType(ContentService.MimeType.TEXT);
  }
}

/** Parse a Telegram text message into a logs row object. Pure + unit-testable. */
function parseMessage(text, when) {
  var sheet = getSheet();
  var nextId = Math.max(0, sheet.getLastRow() - 1) + 1; // header is row 1
  var row = {
    entry_id: nextId,
    date: Utilities.formatDate(when, TZ, 'yyyy-MM-dd'),
    time: Utilities.formatDate(when, TZ, 'HH:mm'),
    day: Utilities.formatDate(when, TZ, 'EEE'),
    type: 'note', item: '', food: '', tags: '', note: ''
  };
  var line = text.trim();
  var typeMatch = line.match(/^\/(\w+)\s*/);
  var rest = line;
  if (typeMatch) { row.type = typeMatch[1].toLowerCase(); rest = line.slice(typeMatch[0].length); }

  var tags = [];
  var freeWords = [];
  rest.split(/\s+/).forEach(function (tok) {
    if (!tok) return;
    var kv = tok.match(/^([a-zA-Z_]+)=(.+)$/);
    if (kv) {
      var k = kv[1].toLowerCase(), v = kv[2];
      if (k === 'carbs' || k === 'netcarbs') row.net_carbs_g = v;
      else if (k === 'protein') row.protein_g = v;
      else if (k === 'fat') row.fat_g = v;
      else if (k === 'fiber') row.fiber_g = v;
      else if (k === 'kcal' || k === 'calories') row.calories_kcal = v;
      else if (k === 'sev' || k === 'severity') row.symptom_sev_1to5 = v;
      else if (k === 'glucose' || k === 'sgv') row.glucose_mgdl = v;
      else if (k === 'sleep') row.sleep_h = v;
      else if (k === 'tags') tags = tags.concat(v.split(','));
      else freeWords.push(tok);
    } else if (tok.charAt(0) === '+') {
      tags.push(tok);                       // intervention shorthand: +acv +walk +methi
    } else {
      freeWords.push(tok);
    }
  });

  var free = freeWords.join(' ').trim();
  if (row.type === 'meal') { row.item = free; row.food = free; }
  else if (row.type === 'mood') { row.mood_1to5 = free || row.mood_1to5; }
  else if (row.type === 'energy') { row.energy_1to5 = free || row.energy_1to5; }
  else if (row.type === 'symptom') { row.symptom = free; }
  else if (row.type === 'glucose') { row.glucose_mgdl = row.glucose_mgdl || free; }
  else if (row.type === 'supplement') { row.supplements = free; row.item = free; }
  else { row.item = free; }
  row.tags = tags.join('; ');
  row.note = text;
  return row;
}

function appendRow(row) {
  var sheet = getSheet();
  ensureHeader(sheet);
  sheet.appendRow(HEADERS.map(function (h) { return row[h] !== undefined ? row[h] : ''; }));
}

function ensureHeader(sheet) {
  if (sheet.getLastRow() === 0) sheet.appendRow(HEADERS);
}

function getSheet() {
  var id = _prop('SHEET_ID');
  var ss = id ? SpreadsheetApp.openById(id) : SpreadsheetApp.getActiveSpreadsheet();
  return ss.getSheetByName('logs') || ss.insertSheet('logs');
}

function reply(chatId, text) {
  var token = _prop('TELEGRAM_TOKEN');
  if (!token) return;
  UrlFetchApp.fetch('https://api.telegram.org/bot' + token + '/sendMessage', {
    method: 'post', contentType: 'application/json', muteHttpExceptions: true,
    payload: JSON.stringify({ chat_id: chatId, text: text })
  });
}

function _prop(k) { return PropertiesService.getScriptProperties().getProperty(k); }
function _ok() { return ContentService.createTextOutput('ok').setMimeType(ContentService.MimeType.TEXT); }
