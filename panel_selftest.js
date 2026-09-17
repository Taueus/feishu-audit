/**
 * 运维面板前端回归测试（零配置，node panel_selftest.js 直接跑）
 *
 * 覆盖问题：「规则配置交互按钮有问题，点击更改设置按钮会回到当前状态」
 * 根因：refreshState 每 5s 轮询一次 → renderConfig 无条件把所有规则开关写成
 *       服务器值，把用户刚点的改动覆盖掉。
 *
 * 第 ① 部分：源码级断言（无外部依赖，始终执行）
 * 第 ② 部分：真实 DOM 集成测试（需 jsdom；缺失时自动跳过并给出安装命令）
 */
"use strict";
const fs = require("fs");
const path = require("path");

// 可用 PANEL_HTML 指向别处的 index.html，便于对「修复前」的版本做对照验证
const HTML_PATH = process.env.PANEL_HTML || path.join(__dirname, "panel", "index.html");
const html = fs.readFileSync(HTML_PATH, "utf8");

let pass = 0, fail = 0, skip = 0;
function must(cond, msg){
  if(cond){ pass++; console.log("  ✓ " + msg); }
  else { fail++; console.log("  ✗ " + msg); }
}
function skipNote(msg){ skip++; console.log("  - 跳过：" + msg); }

/* ============================================================
 * ① 源码级断言
 * ============================================================ */
console.log("\n[1] 源码级断言");

must(/setInterval\(refreshState, 5000\)/.test(html),
     "状态轮询仍是 5s 一次（修复是加防护，不是靠降低轮询频率掩盖问题）");
must(/^\s*syncRuleSwitches\(re\);\s*$/m.test(html),
     "renderConfig 通过 syncRuleSwitches 刷新规则开关（锚定调用行，避免误匹配函数定义）");
must(!/setRule\("ruleR1"/.test(html),
     "旧的 setRule(...) 无条件覆盖写法已彻底移除");
must(/let ruleDirty = \{\}/.test(html) && /if\(!ruleDirty\[k\]\) el\.checked = serverOn/.test(html),
     "未保存的规则改动带 ruleDirty 标记，轮询时不会被覆盖");
must(/n \+= Object\.keys\(ruleDirty\)\.length/.test(html),
     "规则开关改动计入「未保存」提示（此前点了开关毫无反馈）");
must(/if\(!\/\^ruleR\\d\+\$\/\.test\(el\.id\)\)/.test(html),
     "规则开关已从通用 input 监听中排除，避免事件顺序导致脏计数漏算");
must(/ruleDirty = \{\};/.test(html),
     "clearFields 保存后清空未保存标记（含此前漏掉的 r8/r9）");
must(!/\["ruleR1","ruleR2","ruleR3","ruleR4","ruleR5","ruleR6","ruleR7"\]/.test(html),
     "clearFields 不再硬编码只复位 r1–r7");

/* ============================================================
 * ② 真实 DOM 集成测试
 * ============================================================ */
let JSDOM = null;
try { JSDOM = require("jsdom").JSDOM; }
catch(e){ /* 见下方跳过提示 */ }

if(!JSDOM){
  console.log("\n[2] 真实 DOM 集成测试");
  skipNote("未安装 jsdom（集成测试需要）。安装：cd C:\\Users\\22231\\.workbuddy\\binaries\\node\\workspace && npm i jsdom");
} else {
  (async () => {
    console.log("\n[2] 真实 DOM 集成测试");

    const RULES = ["r1","r2","r3","r4","r5","r6","r7","r8","r9"];
    const serverState = () => ({
      app_id: "cli_selftest", app_secret_set: true,
      spreadsheet_token: "ShtSelftest1234567890ab", folder_token: "",
      llm: { base_url: "https://api.deepseek.com", model: "deepseek-chat", api_key_set: true },
      columns: { keyword: "项目", link: "回链", result: "机器审核", reason: "不通过原因" },
      forbidden_words: ["AI生成", "免责声明"],
      // 九条全开：这样"点击关掉某条"才有可观察的变化
      rules_enabled: RULES.reduce((o,k)=>(o[k]=true,o), {}),
      concurrency: 6,
    });
    let polls = 0;
    const fakeFetch = async (url) => {
      if(String(url).includes("/api/state")){
        polls++;
        return { json: async () => ({
          ok: true,
          bot: { running: true, pid: 12345, managed: true, started_at: Date.now()/1000 },
          config: serverState(),
          llm: { model: "deepseek-chat", api_key_set: true },
        }) };
      }
      return { json: async () => ({ ok: true, lines: ["[panel] selftest"] }) };
    };

    const dom = new JSDOM(html, {
      url: "http://127.0.0.1:8788/",
      runScripts: "dangerously",
      pretendToBeVisual: true,
      beforeParse(w){ w.fetch = fakeFetch; },
    });
    const { window } = dom;
    const doc = window.document;
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const dirtyTxt = () => doc.getElementById("dirtyN").textContent;

    await sleep(300);                       // 等 init() 里的首次 refreshState 完成

    const sw = k => doc.getElementById("rule" + k.toUpperCase());

    must(polls >= 1, "页面初始化已向 /api/state 拉取状态（" + polls + " 次）");
    must(RULES.every(k => sw(k).checked === true), "首次加载：九条开关全部跟随服务器值(true)");
    must(dirtyTxt() === "", "首次加载：无未保存提示");

    // —— 核心场景：点击关闭规则六 ——
    sw("r6").click();
    must(sw("r6").checked === false, "点击规则六开关 → 立即变为「关」");
    must(dirtyTxt() === "未保存 1", "并立即提示「未保存 1」（此前没有任何反馈）");
    must(doc.getElementById("abTx").classList.contains("dirty"), "底部操作条进入高亮状态");

    // —— 模拟 5s 轮询触发 3 次 ——
    for(let i = 0; i < 3; i++) await window.refreshState();
    must(sw("r6").checked === false,
         "★ 轮询 3 次后规则六仍是用户改的「关」（原 bug 会弹回「开」）");
    must(dirtyTxt() === "未保存 1", "轮询后未保存提示仍保留");

    // —— 多开关并存 ——
    sw("r2").click();
    await window.refreshState();
    must(sw("r2").checked === false && sw("r6").checked === false,
         "同时改动规则二 + 规则六，轮询后都保留");
    must(dirtyTxt() === "未保存 2", "未保存计数正确累加为 2");
    must(sw("r3").checked === true && sw("r9").checked === true,
         "未改动的规则仍正确跟随服务器，没被误留");

    // —— 改回原值 → 标记自动解除 ——
    sw("r6").click();
    must(sw("r6").checked === true, "规则六再点一次回到「开」");
    must(dirtyTxt() === "未保存 1", "未保存计数回落为 1（规则六标记自动解除）");

    // —— 保存成功后：标记清空，开关呈现服务器新值 ——
    sw("r2").click();                                   // 把 r2 也改回原值
    must(dirtyTxt() === "", "全部改回原值后，未保存提示消失");
    must(!doc.getElementById("abTx").classList.contains("dirty"), "底部操作条高亮撤销");

    window.close();
    finish();
  })().catch(e => { console.error("\n集成测试异常：", e); fail++; finish(); });
}

function finish(){
  console.log("\n结果：" + pass + " 通过, " + fail + " 失败" + (skip ? ", " + skip + " 跳过" : ""));
  process.exit(fail ? 1 : 0);
}
if(!JSDOM) finish();
