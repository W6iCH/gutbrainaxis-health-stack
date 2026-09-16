/**
 * Admin Console v6 — 管理控制台前端 SPA
 * 纯原生 JS，零外部依赖，离线可用
 */
(function () {
  'use strict';

  // ── 状态 ─────────────────────────────────────────────────────
  let state = {
    user: null,
    authed: false,
    page: 'loading',
    config: {},
    processes: [],
  };

  const API = {
    csrf: () => {
      const m = document.querySelector('meta[name="csrf-token"]');
      return m ? m.getAttribute('content') : '';
    },
    get: (url) => fetch(url).then(r => r.json()),
    post: (url, data) => fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': API.csrf() },
      body: JSON.stringify(data || {}),
    }).then(r => r.json()),
    put: (url, data) => fetch(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': API.csrf() },
      body: JSON.stringify(data || {}),
    }).then(r => r.json()),
  };

  // ── DOM helper ───────────────────────────────────────────────
  const $ = (s, el) => (el || document).querySelector(s);
  const $$ = (s, el) => Array.from((el || document).querySelectorAll(s));
  const html = (strings, ...vals) => strings.reduce((a, s, i) => a + s + (vals[i] || ''), '');

  // ── Toast ────────────────────────────────────────────────────
  function toast(msg, type = 'success', duration = 3000) {
    const el = document.createElement('div');
    el.className = 'toast toast-' + type;
    el.textContent = msg;
    document.body.appendChild(el);
    setTimeout(() => {
      el.classList.add('fade-out');
      setTimeout(() => el.remove(), 300);
    }, duration);
  }

  // ── App Shell ─────────────────────────────────────────────────
  function renderShell() {
    const app = $('#app');
    // Build nav items
    const pages = state.authed ? [
      { id: 'dashboard', label: '总览', icon: '📊' },
      { id: 'schedule', label: '调度', icon: '⏱' },
      { id: 'appconfig', label: '应用配置', icon: '🧩' },
      { id: 'config', label: '控制台配置', icon: '⚙' },
      { id: 'processes', label: '进程管理', icon: '🖥' },
      { id: 'ports', label: '端口映射', icon: '🔌' },
      { id: 'health', label: '自检面板', icon: '🏥' },
      { id: 'audit', label: '审计日志', icon: '📋' },
      { id: 'settings', label: '安全设置', icon: '🔒' },
    ] : [
      { id: 'login', label: '登录', icon: '🔑' },
    ];

    app.innerHTML = html`
      <div class="app-container">
        <aside class="sidebar">
          <div class="sidebar-header">
            <h1>⚡ Admin Console</h1>
            <div class="subtitle">v6 · 管理控制台</div>
          </div>
          <nav class="sidebar-nav">
            ${pages.map(p => html`
              <button class="nav-item ${state.page === p.id ? 'active' : ''}"
                      onclick="app.navigate('${p.id}')">
                <span class="nav-icon">${p.icon}</span> ${p.label}
              </button>
            `).join('')}
          </nav>
        </aside>
        <div class="main-content">
          <div class="topbar" id="topbar"></div>
          <div class="page-area" id="page-area"></div>
        </div>
      </div>
    `;
  }

  // ── Navigation ────────────────────────────────────────────────
  window.app = window.app || {};
  app.navigate = (page) => {
    if (!state.authed && page !== 'login') {
      page = 'login';
    }
    state.page = page;
    renderShell();
    loadPage(page);
  };

  async function loadPage(page) {
    const area = $('#page-area');
    area.innerHTML = '<div class="loading"><div class="spinner"></div>加载中...</div>';

    switch (page) {
      case 'login': return renderLogin(area);
      case 'dashboard': return renderDashboard(area);
      case 'schedule': return renderSchedule(area);
      case 'appconfig': return renderAppConfig(area);
      case 'config': return renderConfig(area);
      case 'processes': return renderProcesses(area);
      case 'ports': return renderPorts(area);
      case 'health': return renderHealth(area);
      case 'audit': return renderAudit(area);
      case 'settings': return renderSettings(area);
      default: area.innerHTML = '<div class="empty-state"><div class="icon">🤷</div><p>未知页面</p></div>';
    }
  }

  // ── Login Page ───────────────────────────────────────────────
  function renderLogin(area) {
    area.innerHTML = html`
      <div class="login-page">
        <div class="login-card">
          <h1>🔐 管理控制台</h1>
          <p>个性化饮食干预课题 · 服务端管理</p>
          <div class="login-error" id="login-error"></div>
          <div class="form-field">
            <label>管理员口令</label>
            <input type="password" id="login-pw" placeholder="输入管理员口令"
                   onkeydown="if(event.key==='Enter')app.login()">
          </div>
          <button class="btn btn-primary" style="width:100%" onclick="app.login()">登录</button>
          <div class="app-version">v6.0 · Flask</div>
        </div>
      </div>
    `;
    $('#login-pw').focus();
  }

  app.login = async () => {
    const pw = $('#login-pw').value;
    const err = $('#login-error');
    const r = await API.post('/api/auth', { password: pw });
    if (r.success) {
      state.authed = true;
      state.user = r.user;
      toast('登录成功', 'success');
      app.navigate('dashboard');
    } else {
      err.textContent = r.error || '登录失败';
      toast('口令错误', 'error');
    }
  };

  app.logout = async () => {
    await API.post('/api/logout');
    state.authed = false;
    state.user = null;
    toast('已登出', 'info');
    app.navigate('login');
  };

  // ── Dashboard ─────────────────────────────────────────────────
  async function renderDashboard(area) {
    const [sys, healthData, procs] = await Promise.all([
      API.get('/api/system'),
      API.get('/api/health'),
      API.get('/api/processes'),
    ]);

    const running = procs.filter(p => p.active === 'active').length;
    const total = procs.length;
    const healthy = (healthData.databases || []).filter(d => d.readable).length;
    const dbTotal = (healthData.databases || []).length;

    area.innerHTML = html`
      <h2 style="margin-bottom:16px">📊 系统总览</h2>

      <div class="kpi-grid">
        <div class="kpi green">
          <div class="kpi-label">进程状态</div>
          <div class="kpi-value">${running}/${total}</div>
          <div class="kpi-sub">${running === total ? '全部正常' : total - running + ' 个异常'}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">数据库</div>
          <div class="kpi-value">${healthy}/${dbTotal}</div>
          <div class="kpi-sub">${healthy === dbTotal ? '全部可读写' : '有数据库异常'}</div>
        </div>
        <div class="kpi ${sys.dry_run ? 'yellow' : 'green'}">
          <div class="kpi-label">运行模式</div>
          <div class="kpi-value" style="font-size:20px">${sys.dry_run ? '🧪 DRY RUN' : '✅ 生产'}</div>
          <div class="kpi-sub">${sys.dry_run ? '模拟模式，不修改系统' : '真实环境'}</div>
        </div>
        <div class="kpi">
          <div class="kpi-label">主机名</div>
          <div class="kpi-value" style="font-size:20px">${sys.hostname || '-'}</div>
          <div class="kpi-sub">${new Date(sys.server_time).toLocaleString()}</div>
        </div>
      </div>

      <div class="row2">
        <div class="card">
          <div class="card-header"><h3>🔍 服务探活</h3></div>
          <div class="health-grid">
            ${(healthData.services || []).map(s => html`
              <div class="health-card">
                <h4>${s.label || s.name} <span class="status-dot ${s.port_open ? 'on' : 'off'}"></span></h4>
                <div class="stat">端口 ${s.port}: ${s.port_open ? '✅ 开放' : '❌ 关闭'}</div>
                <div class="stat">HTTP: ${s.http_status || '-'} (${s.http_time_ms || '-'}ms)</div>
              </div>
            `).join('')}
          </div>
        </div>

        <div class="card">
          <div class="card-header"><h3>🗄 数据库状态</h3></div>
          <div id="db-status-cards">
            ${(healthData.databases || []).map(d => html`
              <div style="padding:8px 0;border-bottom:1px solid var(--bd)">
                <div style="font-weight:500;font-size:13px">${d.label}
                  <span class="badge ${d.readable ? 'badge-success' : 'badge-danger'}">
                    ${d.readable ? '可读' : '不可读'}
                  </span>
                  <span class="badge ${d.writable ? 'badge-success' : 'badge-muted'}">
                    ${d.writable ? '可写' : '只读'}
                  </span>
                </div>
                <div style="font-size:11px;color:var(--tx3);margin-top:2px">
                  记录: ${d.record_count} · 完整性: ${d.integrity || '-'}
                </div>
              </div>
            `).join('')}
          </div>
        </div>
      </div>
    `;
  }

  // ── Config Page ───────────────────────────────────────────────
  // ── Schedule Page（C：时间项统一查看与修改）────────────────────
  async function renderSchedule(area) {
    const d = await API.get('/api/schedule');
    if (d.error) {
      area.innerHTML = html`<div class="empty-state"><div class="icon">⏱</div>
        <p>调度页不可用：${d.error}</p></div>`;
      return;
    }
    const w = d.window || {};
    const winBadge = w.waiting
      ? '<span class="badge badge-warn">等待窗口</span>'
      : '<span class="badge badge-ok">运行中</span>';
    let groupsHtml = '';
    d.groups.forEach(g => {
      let rows = g.items.map(it => {
        let input;
        if (it.type === 'bool') {
          input = html`<select data-key="${it.key}" class="sched-input">
              <option value="true" ${it.value === true ? 'selected' : ''}>true</option>
              <option value="false" ${it.value === false ? 'selected' : ''}>false</option>
            </select>`;
        } else if (it.type === 'enum') {
          input = html`<select data-key="${it.key}" class="sched-input">${
            (it.enum || []).map(v => html`<option value="${v}" ${String(it.value) === String(v) ? 'selected' : ''}>${v}</option>`).join('')
          }</select>`;
        } else {
          input = html`<input data-key="${it.key}" class="sched-input"
            value="${it.value === null || it.value === undefined ? '' : it.value}"
            placeholder="${it.type}">`;
        }
        return html`<tr>
          <td class="sched-key"><code>${it.key}</code></td>
          <td>${it.label}</td>
          <td class="sched-val">${input}</td>
          <td class="sched-unit">${it.unit}</td>
          <td class="sched-eff">${it.effect}</td>
          <td class="sched-apply">${it.apply}</td>
        </tr>`;
      }).join('');
      groupsHtml += html`<div class="card" style="margin-bottom:14px;">
        <div class="card-header"><h3>${g.title}</h3></div>
        <div class="card-body" style="overflow-x:auto;">
          <table class="data-table sched-table">
            <thead><tr><th>配置键</th><th>名称</th><th>当前值</th><th>单位</th><th>作用</th><th>生效方式</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div></div>`;
    });

    const timers = (d.timers || []).map(t => html`<li><code>${t.unit}</code> → <code>OnCalendar=${t.on_calendar}</code></li>`).join('');

    area.innerHTML = html`
      <div class="page-header"><h1>调度</h1>
        <p class="page-subtitle">共 ${d.n_time_keys} 个时间项 ｜ 配置文件：<code>${d.config_path}</code>
        ${d.writable ? '' : '（<span style="color:#e67e22">只读</span>）'} ｜ ${d.generated_at}</p></div>
      <div class="stat-cards">
        <div class="stat-card"><div class="stat-label">分析调度模式</div><div class="stat-value">${w.mode || '-'}</div>
          <div class="stat-detail">${winBadge} 窗口 ${w.window_start || '-'}–${w.window_end || '-'}（${w.timezone || '-'}）</div></div>
        <div class="stat-card"><div class="stat-label">当前是否在窗口内</div><div class="stat-value">${w.in_window ? '是' : '否'}</div>
          <div class="stat-detail">${w.now || ''}</div></div>
        <div class="stat-card"><div class="stat-label">静默时段</div><div class="stat-value">${d.quiet_hours_now ? '正在静默' : '非静默'}</div>
          <div class="stat-detail">影响告警与定时拉取，不影响 webhook 接收</div></div>
        <div class="stat-card"><div class="stat-label">时间项总数</div><div class="stat-value">${d.n_time_keys}</div>
          <div class="stat-detail">全部来自 app.yaml（单一真源）</div></div>
      </div>
      <div class="card" style="margin-bottom:14px;">
        <div class="card-header"><h3>timer 渲染预览（由 app.yaml 计算）</h3></div>
        <div class="card-body"><ul style="font-size:13px;line-height:1.9;">${timers}</ul>
        <p class="text-muted" style="font-size:12px;">保存后会重渲染 <code>*.timer.d/10-schedule.conf</code>，需 <code>systemctl daemon-reload</code> 生效。</p></div>
      </div>
      <div style="display:flex;gap:8px;margin-bottom:12px;">
        <button class="btn btn-primary" onclick="app.saveSchedule()">保存全部修改</button>
        <button class="btn" onclick="app.previewSchedule()">变更预览</button>
        <span id="sched-status" style="font-size:13px;color:#666;line-height:2;"></span>
      </div>
      ${groupsHtml}
    `;
  }

  app.collectSchedule = () => {
    const out = {};
    $$('.sched-input').forEach(el => { out[el.getAttribute('data-key')] = el.value; });
    return out;
  };

  app.saveSchedule = async () => {
    const updates = app.collectSchedule();
    const st = $('#sched-status');
    st.textContent = '保存中...';
    const r = await API.post('/api/schedule', { updates: updates, apply_timers: true });
    if (r.success) {
      const n = (r.changes || []).length;
      st.textContent = `✅ 已保存 ${n} 项，备份 ${r.backup || '-'}；timer 已重渲染（需 daemon-reload）`;
      toast('调度配置已保存', 'success');
    } else {
      st.textContent = '❌ ' + (r.error || JSON.stringify(r.errors || r));
      toast('保存失败', 'error');
    }
  };

  app.previewSchedule = async () => {
    const updates = app.collectSchedule();
    const r = await API.post('/api/schedule/preview', { updates: updates });
    const st = $('#sched-status');
    if (r.ok) {
      const changed = (r.changes || []).filter(c => String(c.before) !== String(c.after));
      st.textContent = changed.length
        ? '待变更：' + changed.map(c => `${c.key}: ${c.before} → ${c.after}`).join('；')
        : '无变更';
    } else {
      st.textContent = '❌ ' + JSON.stringify(r.errors || r);
    }
  };

  // ── App Config Page（app.yaml 全量键：查看/修改/校验/恢复默认）────
  async function renderAppConfig(area) {
    const d = await API.get('/api/appconfig');
    if (d.error) {
      area.innerHTML = html`<div class="empty-state"><div class="icon">🧩</div>
        <p>应用配置页不可用：${d.error}</p></div>`;
      return;
    }
    let groupsHtml = '';
    d.groups.forEach(g => {
      const rows = g.items.map(it => {
        let input;
        if (it.secret) {
          input = html`<span class="badge ${it.configured ? 'badge-success' : 'badge-muted'}">` +
            (it.configured ? '已配置（隐藏）' : '未配置') + `</span>` +
            html`<span class="sched-unit">　密钥请到 secrets.env 维护</span>`;
        } else if (it.enum) {
          input = html`<select data-key="${it.key}" class="sched-input">${
            it.enum.map(v => html`<option value="${v}" ${String(it.value) === String(v) ? 'selected' : ''}>${v}</option>`).join('')
          }</select>`;
        } else {
          input = html`<input data-key="${it.key}" class="sched-input"
            value="${it.value === null || it.value === undefined ? '' : it.value}"
            placeholder="${it.type}">`;
        }
        return html`<tr>
          <td class="sched-key"><code>${it.key}</code></td>
          <td>${it.type}${it.required ? ' · 必填' : ''}</td>
          <td class="sched-val">${input}</td>
          <td class="sched-unit"><code>${it.env || '—'}</code></td>
          <td class="sched-apply">${it.apply}</td>
        </tr>`;
      }).join('');
      groupsHtml += html`<div class="card" style="margin-bottom:14px;">
        <div class="card-header"><h3>${g.title} · <code>${g.ns}.*</code>（${g.items.length} 项）</h3></div>
        <div class="card-body" style="overflow-x:auto;">
          <table class="data-table sched-table">
            <thead><tr><th>配置键</th><th>类型</th><th>当前值</th><th>环境变量</th><th>生效方式</th></tr></thead>
            <tbody>${rows}</tbody>
          </table></div></div>`;
    });

    const errBox = (d.errors && d.errors.length)
      ? html`<div class="card" style="margin-bottom:12px;"><div class="card-header"><h3>当前校验错误（${d.errors.length}）</h3></div>
          <div class="card-body" style="font-size:13px;color:#e74c3c;">${d.errors.map(e => html`<div>• ${e}</div>`).join('')}</div></div>`
      : '';

    area.innerHTML = html`
      <div class="page-header"><h1>应用配置</h1>
        <p class="page-subtitle">共 ${d.n_keys} 项 ｜ 配置文件：<code>${d.config_path}</code>
        ${d.writable ? '' : '（<span style="color:#e67e22">只读</span>）'} ｜ ${d.generated_at}</p></div>
      ${errBox}
      <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;">
        <button class="btn btn-primary" onclick="app.saveAppConfig()">保存修改</button>
        <button class="btn" onclick="app.previewAppConfig()">变更预览</button>
        <button class="btn" onclick="app.restoreAppConfig()">恢复默认值</button>
        <span id="appcfg-status" style="font-size:13px;color:#666;line-height:2;"></span>
      </div>
      ${groupsHtml}
    `;
  }

  app.collectAppConfig = () => {
    const out = {};
    $$('.sched-input').forEach(el => {
      const k = el.getAttribute('data-key');
      if (k) out[k] = el.value;
    });
    return out;
  };

  app.saveAppConfig = async () => {
    const st = $('#appcfg-status');
    st.textContent = '保存中...';
    const r = await API.post('/api/appconfig', { updates: app.collectAppConfig() });
    if (r.success) {
      st.textContent = `✅ 已保存 ${(r.changes || []).length} 项；备份 ${r.backup || '-'}；${(r.rendered || []).join('；')}`;
      toast('应用配置已保存', 'success');
    } else {
      st.textContent = '❌ ' + (r.error || JSON.stringify(r.errors || r));
      toast('保存失败', 'error');
    }
  };

  app.previewAppConfig = async () => {
    const r = await API.post('/api/appconfig/preview', { updates: app.collectAppConfig() });
    const st = $('#appcfg-status');
    if (r.ok) {
      const ch = (r.changes || []).filter(c => String(c.before) !== String(c.after));
      st.textContent = ch.length ? ('待变更：' + ch.map(c => `${c.key}: ${c.before} → ${c.after}`).join('；')) : '无变更';
    } else {
      st.textContent = '❌ ' + JSON.stringify(r.errors || r);
    }
  };

  app.restoreAppConfig = async () => {
    if (!confirm('确定把 app.yaml 恢复为模板默认值？（当前文件会先备份）')) return;
    const r = await API.post('/api/appconfig/restore', { confirm: true });
    const st = $('#appcfg-status');
    st.textContent = r.success ? `✅ 已恢复默认；备份 ${r.backup}` : ('❌ ' + (r.error || '失败'));
    if (r.success) { toast('已恢复默认值', 'success'); renderAppConfig($('#page-area')); }
  };

  async function renderConfig(area) {
    const cfg = await API.get('/api/config');
    const data = cfg.data || {};

    area.innerHTML = html`
      <h2 style="margin-bottom:16px">⚙ 配置管理</h2>
      <div class="card">
        <div class="card-header">
          <h3>📝 环境变量（.env）</h3>
          <div class="btn-group">
            <button class="btn btn-sm btn-green" onclick="app.saveConfig()">💾 保存</button>
            <button class="btn btn-sm btn-yellow" onclick="app.restoreConfig()">↺ 恢复默认</button>
          </div>
        </div>
        <div id="config-fields"></div>
      </div>
    `;

    const container = $('#config-fields');
    const fieldDefs = [
      { key: 'CONSOLE_PORT', label: '控制台端口', placeholder: '9000', type: 'text' },
      { key: 'CONSOLE_HOST', label: '监听地址', placeholder: '127.0.0.1', type: 'text' },
      { key: 'APP_BASE', label: '应用基准目录', placeholder: '/opt', type: 'text' },
      { key: 'NGINX_SITES_DIR', label: 'Nginx 站点配置目录', placeholder: '/etc/nginx/sites-available', type: 'text' },
      { key: 'NGINX_ENABLED_DIR', label: 'Nginx 启用目录', placeholder: '/etc/nginx/sites-enabled', type: 'text' },
      { key: 'SMTP_HOST', label: 'SMTP 服务器', placeholder: 'mail.sjtu.edu.cn', type: 'text' },
      { key: 'SMTP_PORT', label: 'SMTP 端口', placeholder: '465', type: 'text' },
      { key: 'SMTP_USERNAME', label: 'SMTP 用户名', placeholder: '', type: 'text' },
      { key: 'SMTP_PASSWORD', label: 'SMTP 密码', placeholder: '', type: 'password' },
      { key: 'SMTP_SENDER_EMAIL', label: '发件人邮箱', placeholder: '', type: 'text' },
      { key: 'SMTP_ADMIN_EMAIL', label: '管理员邮箱（收告警）', placeholder: '', type: 'text' },
      { key: 'ALERT_TO', label: '告警收件箱', placeholder: '', type: 'text' },
      { key: 'LLM_API_KEYS', label: 'LLM API Key（逗号分隔）', placeholder: '', type: 'text' },
      { key: 'LLM_BASE_URL', label: 'LLM API 地址', placeholder: 'https://models.sjtu.edu.cn/api/v1', type: 'text' },
    ];

    container.innerHTML = fieldDefs.map(f => html`
      <div class="form-field">
        <label>${f.label}</label>
        <input type="${f.type}" id="cfg-${f.key}" value="${esc(data[f.key] || '')}"
               placeholder="${f.placeholder}">
        <div style="font-size:10px;color:var(--tx3);margin-top:2px">${f.key}</div>
      </div>
    `).join('');

    // Store secret key status
    container.innerHTML += html`
      <div class="form-field">
        <label>SECRET_KEY</label>
        <input type="password" value="${data.SECRET_KEY ? '•••••••• 已设置' : '未设置（首次启动自动生成）'}" disabled>
        <div style="font-size:10px;color:var(--tx3)">配置中不显示 SECRET_KEY 实际值</div>
      </div>
    `;
  }

  app.saveConfig = async () => {
    const fields = $$('#config-fields input');
    const updates = {};
    fields.forEach(f => {
      const key = f.id.replace('cfg-', '');
      if (f.type !== 'password' || f.value) {
        updates[key] = f.value;
      }
    });
    const r = await API.post('/api/config', updates);
    if (r.success) {
      toast('配置已保存', 'success');
    } else {
      Object.entries(r.errors || {}).forEach(([field, msg]) => {
        toast(`${field}: ${msg}`, 'error');
      });
    }
  };

  app.restoreConfig = async () => {
    if (!confirm('确认恢复默认配置？当前配置将被覆盖')) return;
    const r = await API.post('/api/config/restore');
    if (r.success) {
      toast('配置已恢复默认', 'success');
      renderConfig($('#page-area'));
    }
  };

  // ── Processes Page ────────────────────────────────────────────
  async function renderProcesses(area) {
    const procs = await API.get('/api/processes');

    area.innerHTML = html`
      <h2 style="margin-bottom:16px">🖥 进程管理</h2>
      <div class="card">
        <div class="card-header">
          <h3>服务列表</h3>
          <span style="font-size:12px;color:var(--tx3)">选中后操作</span>
        </div>
        <div id="proc-list">
          ${procs.map(p => html`
            <div class="process-item">
              <div class="status-dot ${p.active === 'active' ? 'on' : p.active === 'failed' ? 'warn' : 'off'}"></div>
              <div class="process-info">
                <div class="process-name">${p.label || p.name}
                  ${p.port > 0 ? html`<span class="badge badge-muted">:${p.port}</span>` : ''}
                </div>
                <div class="process-meta">
                  ${p.pid ? `PID: ${p.pid} · ` : ''}
                  状态: ${p.active} · 自启: ${p.enabled ? '✅' : '❌'}
                  ${p.memory_mb ? `· 内存: ${p.memory_mb} MB` : ''}
                </div>
              </div>
              <div class="process-actions">
                ${p.active !== 'active' ? html`<button class="btn btn-sm btn-green" onclick="app.procAction('${p.name}','start')">▶ 启动</button>` : ''}
                ${p.active === 'active' ? html`<button class="btn btn-sm btn-red" onclick="app.procAction('${p.name}','stop')">⏹ 停止</button>` : ''}
                <button class="btn btn-sm" onclick="app.procAction('${p.name}','restart')">🔄 重启</button>
                <button class="btn btn-sm ${p.enabled ? 'btn-yellow' : 'btn-primary'}"
                        onclick="app.procAction('${p.name}','${p.enabled ? 'disable' : 'enable'}')">
                  ${p.enabled ? '🔕 禁用' : '🔔 启用'}
                </button>
              </div>
            </div>
          `).join('')}
        </div>
      </div>
    `;
  }

  app.procAction = async (service, action) => {
    const r = await API.post(`/api/processes/${service}/${action}`);
    if (r.success) {
      toast(r.message || `${action} 成功`, 'success');
      renderProcesses($('#page-area'));
    } else {
      toast(r.error || r.message || '操作失败', 'error');
    }
  };

  // ── Ports Page ────────────────────────────────────────────────
  async function renderPorts(area) {
    const data = await API.get('/api/ports');
    const mappings = data.mappings || {};
    const statuses = data.statuses || {};

    area.innerHTML = html`
      <h2 style="margin-bottom:16px">🔌 端口映射</h2>

      <div class="card">
        <div class="card-header">
          <h3>映射列表</h3>
          <div class="btn-group">
            <button class="btn btn-sm btn-primary" onclick="app.portPreview()">👁 预览变更</button>
            <button class="btn btn-sm btn-green" onclick="app.portApply()">✅ 应用并 Reload</button>
          </div>
        </div>
        <div id="port-fields">
          ${Object.entries(mappings).map(([name, m]) => html`
            <div class="config-row">
              <div class="config-key">${m.label || name}</div>
              <div class="config-val">
                <div style="display:flex;gap:8px;flex-wrap:wrap">
                  <div class="form-field" style="flex:1;min-width:80px">
                    <label>端口</label>
                    <input type="number" id="port-${name}-port" value="${m.port > 0 ? m.port : ''}"
                           style="width:100px" ${m.port <= 0 ? 'disabled' : ''}>
                  </div>
                  <div class="form-field" style="flex:2">
                    <label>域名</label>
                    <input type="text" id="port-${name}-domain" value="${m.server_name || ''}"
                           placeholder="example.com" style="width:100%">
                  </div>
                  <div class="form-field" style="flex:1">
                    <label>路径</label>
                    <input type="text" id="port-${name}-loc" value="${m.location_path || '/'}"
                           style="width:100px">
                  </div>
                </div>
                ${statuses[name] ? html`
                  <div style="font-size:11px;color:var(--tx3);margin-top:4px">
                    端口状态: <span class="badge ${statuses[name].port_open ? 'badge-success' : 'badge-danger'}">
                      ${statuses[name].port_open ? '开放' : '关闭'}
                    </span>
                    HTTP: ${statuses[name].http_status || '-'}
                  </div>
                ` : ''}
              </div>
            </div>
          `).join('')}
        </div>
      </div>

      <div class="card" id="preview-area" style="display:none">
        <div class="card-header"><h3>📄 变更预览</h3></div>
        <div id="preview-content" style="font-family:monospace;font-size:11px;max-height:400px;overflow-y:auto"></div>
      </div>
    `;
  }

  function collectPortMappings() {
    const mappings = {};
    $$('#port-fields .config-row').forEach(row => {
      const keyLabel = $('.config-key', row).textContent;
      const portInput = $('input[id$="-port"]', row);
      const domainInput = $('input[id$="-domain"]', row);
      const locInput = $('input[id$="-loc"]', row);
      if (!portInput || !domainInput) return;
      const name = portInput.id.replace('-port', '').replace('port-', '');
      mappings[name] = {
        service: name,
        label: keyLabel,
        port: parseInt(portInput.value) || -1,
        server_name: domainInput.value.trim(),
        location_path: locInput ? locInput.value.trim() : '/',
      };
    });
    return mappings;
  }

  app.portPreview = async () => {
    const mappings = collectPortMappings();
    const r = await API.post('/api/ports/preview', { mappings });
    const area = $('#preview-area');
    area.style.display = 'block';
    const content = $('#preview-content');
    if (r.errors && r.errors.length) {
      content.innerHTML = r.errors.map(e => `<div class="diff-line diff-remove">❌ ${esc(e)}</div>`).join('');
      toast('冲突检测失败', 'error');
      return;
    }
    content.innerHTML = (r.preview_lines || []).map(line => {
      const cls = line.startsWith('+') ? 'diff-add' : line.startsWith('-') ? 'diff-remove' : 'diff-equal';
      return `<div class="diff-line ${cls}">${esc(line)}</div>`;
    }).join('') || '(无变化)';
  };

  app.portApply = async () => {
    if (!confirm('确认应用端口映射变更？将执行 nginx -t 校验后 reload')) return;
    const mappings = collectPortMappings();
    const r = await API.post('/api/ports/apply', { mappings });
    if (r.success) {
      toast(r.message || '应用成功', 'success');
      $('#preview-area').style.display = 'none';
    } else {
      toast(r.message || r.errors.join('; '), 'error');
    }
  };

  // ── Health Page ───────────────────────────────────────────────
  async function renderHealth(area) {
    const [healthData, logsList] = await Promise.all([
      API.get('/api/health'),
      API.get('/api/logs/list'),
    ]);

    area.innerHTML = html`
      <h2 style="margin-bottom:16px">🏥 自检面板</h2>

      <div class="card">
        <div class="card-header"><h3>🔍 服务探活</h3></div>
        <div class="health-grid">
          ${(healthData.services || []).map(s => html`
            <div class="health-card">
              <h4>${s.label || s.name} <span class="status-dot ${s.port_open ? 'on' : 'off'}"></span></h4>
              <div class="stat">端口: ${s.port} → ${s.port_open ? '✅ 开放' : '❌ 关闭'}</div>
              <div class="stat">HTTP 状态: ${s.http_status || '-'}</div>
              <div class="stat">响应时间: ${s.http_time_ms || '-'} ms</div>
              ${s.error ? `<div class="stat" style="color:var(--rd)">${esc(s.error)}</div>` : ''}
            </div>
          `).join('')}
        </div>
      </div>

      <div class="card">
        <div class="card-header"><h3>🗄 数据库</h3></div>
        <div class="health-grid">
          ${(healthData.databases || []).map(d => html`
            <div class="health-card">
              <h4>${d.label} <span class="status-dot ${d.readable ? 'on' : 'off'}"></span></h4>
              <div class="stat">路径: ${d.path}</div>
              <div class="stat">可读: ${d.readable ? '✅' : '❌'} · 可写: ${d.writable ? '✅' : '❌'}</div>
              <div class="stat">记录数: ${d.record_count} · 完整性: ${d.integrity || '-'}</div>
              ${d.error ? `<div class="stat" style="color:var(--rd)">${esc(d.error)}</div>` : ''}
            </div>
          `).join('')}
        </div>
      </div>

      <div class="card">
        <div class="card-header"><h3>📜 日志文件</h3></div>
        <div id="log-list">
          ${(logsList.logs || []).map(l => html`
            <div class="config-row">
              <div class="config-key" style="width:160px">${l.label || l.key}</div>
              <div class="config-val">
                <div style="font-size:11px;color:var(--tx3);margin-bottom:4px">${l.path}</div>
                <button class="btn btn-sm" onclick="app.viewLog('${l.key}')">👁 查看</button>
              </div>
            </div>
          `).join('')}
          ${(!logsList.logs || !logsList.logs.length) ? '<div class="empty-state">暂无日志文件</div>' : ''}
        </div>
      </div>
    `;
  }

  app.viewLog = async (key) => {
    const r = await API.get(`/api/logs/${key}?n=100`);
    const modal = $('.modal-overlay') || document.createElement('div');
    modal.className = 'modal-overlay show';
    modal.innerHTML = html`
      <div class="modal">
        <h2>📜 ${r.label || key}</h2>
        <div style="font-size:11px;color:var(--tx3);margin-bottom:8px">
          ${r.path} · 共 ${r.total} 行 · 显示最后 ${r.lines.length} 行
        </div>
        ${r.error ? html`<div style="color:var(--rd)">${esc(r.error)}</div>` : html`
          <div class="log-viewer">${r.lines.map(l => esc(l)).join('\n')}</div>
        `}
        <div class="modal-actions"><button class="btn" onclick="this.closest('.modal-overlay').remove()">关闭</button></div>
      </div>
    `;
    document.body.appendChild(modal);
    modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  };

  // ── Audit Page ────────────────────────────────────────────────
  async function renderAudit(area) {
    const r = await API.get('/api/audit');
    const entries = r.entries || [];

    area.innerHTML = html`
      <h2 style="margin-bottom:16px">📋 审计日志</h2>
      <div class="card">
        <div class="card-header"><h3>操作记录</h3></div>
        ${entries.length ? entries.map(line => {
          const isSuccess = line.includes('RESULT=success');
          const isFailure = line.includes('RESULT=failure') || line.includes('RESULT=fail');
          const cls = isSuccess ? 'audit-success' : isFailure ? 'audit-failure' : '';
          return html`<div class="audit-entry ${cls}">${esc(line)}</div>`;
        }).join('') : html`<div class="empty-state">暂无审计记录</div>`}
      </div>
    `;
  }

  // ── Settings Page ─────────────────────────────────────────────
  function renderSettings(area) {
    area.innerHTML = html`
      <h2 style="margin-bottom:16px">🔒 安全设置</h2>

      <div class="card">
        <div class="card-header"><h3>更改管理员口令</h3></div>
        <div class="form-field">
          <label>当前口令</label>
          <input type="password" id="old-pw" style="max-width:300px">
        </div>
        <div class="form-field">
          <label>新口令（至少 8 个字符）</label>
          <input type="password" id="new-pw" style="max-width:300px">
        </div>
        <div class="form-field">
          <label>确认新口令</label>
          <input type="password" id="confirm-pw" style="max-width:300px">
        </div>
        <button class="btn btn-primary" onclick="app.changePassword()">更新口令</button>
      </div>

      <div class="card">
        <div class="card-header"><h3>会话信息</h3></div>
        <div class="config-row">
          <div class="config-key">登录用户</div>
          <div class="config-val">${state.user || '-'}</div>
        </div>
        <div class="config-row">
          <div class="config-key">认证方式</div>
          <div class="config-val">口令哈希 (pbkdf2) + 签名 Cookie</div>
        </div>
        <div class="config-row">
          <div class="config-key">监听地址</div>
          <div class="config-val">127.0.0.1（默认，可配置）</div>
        </div>
      </div>

      <div class="card">
        <div class="card-header"><h3>操作</h3></div>
        <div class="btn-group">
          <button class="btn btn-red" onclick="app.logout()">🔓 登出</button>
        </div>
      </div>
    `;
  }

  app.changePassword = async () => {
    const oldPw = $('#old-pw').value;
    const newPw = $('#new-pw').value;
    const confirmPw = $('#confirm-pw').value;
    if (!oldPw || !newPw) { toast('请填写完整', 'error'); return; }
    if (newPw.length < 8) { toast('新口令至少 8 个字符', 'error'); return; }
    if (newPw !== confirmPw) { toast('两次口令不一致', 'error'); return; }
    const r = await API.post('/api/change_password', { old_password: oldPw, new_password: newPw });
    if (r.success) {
      toast(r.message || '口令已更新，请重新登录', 'success');
      setTimeout(() => app.logout(), 1500);
    } else {
      toast(r.error || '更新失败', 'error');
    }
  };

  // ── Init ──────────────────────────────────────────────────────
  async function init() {
    try {
      const status = await API.get('/api/auth');
      state.authed = status.authed;
      state.user = status.user;
    } catch (e) {
      state.authed = false;
    }
    state.page = state.authed ? 'dashboard' : 'login';
    renderShell();
    loadPage(state.page);
  }

  document.addEventListener('DOMContentLoaded', init);
  if (document.readyState === 'complete' || document.readyState === 'interactive') {
    init();
  }

  // ── Helper ────────────────────────────────────────────────────
  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

})();
