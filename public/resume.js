/* 刷新后自动跳回上次的线程。
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
(function () {
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
