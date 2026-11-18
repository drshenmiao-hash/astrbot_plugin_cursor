function* cursorCsvRows(source) {
  const field = /"[^"]*(?:""[^"]*)*"|[^",\r\n]*/y;
  let row = [];
  while (true) {
    const [value] = field.exec(source);
    row.push(value.startsWith('"') ? value.slice(1, -1).replaceAll('""', '"') : value);
    const end = field.lastIndex;
    if (end === source.length) {
      yield row;
      return;
    }
    const separator = source[end];
    if (separator === ",") {
      field.lastIndex = end + 1;
      continue;
    }
    if (separator !== "\r" && separator !== "\n") throw new Error("CSV 引号格式有误。");
    yield row;
    row = [];
    field.lastIndex = end + (separator === "\r" && source[end + 1] === "\n" ? 2 : 1);
  }
}

function parseCursorUsageCsv(source) {
  const rows = cursorCsvRows(source.replace(/^\uFEFF/, ""));
  const headers = rows.next().value.map((value) => value.trim().toLowerCase());
  const modelIndex = headers.indexOf("model");
  const tokenIndex = headers.indexOf("total tokens");
  if (
    modelIndex < 0 || tokenIndex < 0 || modelIndex !== headers.lastIndexOf("model") ||
    tokenIndex !== headers.lastIndexOf("total tokens")
  ) {
    throw new Error("需要 Cursor 用量页导出的 CSV，且包含唯一的 Model 和 Total Tokens 列。");
  }

  let events = 0;
  let reported = 0;
  let total = 0n;
  const models = new Map();
  for (const row of rows) {
    if (row.length === 1 && !row[0].trim()) continue;
    events += 1;
    if (row.length !== headers.length) throw new Error(`CSV 第 ${events} 条用量记录的列数有误。`);
    const value = row[tokenIndex].trim();
    if (value && !/^(?:\d+|\d{1,3}(?:,\d{3})+)$/.test(value)) {
      throw new Error(`CSV 第 ${events} 条用量记录的 Token 数无效。`);
    }
    const label = row[modelIndex].trim();
    const model = models.get(label) ?? { label, events: 0, reported: 0, total: 0n };
    model.events += 1;
    models.set(label, model);
    if (!value) continue;
    const tokens = BigInt(value.replaceAll(",", ""));
    reported += 1;
    total += tokens;
    model.reported += 1;
    model.total += tokens;
  }
  const byModel = [...models.values()].sort((a, b) => {
    if (Boolean(a.reported) !== Boolean(b.reported)) return a.reported ? -1 : 1;
    if (a.total !== b.total) return a.total > b.total ? -1 : 1;
    return a.label.localeCompare(b.label);
  });
  return { events, reported, total, models: byModel };
}
