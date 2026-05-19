/**
 * Ozon Monitor — Google Apps Script
 * onEdit триггер: при согласовании цен запускает GitHub Actions workflow
 *
 * Установка:
 * 1. Открой таблицу Google Sheets
 * 2. Расширения → Apps Script
 * 3. Вставь этот код
 * 4. Проект → Свойства скрипта → Добавь свойство:
 *    GITHUB_TOKEN = <твой Personal Access Token с правом workflow>
 *    GITHUB_REPO  = zera-art/ozon-monitor
 * 5. Триггеры → Добавить триггер:
 *    Функция: onEditTrigger, Тип: Из таблицы, Событие: При редактировании
 */

var APPROVE_SHEET  = "📋 ОЧЕРЕДЬ ИЗМЕНЕНИЙ";
var APPROVE_COL    = 9;  // колонка "Согласовано" (1-based)
var WORKFLOW_FILE  = "ozon_daily.yml";

function onEditTrigger(e) {
  if (!e || !e.range) return;

  var sheet = e.range.getSheet();
  if (sheet.getName() !== APPROVE_SHEET) return;
  if (e.range.getColumn() !== APPROVE_COL) return;
  if (e.range.getRow() < 2) return;

  var value = e.range.getValue();
  if (value !== true && value !== "TRUE" && value !== "ИСТИНА") return;

  var props = PropertiesService.getScriptProperties();
  var token = props.getProperty("GITHUB_TOKEN");
  var repo  = props.getProperty("GITHUB_REPO") || "zera-art/ozon-monitor";

  if (!token) {
    SpreadsheetApp.getUi().alert(
      "⚠️ GITHUB_TOKEN не задан в свойствах скрипта.\n" +
      "Расширения → Apps Script → Свойства скрипта → Добавить GITHUB_TOKEN"
    );
    return;
  }

  var url = "https://api.github.com/repos/" + repo + "/actions/workflows/" +
            WORKFLOW_FILE + "/dispatches";

  var options = {
    method:  "post",
    headers: {
      "Authorization": "Bearer " + token,
      "Accept":        "application/vnd.github+json",
      "Content-Type":  "application/json",
    },
    payload: JSON.stringify({ ref: "master" }),
    muteHttpExceptions: true,
  };

  var response = UrlFetchApp.fetch(url, options);
  var code     = response.getResponseCode();

  if (code === 204) {
    SpreadsheetApp.getActiveSpreadsheet().toast(
      "⏳ Цены отправляются на Ozon. Обновится через 5–7 минут.",
      "✅ Запущено", 8
    );
  } else {
    SpreadsheetApp.getActiveSpreadsheet().toast(
      "Код: " + code + " | " + response.getContentText().substring(0, 100),
      "❌ Ошибка GitHub Actions", 10
    );
  }
}
