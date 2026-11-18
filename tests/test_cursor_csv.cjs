const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const path = require("node:path");
const { test } = require("node:test");
const { runInNewContext } = require("node:vm");

const script = readFileSync(path.join(__dirname, "../views/usage/cursor-csv.js"), "utf8");
const parse = runInNewContext(`${script}\nparseCursorUsageCsv;`);

test("official export columns: sum Total Tokens only, count separate events and group models", () => {
  const result = parse([
    "Date,User,Cloud Agent ID,Automation ID,Kind,Model,Max Mode,Input (w/ Cache Write),Input (w/o Cache Write),Cache Read,Output Tokens,Total Tokens,Cost",
    '2026-11-18,"billing@example.invalid",,,Included,grok-4.6,No,20,100,880,234,"1,234",0',
    "2026-11-18,,,,Included,grok-4.6,No,0,20,0,6,26,0",
    '2026-11-18,,,,Included,"model, ""quoted""",No,0,10,15,5,30,0',
  ].join("\r\n"));
  assert.equal(result.events, 3);
  assert.equal(result.reported, 3);
  assert.equal(result.total, 1290n);
  assert.deepEqual(Array.from(result.models, (row) => [row.label, row.total, row.events]), [
    ["grok-4.6", 1260n, 2],
    ['model, "quoted"', 30n, 1],
  ]);
  assert.equal(Object.hasOwn(result, "User"), false);
});

test("accept BOM, reordered headers, embedded line breaks and a large exact integer", () => {
  const result = parse('\uFEFF"Total Tokens"," Model ",Kind\n9007199254740993,x,"note\ncontinued"\n\n');
  assert.equal(result.events, 1);
  assert.equal(result.total, 9007199254740993n);
});

test("missing counts stay unknown, including models with no reported count, while zero is known", () => {
  const result = parse("Model,Total Tokens\runknown,\rknown,0\r,200\r");
  assert.equal(result.total, 200n);
  assert.equal(result.reported, 2);
  assert.equal(result.events, 3);
  assert.deepEqual(Array.from(result.models, (row) => [row.label, row.reported]), [
    ["", 1], ["known", 1], ["unknown", 0],
  ]);
  const empty = parse("Model,Total Tokens\n");
  assert.equal(empty.events, 0);
  assert.equal(empty.models.length, 0);
});

test("reject missing and ambiguous headers, malformed CSV, and misleading totals", () => {
  for (const source of [
    "Model,Output Tokens\ngrok,100",
    "Model,Total Tokens,Model\na,10,b",
    'Model,Total Tokens\n"missing close,100',
    'Model,Total Tokens\n"grok"garbage,100',
    "Model,Total Tokens\ngrok,1,000",
    "Model,Total Tokens\ngrok,-1",
    'Model,Total Tokens\ngrok,"1,00"',
    "Model,Total Tokens\ngrok,NaN",
  ]) assert.throws(() => parse(source), /CSV/);
});
