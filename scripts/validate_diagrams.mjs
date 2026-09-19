/**
 * 校验 Markdown 文档中所有 Mermaid 代码块的语法。
 *
 * 为什么需要这个脚本：
 *   Mermaid 的语法错误通常不会报错，而是**静默渲染成一片空白**。
 *   写文档的人看不到，读者看到的是残缺的图 —— 这种失败最隐蔽。
 *   因此在提交文档前应当做一次机器校验。
 *
 * 用法：
 *   node scripts/validate_diagrams.mjs ../SupportOS_系统架构.md
 *   node scripts/validate_diagrams.mjs ../SupportOS_系统架构.md ../README.md
 */

import { readFileSync } from "node:fs";
import { JSDOM } from "jsdom";

// mermaid 需要 DOM 环境
const dom = new JSDOM("<!DOCTYPE html><body></body>", { pretendToBeVisual: true });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
// Node 24 起 navigator 是只读的 getter，必须用 defineProperty 覆盖
Object.defineProperty(globalThis, "navigator", {
  value: dom.window.navigator,
  configurable: true,
  writable: true,
});
globalThis.DOMParser = dom.window.DOMParser;
globalThis.SVGElement = dom.window.SVGElement;

const mermaid = (await import("mermaid")).default;
mermaid.initialize({ startOnLoad: false, securityLevel: "loose" });

/** 从 markdown 中抽出 ```mermaid 代码块 */
function extractBlocks(md) {
  const blocks = [];
  const re = /```mermaid\r?\n([\s\S]*?)```/g;
  let m;
  while ((m = re.exec(md)) !== null) {
    const before = md.slice(0, m.index);
    const line = before.split(/\r?\n/).length;
    blocks.push({ code: m[1].trim(), line });
  }
  return blocks;
}

let total = 0;
let failed = 0;

for (const file of process.argv.slice(2)) {
  let text;
  try {
    text = readFileSync(file, "utf8");
  } catch (e) {
    console.log(`[跳过] 无法读取 ${file}: ${e.message}`);
    failed++;
    continue;
  }

  const blocks = extractBlocks(text);
  console.log(`\n${file}`);
  console.log(`  发现 ${blocks.length} 个 Mermaid 图`);

  for (const [i, b] of blocks.entries()) {
    total++;
    const kind = b.code.split(/\r?\n/)[0].trim().slice(0, 40);
    try {
      await mermaid.parse(b.code);
      console.log(`    [OK]   #${i + 1} 行 ${b.line}  〈${kind}〉`);
    } catch (e) {
      failed++;
      const msg = String(e.message || e).split("\n").slice(0, 6).join("\n         ");
      console.log(`    [FAIL] #${i + 1} 行 ${b.line}  〈${kind}〉`);
      console.log(`         ${msg}`);
    }
  }
}

console.log(`\n${"=".repeat(60)}`);
console.log(`Mermaid 校验：${total - failed}/${total} 通过`);
console.log("=".repeat(60));
process.exit(failed ? 1 : 0);
