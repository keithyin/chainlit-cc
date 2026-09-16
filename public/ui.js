/* 浏览器端 UI 补丁。
 *
 * Chainlit 没有插件机制，只能在 .chainlit/config.toml 的 custom_js 里注入一个
 * script（那个配置**只有单值**，所以这里的多个补丁共用一个文件）。每个补丁是一个
 * 互不引用的 IIFE，各管各的；再多就该拆成入口 + 动态 import 了。
 */
(function () {
/* ---- 1. 刷新后自动跳回上次的线程 ----
 *
 * Chainlit 2.12 的前端把会话 id 放在内存里（SessionId atom 默认 v4()），刷新即丢，
 * 所以停在 "/" 刷新会新建一条线程。服务端本身能恢复（数据层 + on_chat_resume），
 * 缺的只是"刷新后还知道上次是哪条线程"。
 *
 * 这里只在同一个标签页里记线程号：sessionStorage 而非 localStorage，因为
 * 一个标签页≈一个 Chainlit 会话，两个标签页各看各的不该互相跳。
 *
 * 注入点在 index.html 里早于应用 bundle，且 custom_js_attributes 默认 defer；
 * 经典 defer 脚本先于 defer module 执行，所以这里的 replaceState 能赶在
 * react-router 初始化之前改掉 URL。
 */
  var KEY = "cc:lastThread";
  var THREAD_RE = /^\/thread\/([^/?#]+)/;

  // 先读再删（消费一次）：线程若已被删掉，恢复会失败退回 "/"，
  // 留着键就会变成"跳转→失败→回首页→再跳"的死循环。
  var last = null;
  try {
    last = sessionStorage.getItem(KEY);
    if (last) sessionStorage.removeItem(KEY);
  } catch (e) {
    return; // 隐私模式等场景下 sessionStorage 不可用，静默降级为不跳转
  }

  var match = location.pathname.match(THREAD_RE);
  if (match) {
    // 已经在某条线程上，刷新交给 Chainlit 自己的 /thread/<id> 恢复流程
    return;
  }
  if (location.pathname === "/" && last) {
    history.replaceState(null, "", "/thread/" + last);
  }

  // 记下离开时的线程号，供下一次刷新使用
  window.addEventListener("pagehide", function () {
    try {
      var m = location.pathname.match(THREAD_RE);
      if (m) {
        sessionStorage.setItem(KEY, m[1]);
      } else if (location.pathname === "/") {
        // 点了"新建对话"：用户是主动要开新的，别再把他拽回去
        sessionStorage.removeItem(KEY);
      }
    } catch (e) {
      /* 忽略 */
    }
  });
})();

(function () {
/* ---- 2. 登录页加一个「注册」入口 ----
 *
 * Chainlit 的登录页是 React 渲染的，没有插槽也没有配置项，只能往表单上挂节点。
 * 用 MutationObserver 而不是定时器：不轮询，且 React 重渲染（比如登录失败时插入
 * Alert）后能自动补回去；回调里的判断很便宜，常驻没有代价。
 *
 * 锚点是 LoginForm.tsx 里的 input#email / input#password。Chainlit 哪天改了 DOM，
 * 这里就静默不注入（5 秒后给一条 console 警告），兜底是直接访问
 * <root>/public/register.html —— 那个地址由 Chainlit 自己的静态路由提供。
 */
  var ID = "cc-register-link";
  var LOGIN_RE = /\/login\/?$/;

  function rootPath() {
    // 与前端 src/lib/router.ts 同源：index.html 里的 og:root_path
    var m = document.querySelector('meta[property="og:root_path"]');
    return (m && m.getAttribute("content")) || "";
  }

  function inject() {
    if (document.getElementById(ID)) return;
    if (!LOGIN_RE.test(location.pathname)) return;
    var pw = document.getElementById("password");
    var form = pw && pw.closest("form");
    if (!form || !form.querySelector("#email")) return;

    var box = document.createElement("p");
    box.id = ID;
    // 用的是 Chainlit 自带样式表里的 Tailwind 类；类名若被 purge 掉链接也还在，
    // 只是没样式
    box.className = "text-center text-sm text-muted-foreground";
    var a = document.createElement("a");
    a.href = rootPath() + "/public/register.html";
    a.className = "text-primary underline-offset-4 hover:underline";
    a.textContent = "还没有账号？注册";
    box.appendChild(a);
    form.appendChild(box);
  }

  inject();
  new MutationObserver(inject).observe(document.documentElement, {
    childList: true,
    subtree: true,
  });

  // 首次渲染前 inject() 必然失败，所以不在这里报警：等几秒再看，还在登录页又没注入
  // 成功，才是真的没找到表单（用来区分"Chainlit 改版了"和"JS 被禁了"）
  setTimeout(function () {
    if (!document.getElementById(ID) && LOGIN_RE.test(location.pathname)) {
      console.warn(
        "[cc] 没找到 Chainlit 的登录表单，注册链接未注入；" +
          "可直接访问 " + rootPath() + "/public/register.html"
      );
    }
  }, 5000);
})();
