function doGet() {
  return HtmlService.createHtmlOutputFromFile('index')
    .setTitle('ChocoLog')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no');
}

// 起動時にマスターと直近のログを取得
function getInitialData() {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const masterSheet = ss.getSheetByName('Master');
  const logSheet = ss.getSheetByName('Log');
  
  const masterData = masterSheet.getDataRange().getValues();
  masterData.shift(); // ヘッダー除去
  
  // 直近ログの逆引きマップを作成
  const lastLogs = {};
  if (logSheet.getLastRow() > 1) {
    const logData = logSheet.getDataRange().getValues();
    for (let i = logData.length - 1; i >= 1; i--) {
      const name = logData[i][1];
      if (!lastLogs[name]) {
        lastLogs[name] = {
          load: logData[i][2],
          volume: logData[i][3],
          mode: logData[i][4]
        };
      }
    }
  }

  // UI用の初期アイテムリストを構築
  const items = masterData.map(row => {
    const name = row[0];
    const loadLabel = row[1] || '';
    const volumeLabel = row[2] || '';
    const modes = row[3] ? String(row[3]).split(',').map(s => s.trim()) : [];
    const defaultLoad = Number(row[4]) || 0;
    const defaultVol = Number(row[5]) || 0;

    const prev = lastLogs[name];
    return {
      name: name,
      loadLabel: loadLabel,
      volumeLabel: volumeLabel,
      modes: modes,
      selectedMode: prev ? prev.mode : (modes[0] || ''),
      load: prev ? Number(prev.load) : defaultLoad,
      volume: prev ? Number(prev.volume) : defaultVol,
      active: false // 実施するかどうかのフラグ（初期値はパス）
    };
  });

  return items;
}

// 記録の一括保存
function saveLogs(payload) {
  const ss = SpreadsheetApp.getActiveSpreadsheet();
  const logSheet = ss.getSheetByName('Log');
  const now = new Date();
  
  const rows = payload.map(item => [
    now,
    item.name,
    item.loadLabel ? item.load : '',
    item.volumeLabel ? item.volume : '',
    item.modes.length ? item.selectedMode : ''
  ]);

  if (rows.length > 0) {
    logSheet.getRange(logSheet.getLastRow() + 1, 1, rows.length, 5).setValues(rows);
  }
  return { status: 'success', count: rows.length };
}