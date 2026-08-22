/* 共用侧边栏 + 权限渲染（所有后台页共用，保证菜单一致、按角色显示）。
 * 用法：页面放 <ul class="sidebar-menu" id="sidebarMenu"></ul> 与（可选）<span id="userBox">，
 *       并在 </body> 前引入 <script src="/static/sidebar.js"></script>
 *
 * 菜单可见性：super 恒见全部；admin/member 由超管在「成员管理→菜单权限」配置
 * （后端 /api/v1/menu_permissions）。拉取失败时回退到下方 MENU 的 roles 默认值。
 */
(function () {
  // 菜单表：roles 仅作为「配置拉取失败」时的兜底默认（正常以服务端配置为准）。
  var MENU = [
    { label: '线索公海', href: '/admin', icon: '/static/icons/gonghaikehu.svg', roles: ['member', 'admin', 'super'] },
    { label: 'AI活动库', href: '/admin/ai_library', icon: '/static/icons/jiqiren.svg', roles: ['member', 'admin', 'super'] },
    { label: '我的活动库', href: '/admin/library', icon: '/static/icons/sirendingzhi.svg', roles: ['member', 'admin', 'super'] },
    { label: '主办方库', href: '/admin/organizers', icon: '/static/icons/shenqingzhubanfang.svg', roles: ['super'] },
    { label: '数据优化', href: '/admin/keywords', roles: ['admin', 'super'] },
    { label: '采集设置', href: '/admin/collection', roles: ['admin', 'super'] },
    { label: '设备监控', href: '/admin/devices', roles: ['admin', 'super'] },
    { label: '搜狗采集', href: '/admin/sogou', roles: ['admin', 'super'] },
    { label: '系统设置', href: '/admin/settings', roles: ['admin', 'super'] },
    { label: '反馈管理', href: '/admin/feedback', roles: ['admin', 'super'] },
    { label: '成员管理', href: '/admin/users', roles: ['admin', 'super'] },
  ];
  // 仅超级管理员可“修改”的页面（普通管理员只读）
  var SUPER_WRITE_PAGES = ['/admin/collection', '/admin/settings', '/admin/sogou'];

  function norm(p) { return (p === '/admin/') ? '/admin' : p; }
  var path = norm(location.pathname);

  Promise.all([
    fetch('/api/v1/me').then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; }),
    fetch('/api/v1/menu_permissions').then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; })
  ]).then(function (res) {
    var me = res[0];
    if (!me) { location.href = '/login'; return; }
    var perms = (res[1] && res[1].permissions) || null;
    var role = me.role;

    // 单项可见性：super 全可见；有服务端配置则以配置为准；否则回退 roles 默认。
    function visible(m) {
      if (role === 'super') return true;
      if (perms && perms[role] && Object.prototype.hasOwnProperty.call(perms[role], m.href)) return !!perms[role][m.href];
      return m.roles.indexOf(role) !== -1;
    }

    // 无权访问当前页 → 回线索公海（与后端中间件拦截一致）
    var cur = MENU.find(function (m) { return m.href === path; });
    if (cur && !visible(cur)) { location.href = '/admin'; return; }

    // 渲染菜单
    var ul = document.getElementById('sidebarMenu');
    if (ul) {
      ul.innerHTML = MENU.filter(visible).map(function (m) {
        var active = (m.href === path) ? ' class="active"' : '';
        var icon = m.icon ? '<img src="' + m.icon + '" style="width:16px;height:16px;vertical-align:middle;margin-right:5px;">' : '';
        return '<a href="' + m.href + '" style="text-decoration:none;"><li' + active + '>' + icon + m.label + '</li></a>';
      }).join('');
    }

    // 顶部用户框
    var ub = document.getElementById('userBox');
    if (ub) ub.textContent = me.username + '（' + (me.team_name || '-') + '）';

    // 采集/系统设置页：非超管只读（禁用表单控件 + 顶部提示）
    if (SUPER_WRITE_PAGES.indexOf(path) !== -1 && role !== 'super') {
      document.querySelectorAll('input, select, textarea, button').forEach(function (el) {
        // 放行导航/退出等（无 form 语义的链接不受影响；这里只禁表单控件与按钮）
        if (el.id === 'logoutBtn') return;
        el.disabled = true;
      });
      var banner = document.createElement('div');
      banner.textContent = '只读：仅超级管理员可修改本页设置';
      banner.style.cssText = 'background:rgba(251,191,36,.15);color:#fcd34d;border:1px solid rgba(251,191,36,.4);padding:10px 14px;border-radius:8px;margin-bottom:16px;font-size:14px;';
      var main = document.querySelector('.main-content') || document.querySelector('.main') || document.body;
      main.insertBefore(banner, main.firstChild);
    }

    window.__ME__ = me;
    document.dispatchEvent(new CustomEvent('me-loaded', { detail: me }));
  });
})();
