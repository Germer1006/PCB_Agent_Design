// 把 Markdown 设计报告转换为 Word（.docx）：封面、目录、标题样式、表格、图片+图题、附录。
//
// 依赖（不在仓库中，首次使用时装到任意临时目录）：
//   mkdir C:\Temp\docgen && cd C:\Temp\docgen && npm init -y && npm i docx marked
// 用法：
//   set DOCX_DEPS=C:\Temp\docgen\node_modules
//   node md2docx.mjs <report.md> <out.docx> [--append report_tables.md] [--title-info "组别/作者 等"]
//
// 映射：# → 封面标题；## → 标题1；### → 标题2；#### → 标题3；单独成段的图片 → 居中插图，
// 紧随其后的 *斜体段* → 图题；表格 → 带表头底色的三线风格表；> 引用 → 灰底说明。

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const deps = process.env.DOCX_DEPS;
const require = createRequire(deps ? path.join(deps, "_") : import.meta.url);
const docx = require("docx");
const { marked } = require("marked");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, Table, TableRow, TableCell, WidthType,
  ImageRun, ShadingType, BorderStyle, TableOfContents, Footer, PageNumber, PageBreak, TableLayoutType,
} = docx;

const args = process.argv.slice(2);
const [mdPath, outPath] = args;
const appendIdx = args.indexOf("--append");
const appendPath = appendIdx > 0 ? args[appendIdx + 1] : null;
const infoIdx = args.indexOf("--title-info");
const titleInfo = infoIdx > 0 ? args[infoIdx + 1] : "";
const baseDir = path.dirname(path.resolve(mdPath));

const CN = "宋体", CN_H = "黑体", EN = "Times New Roman", MONO = "Consolas";
const BODY = 21, TABLE = 18, SMALL = 16; // half-points: 10.5 / 9 / 8 pt
const PAGE_W_TWIP = 11906, MARGIN = 1418; // A4, 2.5 cm
const CONTENT_TWIP = PAGE_W_TWIP - 2 * MARGIN;
const MAX_IMG_PX = 600;

const decode = (s) => s.replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
  .replace(/&amp;/g, "&");

function runs(tokens, base = {}) {
  const out = [];
  for (const t of tokens || []) {
    const st = { ...base };
    switch (t.type) {
      case "strong": out.push(...runs(t.tokens, { ...st, bold: true })); break;
      case "em": out.push(...runs(t.tokens, { ...st, italics: true })); break;
      case "codespan":
        out.push(new TextRun({ text: decode(t.text), font: { ascii: MONO, hAnsi: MONO, eastAsia: CN }, size: st.size, bold: st.bold }));
        break;
      case "link": out.push(...runs(t.tokens, st)); break;
      case "br": out.push(new TextRun({ break: 1 })); break;
      case "text":
        if (t.tokens && t.tokens.length) out.push(...runs(t.tokens, st));
        else out.push(new TextRun({ text: decode(t.text), ...fontOf(st) }));
        break;
      case "escape": out.push(new TextRun({ text: decode(t.text), ...fontOf(st) })); break;
      case "image": break;
      default: if (t.text) out.push(new TextRun({ text: decode(t.text), ...fontOf(st) }));
    }
  }
  return out;
}

function fontOf(st) {
  return { font: { ascii: EN, hAnsi: EN, eastAsia: st.heading ? CN_H : CN }, size: st.size, bold: st.bold,
    italics: st.italics, color: st.color };
}

function pngSize(file) {
  const b = fs.readFileSync(file);
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) };
}

function imageParagraph(src) {
  const file = path.resolve(baseDir, src);
  if (!fs.existsSync(file)) return new Paragraph({ children: [new TextRun({ text: `[缺少图片 ${src}]`, color: "C00000" })] });
  const { w, h } = pngSize(file);
  const scale = Math.min(1, MAX_IMG_PX / w);
  return new Paragraph({
    alignment: AlignmentType.CENTER, spacing: { before: 120, after: 60 },
    children: [new ImageRun({ type: "png", data: fs.readFileSync(file), transformation: { width: Math.round(w * scale), height: Math.round(h * scale) } })],
  });
}

