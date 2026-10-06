const { Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
        AlignmentType, LevelFormat, HeadingLevel, BorderStyle, WidthType,
        ShadingType, VerticalAlign } = require("docx");
const fs = require("fs");

const FONT = "微软雅黑";
const NAVY = "1F3864";
const BLUE = "2E75B6";
const LIGHT = "D9E2F3";
const GREY = "F2F2F2";

const border = { style: BorderStyle.SINGLE, size: 4, color: "BFBFBF" };
const borders = { top: border, bottom: border, left: border, right: border };

function cell(text, opts = {}) {
  const { width, fill, bold = false, color = "000000", align = AlignmentType.LEFT } = opts;
  const runs = (Array.isArray(text) ? text : [text]).map(
    (t) => new TextRun({ text: t, bold, color, font: FONT, size: 21 })
  );
  return new TableCell({
    borders,
    width: { size: width, type: WidthType.DXA },
    shading: fill ? { fill, type: ShadingType.CLEAR } : undefined,
    margins: { top: 60, bottom: 60, left: 120, right: 120 },
    verticalAlign: VerticalAlign.CENTER,
    children: [new Paragraph({ alignment: align, children: runs })],
  });
}

function headerRow(cells, widths) {
  return new TableRow({
    tableHeader: true,
    children: cells.map((c, i) =>
      cell(c, { width: widths[i], fill: BLUE, bold: true, color: "FFFFFF" })
    ),
  });
}

function bodyRow(cells, widths, idx) {
  const fill = idx % 2 === 1 ? GREY : undefined;
  return new TableRow({
    children: cells.map((c, i) => cell(c, { width: widths[i], fill })),
  });
}

function makeTable(headers, rows, widths) {
  const total = widths.reduce((a, b) => a + b, 0);
  return new Table({
    width: { size: total, type: WidthType.DXA },
    columnWidths: widths,
    rows: [headerRow(headers, widths), ...rows.map((r, i) => bodyRow(r, widths, i))],
  });
}

const space = (after = 120, before = 0) => ({ after, before, line: 300 });

function example(text) {
  return new Paragraph({
    spacing: space(80, 80),
    shading: { fill: GREY, type: ShadingType.CLEAR },
    border: {
      left: { style: BorderStyle.SINGLE, size: 18, color: BLUE, space: 8 },
    },
    children: [new TextRun({ text, font: FONT, size: 22, color: "222222" })],
  });
}

function body(runs, opts = {}) {
  const arr = Array.isArray(runs) ? runs : [runs];
  return new Paragraph({
    spacing: space(opts.after ?? 120, opts.before ?? 0),
    numbering: opts.bullet ? { reference: "bullets", level: 0 } : undefined,
    children: arr.map((r) =>
      typeof r === "string"
        ? new TextRun({ text: r, font: FONT, size: 22 })
        : new TextRun({ ...r, font: FONT, size: 22 })
    ),
  });
}

