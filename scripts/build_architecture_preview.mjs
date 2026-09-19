/**
 * 从 Markdown 生成自包含的 HTML 预览页。
 *
 * 为什么需要它：
 *   Mermaid 在 GitHub 上能直接渲染，但在本地 Markdown 编辑器、
 *   微信、PPT 里都不渲染。生成一个 HTML 就能：
 *     - 本地双击打开查看
 *     - 截图放进幻灯片
 *     - 发给不看 GitHub 的人
 *
 * 为什么是「生成」而不是「手写 HTML」：
 *   手写就会与 .md 源文件分叉 —— 改了图忘了改 HTML，
 *   阅读者看到的是过期内容。生成脚本保证两者永远一致。
 *
 * 用法：
 *   node scripts/build_architecture_preview.mjs \
 *        "../SupportOS_系统架构.md" "../architecture.html"
 */

import { readFileSync, writeFileSync } from "node:fs";

const [src, dest] = process.argv.slice(2);
if (!src || !dest) {
  console.error("用法：node build_architecture_preview.mjs <input.md> <output.html>");
  process.exit(1);
}

const md = readFileSync(src, "utf8");
const title = (md.match(/^#\s+(.+)$/m) || [, "架构文档"])[1];

// 依次抽出 mermaid 块与普通 markdown 段落
const parts = [];
const re = /```mermaid\r?\n([\s\S]*?)```/g;
let last = 0;
let m;
while ((m = re.exec(md)) !== null) {
  parts.push({ type: "md", text: md.slice(last, m.index) });
  parts.push({ type: "mermaid", text: m[1].trim() });
  last = m.index + m[0].length;
}
parts.push({ type: "md", text: md.slice(last) });

/** 极简 Markdown → HTML（只处理本文档用到的语法） */
function mdToHtml(s) {
  const esc = (t) =>
    t.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

  // 先把代码块/表格等块级元素摘出来，避免行内规则误伤
  const blocks = [];
  let t = s.replace(/```[a-z]*\r?\n([\s\S]*?)```/g, (_, code) => {
    blocks.push(`<pre><code>${esc(code.trim())}</code></pre>`);
    return `\u0000B${blocks.length - 1}\u0000`;
  });

  // 表格
  t = t.replace(
    /(^\|.+\|\r?\n\|[\s:|-]+\|\r?\n(?:\|.*\|\r?\n?)*)/gm,
    (tbl) => {
      const rows = tbl.trim().split(/\r?\n/);
      const cells = (r) =>
        r.split("|").slice(1, -1).map((c) => c.trim());
      const head = cells(rows[0]);
      const body = rows.slice(2).map(cells);
      const th = head.map((c) => `<th>${inline(c)}</th>`).join("");
      const tb = body
        .map(
          (r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`
        )
        .join("");
      return `<table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table>`;
    }
  );

  function inline(x) {
    return esc(x)
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2">$1</a>');
  }

  const lines = t.split(/\r?\n/);
  const out = [];
  let list = null;
  for (const line of lines) {
    const trimmed = line.trim();
    if (/^\u0000B\d+\u0000$/.test(trimmed)) {
      if (list) { out.push(`</${list}>`); list = null; }
      out.push(blocks[+trimmed.replace(/\u0000B|\u0000/g, "")]);
      continue;
    }
    const h = trimmed.match(/^(#{1,4})\s+(.*)$/);
    if (h) {
      if (list) { out.push(`</${list}>`); list = null; }
      out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`);
      continue;
    }
    if (/^[-*]\s+/.test(trimmed)) {
      if (list !== "ul") { if (list) out.push(`</${list}>`); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(trimmed.replace(/^[-*]\s+/, ""))}</li>`);
      continue;
    }
    if (/^\d+\.\s+/.test(trimmed)) {
      if (list !== "ol") { if (list) out.push(`</${list}>`); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(trimmed.replace(/^\d+\.\s+/, ""))}</li>`);
      continue;
    }
    if (list) { out.push(`</${list}>`); list = null; }
    if (trimmed === "---") { out.push("<hr/>"); continue; }
    if (trimmed === "") continue;
    out.push(`<p>${inline(trimmed)}</p>`);
  }
  if (list) out.push(`</${list}>`);
  return out.join("\n");
}

const body = parts
  .map((p) =>
    p.type === "mermaid"
      ? `<div class="mermaid">${p.text}</div>`
      : mdToHtml(p.text)
  )
  .join("\n");

const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>${title}</title>
<style>
  :root { --fg:#24292f; --muted:#57606a; --border:#d0d7de; --bg:#ffffff; --code:#f6f8fa; }
  * { box-sizing: border-box; }
  body {
    max-width: 1100px; margin: 0 auto; padding: 40px 24px 80px;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
                 "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
    color: var(--fg); background: var(--bg); line-height: 1.7;
  }
  h1 { font-size: 2em; border-bottom: 1px solid var(--border); padding-bottom: .3em; }
  h2 { font-size: 1.5em; margin-top: 2.2em; border-bottom: 1px solid var(--border); padding-bottom: .25em; }
  h3 { font-size: 1.2em; margin-top: 1.8em; }
  h4 { font-size: 1.05em; margin-top: 1.5em; color: var(--muted); }
  code { background: var(--code); padding: .15em .4em; border-radius: 4px; font-size: .88em;
         font-family: ui-monospace, SFMono-Regular, Consolas, monospace; }
  pre { background: var(--code); padding: 14px 16px; border-radius: 8px; overflow-x: auto; }
  pre code { background: none; padding: 0; }
  table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: .94em; }
  th, td { border: 1px solid var(--border); padding: 7px 12px; text-align: left; }
  th { background: var(--code); font-weight: 600; }
  blockquote { border-left: 4px solid #d0d7de; margin: 1em 0; padding: .2em 1em;
               color: var(--muted); background: #fafbfc; }
  hr { border: none; border-top: 1px solid var(--border); margin: 2.5em 0; }
  .mermaid { margin: 1.6em 0; text-align: center; }
  a { color: #0969da; }
  .banner { background: #fff8e1; border: 1px solid #f0c36d; border-radius: 8px;
            padding: 10px 16px; font-size: .88em; color: #7a5b00; margin-bottom: 24px; }
</style>
</head>
<body>
<div class="banner">
  本页由 <code>scripts/build_architecture_preview.mjs</code> 从
  <code>${src.split(/[\\/]/).pop()}</code> 自动生成 —— 请勿直接编辑本文件，
  改动会随下次生成被覆盖。
</div>
${body}
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({
    startOnLoad: true,
    theme: "base",
    themeVariables: {
      fontFamily: '-apple-system, "PingFang SC", "Microsoft YaHei", sans-serif',
      fontSize: "14px",
      primaryColor: "#e7f5ff",
      primaryBorderColor: "#1971c2",
      lineColor: "#868e96",
    },
    flowchart: { curve: "basis", useMaxWidth: true },
    sequence: { useMaxWidth: true },
    state: { useMaxWidth: true },
  });
</script>
</body>
</html>
`;

writeFileSync(dest, html, "utf8");
console.log(`已生成：${dest}`);
console.log(`  源文件：${src}`);
console.log(`  图表数：${parts.filter((p) => p.type === "mermaid").length}`);