function cellText(cell) {
  return (cell.text || "").replace(/\*\*|`/g, "");
}

function table(tok) {
  const ncol = tok.header.length;
  const all = [tok.header, ...tok.rows];
  const len = Array.from({ length: ncol }, (_, j) =>
    Math.min(40, Math.max(4, ...all.map((r) => [...cellText(r[j] || {})].reduce((a, ch) => a + (ch.charCodeAt(0) > 255 ? 2 : 1), 0)))));
  const total = len.reduce((a, b) => a + b, 0);
  const widths = len.map((l) => Math.round((CONTENT_TWIP * l) / total));
  const size = ncol >= 6 || total > 110 ? SMALL : TABLE;
  const border = { style: BorderStyle.SINGLE, size: 4, color: "808080" };
  const mk = (cell, j, header) => new TableCell({
    width: { size: widths[j], type: WidthType.DXA },
    shading: header ? { type: ShadingType.CLEAR, color: "auto", fill: "D9E2F3" } : undefined,
    margins: { top: 40, bottom: 40, left: 70, right: 70 },
    borders: { top: border, bottom: border, left: border, right: border },
    children: [new Paragraph({
      alignment: header ? AlignmentType.CENTER : AlignmentType.LEFT, spacing: { before: 0, after: 0, line: 260 },
      children: runs(cell.tokens, { size, bold: header || undefined }),
    })],
  });
  return new Table({
    width: { size: CONTENT_TWIP, type: WidthType.DXA }, columnWidths: widths, layout: TableLayoutType.FIXED,
    rows: [new TableRow({ tableHeader: true, children: tok.header.map((c, j) => mk(c, j, true)) }),
      ...tok.rows.map((r) => new TableRow({ children: r.map((c, j) => mk(c, j, false)) }))],
  });
}

function blocks(tokens, out, opts = {}) {
  for (let i = 0; i < tokens.length; i++) {
    const t = tokens[i];
    switch (t.type) {
      case "heading": {
        const depth = t.depth + (opts.demote || 0);
        if (depth === 1) break; // 封面标题单独处理
        const level = [null, null, HeadingLevel.HEADING_1, HeadingLevel.HEADING_2, HeadingLevel.HEADING_3][Math.min(depth, 4)];
        const size = [0, 0, 32, 28, 24][Math.min(depth, 4)];
        out.push(new Paragraph({ heading: level, spacing: { before: depth === 2 ? 360 : 240, after: 120 },
          children: runs(t.tokens, { heading: true, size, bold: true, color: "000000" }) }));
        break;
      }
      case "paragraph": {
        const img = t.tokens.find((x) => x.type === "image");
        if (img && t.tokens.filter((x) => x.type !== "text" || x.text.trim()).length === 1) {
          out.push(imageParagraph(img.href));
          break;
        }
        const isCaption = t.tokens.length === 1 && t.tokens[0].type === "em" && /^图\s*\d/.test(t.tokens[0].text);
        if (isCaption) {
          out.push(new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 },
            children: runs(t.tokens[0].tokens, { size: TABLE }) }));
          break;
        }
        out.push(new Paragraph({ spacing: { after: 100, line: 360 }, indent: opts.indent ? { left: opts.indent } : undefined,
          children: runs(t.tokens, { size: BODY }) }));
        break;
      }
      case "table":
        out.push(table(t));
        out.push(new Paragraph({ spacing: { after: 120 }, children: [] }));
        break;
      case "list":
        t.items.forEach((it, k) => {
          const bullet = t.ordered ? `${(t.start || 1) + k}. ` : "• ";
          const inline = [], nested = [];
          for (const c of it.tokens) (c.type === "list" ? nested : inline).push(c);
          const r = inline.flatMap((c) => (c.tokens ? runs(c.tokens, { size: BODY }) : runs([c], { size: BODY })));
          out.push(new Paragraph({ spacing: { after: 60, line: 340 }, indent: { left: 420 + (opts.indent || 0), hanging: 300 },
            children: [new TextRun({ text: bullet, font: { ascii: EN, eastAsia: CN }, size: BODY }), ...r] }));
          if (nested.length) blocks(nested, out, { ...opts, indent: (opts.indent || 0) + 420 });
        });
        break;
      case "blockquote":
        for (const c of t.tokens) {
          if (c.type !== "paragraph") continue;
          out.push(new Paragraph({ spacing: { after: 60, line: 300 }, shading: { type: ShadingType.CLEAR, color: "auto", fill: "F2F2F2" },
            border: { left: { style: BorderStyle.SINGLE, size: 18, color: "A6A6A6", space: 6 } },
            children: runs(c.tokens, { size: TABLE, color: "404040" }) }));
        }
        break;
      case "code":
        for (const line of t.text.split("\n")) {
          out.push(new Paragraph({ spacing: { after: 0, line: 260 }, shading: { type: ShadingType.CLEAR, color: "auto", fill: "F2F2F2" },
            children: [new TextRun({ text: line || " ", font: { ascii: MONO, hAnsi: MONO, eastAsia: CN }, size: SMALL })] }));
        }
        out.push(new Paragraph({ children: [] }));
        break;
      case "hr":
      case "space":
        break;
      default:
        if (t.text) out.push(new Paragraph({ children: [new TextRun({ text: decode(t.text), size: BODY })] }));
    }
  }
}

const md = fs.readFileSync(mdPath, "utf8");
const toks = marked.lexer(md);
const titleTok = toks.find((t) => t.type === "heading" && t.depth === 1);
const title = titleTok ? titleTok.text : path.basename(mdPath, ".md");
const [mainTitle, subTitle] = title.split(/\s+—\s+/);

const cover = [
  new Paragraph({ spacing: { before: 2600 }, children: [] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 300 },
    children: [new TextRun({ text: "嵌入式系统实践课程设计", font: { ascii: EN, eastAsia: CN_H }, size: 32 })] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 300 },
    children: [new TextRun({ text: mainTitle, font: { ascii: EN, eastAsia: CN_H }, size: 44, bold: true })] }),
  ...(subTitle ? [new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 1200 },
    children: [new TextRun({ text: subTitle, font: { ascii: EN, eastAsia: CN_H }, size: 32 })] })] : []),
  ...(titleInfo ? titleInfo.split("|").map((s) => new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 160 },
    children: [new TextRun({ text: s.trim(), font: { ascii: EN, eastAsia: CN }, size: 26 })] })) : []),
  new Paragraph({ children: [new PageBreak()] }),
  new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 200 },
    children: [new TextRun({ text: "目　录", font: { ascii: EN, eastAsia: CN_H }, size: 32, bold: true })] }),
  new TableOfContents("目录", { hyperlink: true, headingStyleRange: "1-2" }),
  new Paragraph({ children: [new PageBreak()] }),
];

const body = [];
blocks(toks, body);
if (appendPath) {
  body.push(new Paragraph({ children: [new PageBreak()] }));
  const at = marked.lexer(fs.readFileSync(appendPath, "utf8"));
  const h = at.find((t) => t.type === "heading" && t.depth === 1);
  body.push(new Paragraph({ heading: HeadingLevel.HEADING_1, spacing: { before: 360, after: 120 },
    children: [new TextRun({ text: "附录　" + (h ? h.text : "数据表"), font: { ascii: EN, eastAsia: CN_H }, size: 32, bold: true, color: "000000" })] }));
  blocks(at, body, { demote: 1 });
}

const doc = new Document({
  features: { updateFields: true },
  styles: {
    default: { document: { run: { font: { ascii: EN, hAnsi: EN, eastAsia: CN }, size: BODY } } },
    paragraphStyles: [1, 2, 3].map((n) => ({
      id: `Heading${n}`, name: `Heading ${n}`, basedOn: "Normal", next: "Normal", quickFormat: true,
      run: { font: { ascii: EN, hAnsi: EN, eastAsia: CN_H }, bold: true, color: "000000", size: [0, 32, 28, 24][n] },
      paragraph: { spacing: { before: 240, after: 120 }, keepNext: true },
    })),
  },
  sections: [{
    properties: { page: { size: { width: PAGE_W_TWIP, height: 16838 }, margin: { top: 1440, bottom: 1440, left: MARGIN, right: MARGIN } } },
    footers: { default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER,
      children: [new TextRun({ children: [PageNumber.CURRENT], size: SMALL })] })] }) },
    children: [...cover, ...body],
  }],
});

fs.writeFileSync(outPath, await Packer.toBuffer(doc));
console.log(`written ${outPath}`);