const doc = new Document({
  styles: {
    default: { document: { run: { font: FONT, size: 22 } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 30, bold: true, font: FONT, color: NAVY },
        paragraph: { spacing: { before: 320, after: 160 }, outlineLevel: 0 } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 25, bold: true, font: FONT, color: BLUE },
        paragraph: { spacing: { before: 240, after: 120 }, outlineLevel: 1 } },
    ],
  },
  numbering: {
    config: [
      { reference: "bullets",
        levels: [{ level: 0, format: LevelFormat.BULLET, text: "•", alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 460, hanging: 260 } } } }] },
    ],
  },
  sections: [
    {
      properties: {
        page: {
          size: { width: 11906, height: 16838 },
          margin: { top: 1440, right: 1440, bottom: 1440, left: 1440 },
        },
      },
      children: [
        // Title
        new Paragraph({
          spacing: { after: 60 },
          children: [new TextRun({ text: "自然语言选股 · 使用参考", bold: true, font: FONT, size: 40, color: NAVY })],
        }),
        new Paragraph({
          spacing: { after: 200 },
          border: { bottom: { style: BorderStyle.SINGLE, size: 8, color: BLUE, space: 4 } },
          children: [new TextRun({ text: "用大白话写条件，软件自动帮您筛选股票", font: FONT, size: 22, color: "595959" })],
        }),

        // 一
        new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("一、直接套用的例句（改数字即可）")] }),
        body([{ text: "照搬下面任意一条，把句中的", }, { text: "数字", bold: true, color: BLUE }, { text: "换成您想要的标准即可。" }]),
        example("找ROE高于15，负债率低于60，PE低于40的股票"),
        example("找市值100亿到1000亿，PE低于50的股票"),
        example("找净利率大于10，营收增长大于20的股票"),
        example("找市净率不高于3，毛利率不低于25的股票"),
        example("找站上20日线，PE低于40的股票"),
        example("找ROE高于15，并按ROE从高到低排序的股票"),

        // 二
        new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("二、万能公式")] }),
        new Paragraph({
          spacing: space(120, 40),
          alignment: AlignmentType.CENTER,
          shading: { fill: LIGHT, type: ShadingType.CLEAR },
          children: [new TextRun({ text: "找〔指标〕〔关系〕〔数字〕，〔指标〕〔关系〕〔数字〕……的股票", bold: true, font: FONT, size: 24, color: NAVY })],
        }),
        body([{ text: "举例：想要“赚钱能力强、不太贵、负债低”的股票，就写成——" }], { after: 60 }),
        example("找ROE高于15，PE低于30，负债率低于50的股票"),

        // 三
        new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("三、可用指标")] }),
        makeTable(
          ["指标", "可以这样写"],
          [
            ["净资产收益率", "ROE、净资产收益率"],
            ["净利率", "净利率、销售净利率"],
            ["毛利率", "毛利率、销售毛利率"],
            ["资产负债率", "资产负债率、负债率"],
            ["市盈率", "PE、市盈率"],
            ["市净率", "PB、市净率"],
            ["营收增长", "营收增长率、营收增长、收入增长"],
            ["净利润增长", "净利润增长率、净利润增长、利润增长"],
            ["市值", "市值、总市值（只能用区间，如“100亿到1000亿”）"],
            ["20日趋势", "站上20日线、收盘价高于20日均线"],
          ],
          [2600, 6760]
        ),

        // 四
        new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("四、可用比较词")] }),
        makeTable(
          ["含义", "可以这样写"],
          [
            ["大于", "高于、大于、超过"],
            ["大于等于", "不低于、至少、大于等于"],
            ["小于", "低于、小于、少于"],
            ["小于等于", "不超过、不高于、小于等于"],
            ["排序（可选）", "按ROE从高到低排序、市值最大的优先、按综合评分排序"],
          ],
          [2600, 6760]
        ),

        // 五
        new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("五、请避免这样写")] }),
        makeTable(
          ["不要这样写", "请改成"],
          [
            ["PE等于20", "PE低于22（用区间或大于/小于代替“等于”）"],
            ["ROE高于15 或 PE低于20", "多个条件请用逗号隔开，软件默认要同时满足"],
            ["估值低、趋势好", "PE低于30、站上20日线（写具体数字）"],
            ["换手率、MACD金叉、股息率", "暂不支持，请用第三部分“可用指标”里的项"],
          ],
          [3400, 5960]
        ),

        new Paragraph({
          spacing: { before: 320 },
          border: { top: { style: BorderStyle.SINGLE, size: 6, color: "BFBFBF", space: 6 } },
          children: [new TextRun({ text: "提示：句子越具体、用上表里的指标和比较词，筛选越快越准。", italics: true, font: FONT, size: 20, color: "808080" })],
        }),
      ],
    },
  ],
});

const out = process.argv[2];
Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(out, buf);
  console.log("written:", out);
});
