(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const viewport = $('viewport');
  const world = $('world');
  const nodesLayer = $('nodes');
  const edgesLayer = $('edges');
  const list = $('node-list');
  const detail = $('detail');
  const authPanel = $('auth-panel');
  const search = $('search');
  const kindFilter = $('kind-filter');
  const layoutKey = 'backbone:lifecycle-map:layout:v1';
  const cardWidth = 258;
  const cardHeight = 148;
  const kindLabels = { intent: '意图', task: '任务', artifact: '制品', approval: '审批' };
  const statusLabels = {
    draft: '草稿', accepted: '已接受', rejected: '已拒绝', superseded: '已替代', completed: '已完成',
    dispatched: '已分发', in_progress: '进行中', submitted: '已提交',
    merged: '已合并', cancelled: '已取消', approved: '已批准'
  };

  let nodes = [];
  let edges = [];
  let visible = [];
  let positions = new Map();
  let customPositions = readPositions();
  let selectedId = null;
  let token = null;
  let scale = 1;
  let panX = 0;
  let panY = 0;
  let panning = null;
  let mobileViewport = matchMedia('(max-width: 760px)').matches;

  function readPositions() {
    try {
      const data = JSON.parse(localStorage.getItem(layoutKey) || '{}');
      if (!data || typeof data !== 'object' || Array.isArray(data)) return {};
      return Object.fromEntries(Object.entries(data).filter(([, point]) =>
        point && Number.isFinite(point.x) && Number.isFinite(point.y) &&
        Math.abs(point.x) <= 100000 && Math.abs(point.y) <= 100000));
    } catch { return {}; }
  }

  function savePositions() {
    try { localStorage.setItem(layoutKey, JSON.stringify(customPositions)); } catch { /* optional */ }
  }

  function el(tag, className, value) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (value !== undefined) item.textContent = String(value);
    return item;
  }

  function statusLine(message) { $('status-line').textContent = message; }
  function pointOf(id) { return customPositions[id] || positions.get(id) || { x: 70, y: 70 }; }
  function label(status) { return statusLabels[status] || status || '—'; }

  function buildGraph(data) {
    const nextNodes = [];
    const nextEdges = [];
    const nextPositions = new Map();
    const intents = [...data.intents].sort((a, b) =>
      String(a.created_at || '').localeCompare(String(b.created_at || '')) || a.id.localeCompare(b.id));
    const decisions = new Map(data.decisions.map(item => [item.id, item]));
    const tasksByIntent = new Map(intents.map(intent => [intent.id, []]));
    const decisionsByIntent = new Map(intents.map(intent => [intent.id, []]));
    for (const task of data.tasks) tasksByIntent.get(task.intent_id)?.push(task);
    for (const decision of data.decisions) {
      for (const intentId of decision.related_intents) decisionsByIntent.get(intentId)?.push(decision);
    }
    let nextY = 68;

    for (const intent of intents) {
      const tasks = tasksByIntent.get(intent.id).sort((a, b) =>
        String(a.created_at || '').localeCompare(String(b.created_at || '')) || a.id.localeCompare(b.id));
      const rowHeight = Math.max(1, tasks.length) * 192;
      const intentKey = `intent:${intent.id}`;
      nextNodes.push({ key: intentKey, group: intent.id, kind: 'intent', title: intent.proposed_outcome,
        subtitle: intent.problem, actor: intent.author, status: intent.status, source: intent,
        decisions: decisionsByIntent.get(intent.id) });
      nextPositions.set(intentKey, { x: 70, y: nextY + (rowHeight - cardHeight) / 2 });

      tasks.forEach((task, index) => {
        const y = nextY + index * 192 + 20;
        const taskKey = `task:${task.id}`;
        nextNodes.push({ key: taskKey, group: intent.id, kind: 'task', title: task.spec || task.id,
          subtitle: task.member_id, actor: task.member_id, status: task.status, source: task });
        nextPositions.set(taskKey, { x: 392, y });
        nextEdges.push({ from: intentKey, to: taskKey });

        if (!task.artifact) return;
        const artifactKey = `artifact:${task.artifact.id}`;
        nextNodes.push({ key: artifactKey, group: intent.id, kind: 'artifact', title: task.artifact.summary,
          subtitle: task.artifact.commit_sha || task.artifact.branch,
          actor: task.artifact.member_id, status: 'submitted', source: task.artifact });
        nextPositions.set(artifactKey, { x: 714, y });
        nextEdges.push({ from: taskKey, to: artifactKey });

        if (!task.approval) return;
        const review = decisions.get(task.approval.decision_id);
        const approvalKey = `approval:${task.id}`;
        nextNodes.push({ key: approvalKey, group: intent.id, kind: 'approval', title: review?.summary || '已合并制品审批',
          subtitle: task.approval.target_sha, actor: review?.author || '',
          status: 'approved', source: task.approval, review });
        nextPositions.set(approvalKey, { x: 1036, y });
        nextEdges.push({ from: artifactKey, to: approvalKey });
      });
      nextY += rowHeight + 20;
    }
    nodes = nextNodes;
    edges = nextEdges;
    positions = nextPositions;
  }

  function filteredNodes() {
    const query = search.value.trim().toLocaleLowerCase();
    const kind = kindFilter.value;
    const matchingGroups = query ? new Set(nodes.filter(node =>
      [node.key, node.title, node.subtitle, node.actor, node.status]
        .some(value => String(value || '').toLocaleLowerCase().includes(query)))
      .map(node => node.group)) : null;
    return nodes.filter(node => {
      if (kind !== 'all' && node.kind !== kind) return false;
      return !matchingGroups || matchingGroups.has(node.group);
    });
  }

  function applyTransform() {
    world.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
    $('zoom-label').textContent = `${Math.round(scale * 100)}%`;
  }

  function edgePath(from, to) {
    const x1 = from.x + cardWidth;
    const y1 = from.y + cardHeight / 2;
    const x2 = to.x - 9;
    const y2 = to.y + cardHeight / 2;
    const bend = Math.max(32, Math.abs(x2 - x1) * .42);
    return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
  }

  function drawEdges() {
    edgesLayer.replaceChildren();
    const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
    marker.setAttribute('id', 'workflow-arrow');
    marker.setAttribute('viewBox', '0 0 8 8');
    marker.setAttribute('refX', '7');
    marker.setAttribute('refY', '4');
    marker.setAttribute('markerWidth', '7');
    marker.setAttribute('markerHeight', '7');
    marker.setAttribute('orient', 'auto-start-reverse');
    const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    arrow.setAttribute('d', 'M 0 0 L 8 4 L 0 8 z');
    arrow.setAttribute('fill', '#aca69b');
    marker.append(arrow);
    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    defs.append(marker);
    edgesLayer.append(defs);
    const shown = new Set(visible.map(node => node.key));
    for (const edge of edges) {
      if (!shown.has(edge.from) || !shown.has(edge.to)) continue;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', edgePath(pointOf(edge.from), pointOf(edge.to)));
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', selectedId === edge.from || selectedId === edge.to ? '#9b5e24' : '#aca69b');
      path.setAttribute('stroke-width', selectedId === edge.from || selectedId === edge.to ? '2.5' : '1.8');
      path.setAttribute('marker-end', 'url(#workflow-arrow)');
      edgesLayer.append(path);
    }
  }

  function renderList() {
    list.replaceChildren();
    for (const node of visible) {
      const item = el('button', `list-item${node.key === selectedId ? ' is-selected' : ''}`);
      item.type = 'button';
      item.append(el('span', 'list-title', node.title),
        el('span', 'list-meta', `${kindLabels[node.kind]} · ${label(node.status)} · ${node.actor}`));
      item.addEventListener('click', () => { select(node.key); focusNode(node.key); });
      list.append(item);
    }
  }

  function section(title, value, mono = false) {
    const block = el('section', 'detail-section');
    block.append(el('h3', '', title), el('p', mono ? 'detail-mono' : '', value || '—'));
    return block;
  }

  function listSection(title, values) {
    if (!Array.isArray(values) || !values.length) return section(title, '—');
    const block = el('section', 'detail-section');
    const items = el('ul', 'detail-mono');
    for (const value of values) items.append(el('li', '', value));
    block.append(el('h3', '', title), items);
    return block;
  }

  function decisionSection(values, showRationale = true) {
    const block = el('section', 'detail-section');
    block.append(el('h3', '', '关联决策'));
    if (!values.length) { block.append(el('p', '', '—')); return block; }
    const items = el('ul', 'detail-mono');
    for (const decision of values) {
      const item = el('li');
      const link = el('a', 'detail-link', `${decision.summary} · ${label(decision.status)}`);
      link.href = `/decision-map?decision=${encodeURIComponent(decision.id)}`;
      item.append(link);
      if (showRationale) item.append(el('p', 'decision-rationale', decision.rationale));
      items.append(item);
    }
    block.append(items);
    return block;
  }

  function renderDetail() {
    detail.replaceChildren();
    const node = nodes.find(item => item.key === selectedId);
    if (!node) {
      detail.append(el('p', 'detail-placeholder', '选择一个节点，查看其来源、状态与审查证据。'));
      return;
    }
    const value = node.source;
    detail.append(el('p', 'eyebrow', kindLabels[node.kind]), el('h3', 'detail-title', node.title),
      el('span', `detail-status status-${node.status}`, label(node.status)),
      section(node.kind === 'approval' ? '所属任务' : '记录 ID',
        value.id || node.key.slice('approval:'.length), true));
    if (node.kind === 'intent') {
      detail.append(section('提出者与问题', `${value.author} · ${value.problem}`),
        section('预期结果', value.proposed_outcome),
        listSection('影响路径', value.affected_paths),
        listSection('影响符号', value.affected_symbols),
        listSection('约束', value.constraints),
        listSection('审查记录', value.reviews.map(review =>
          `${review.reviewer} · ${label(review.outcome)} · ${review.rationale}`)),
        decisionSection(node.decisions));
    } else if (node.kind === 'task') {
      detail.append(section('负责人', value.member_id), section('工作说明', value.spec),
        section('目标分支与起点', `${value.base_ref} · ${value.base_sha || '—'}`, true),
        section('任务账本版本', value.backbone_version, true),
        listSection('决策快照', value.decisions_at_fork),
        listSection('约束', value.constraints), listSection('禁止路径', value.forbidden_paths));
      if (value.cancel_reason) detail.append(section('取消原因', value.cancel_reason));
    } else if (node.kind === 'artifact') {
      detail.append(section('提交者', value.member_id), section('分支', value.branch, true),
        section('制品提交', value.commit_sha, true), section('基线提交', value.base_sha, true),
        listSection('变更路径', value.changed_paths));
      for (const [name, check] of Object.entries(value.checks || {})) {
        detail.append(section(`${name} · ${check.status || '—'}`, check.detail ||
          (check.status === 'passed' ? '确定性检查通过' : '请查看审查结论')));
      }
    } else {
      detail.append(section('审查决策 ID', value.decision_id, true),
        section('审查者', node.review?.author),
        section('目标 Git 提交', value.target_sha, true),
        section('审阅账本版本', value.reviewed_version, true),
        section('审批理由', node.review?.rationale));
      if (node.review) detail.append(decisionSection([node.review], false));
    }
    detail.append(section('创建时间', value.created_at || node.review?.created_at));
  }

  function select(id) {
    selectedId = id;
    for (const item of nodesLayer.children) {
      const selected = item.dataset.id === id;
      item.classList.toggle('is-selected', selected);
      item.setAttribute('aria-pressed', selected ? 'true' : 'false');
    }
    drawEdges();
    renderList();
    renderDetail();
  }

  function focusNode(id) {
    const point = pointOf(id);
    panX = viewport.clientWidth / 2 - (point.x + cardWidth / 2) * scale;
    panY = viewport.clientHeight / 2 - (point.y + cardHeight / 2) * scale;
    applyTransform();
  }

  function nodeFor(node) {
    const card = el('button', `decision-node workflow-node kind-${node.kind}`);
    card.type = 'button';
    card.dataset.id = node.key;
    card.setAttribute('aria-label', `${kindLabels[node.kind]}：${node.title}，${label(node.status)}`);
    card.setAttribute('aria-pressed', node.key === selectedId ? 'true' : 'false');
    if (node.key === selectedId) card.classList.add('is-selected');
    const top = el('div', 'node-top');
    top.append(el('span', 'node-kind', kindLabels[node.kind]),
      el('span', `node-status status-${node.status}`, label(node.status)));
    const footer = el('div', 'node-footer');
    footer.append(el('span', '', node.actor), el('span', '', node.subtitle));
    card.append(top, el('span', 'node-title', node.title), footer);
    const point = pointOf(node.key);
    card.style.left = `${point.x}px`;
    card.style.top = `${point.y}px`;
    let drag = null;
    card.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.stopPropagation();
      const start = pointOf(node.key);
      drag = { pointerId: event.pointerId, x: event.clientX, y: event.clientY,
        startX: start.x, startY: start.y, moved: false };
      card.setPointerCapture(event.pointerId);
    });
    card.addEventListener('pointermove', event => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (Math.hypot(dx, dy) > 4) drag.moved = true;
      if (!drag.moved) return;
      const next = { x: Math.round(drag.startX + dx / scale), y: Math.round(drag.startY + dy / scale) };
      customPositions[node.key] = next;
      card.style.left = `${next.x}px`;
      card.style.top = `${next.y}px`;
      card.classList.add('is-dragging');
      drawEdges();
    });
    const finish = event => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const moved = drag.moved;
      drag = null;
      card.classList.remove('is-dragging');
      if (moved) savePositions(); else select(node.key);
    };
    card.addEventListener('pointerup', finish);
    card.addEventListener('pointercancel', finish);
    card.addEventListener('click', event => { if (event.detail === 0) select(node.key); });
    return card;
  }

  function render() {
    visible = filteredNodes();
    $('node-count').textContent = `${visible.length}/${nodes.length}`;
    if (selectedId && !visible.some(node => node.key === selectedId)) selectedId = null;
    nodesLayer.replaceChildren(...visible.map(nodeFor));
    const points = visible.map(node => pointOf(node.key));
    const width = Math.max(1000, ...points.map(point => point.x + cardWidth + 80));
    const height = Math.max(800, ...points.map(point => point.y + cardHeight + 80));
    world.style.width = `${width}px`;
    world.style.height = `${height}px`;
    edgesLayer.setAttribute('width', width);
    edgesLayer.setAttribute('height', height);
    drawEdges();
    renderList();
    renderDetail();
    $('empty-state').hidden = visible.length > 0 || !authPanel.hidden;
    $('empty-state').querySelector('p').textContent = nodes.length
      ? '调整搜索词或类型筛选，查看其他工作记录。'
      : '创建意图后，工作将沿着任务、制品和审批逐步展开。';
    statusLine(visible.length ? `显示 ${visible.length} 个工作节点` : '没有匹配的工作记录');
  }

  function fit() {
    if (!visible.length) return;
    const points = visible.map(node => pointOf(node.key));
    const minX = Math.min(...points.map(point => point.x));
    const minY = Math.min(...points.map(point => point.y));
    const maxX = Math.max(...points.map(point => point.x + cardWidth));
    const maxY = Math.max(...points.map(point => point.y + cardHeight));
    scale = Math.min(1.15, Math.max(.3, Math.min(
      (viewport.clientWidth - 80) / (maxX - minX),
      (viewport.clientHeight - 80) / (maxY - minY))));
    panX = (viewport.clientWidth - (maxX - minX) * scale) / 2 - minX * scale;
    panY = (viewport.clientHeight - (maxY - minY) * scale) / 2 - minY * scale;
    applyTransform();
  }

  function initialView() {
    fit();
    if (scale < .75 && visible.length) {
      scale = .85;
      const first = pointOf(visible[0].key);
      panX = 24 - first.x * scale;
      panY = viewport.clientHeight / 2 - (first.y + cardHeight / 2) * scale;
      applyTransform();
    }
  }

  function zoom(factor, x = viewport.clientWidth / 2, y = viewport.clientHeight / 2) {
    const next = Math.max(.3, Math.min(2.5, scale * factor));
    const worldX = (x - panX) / scale;
    const worldY = (y - panY) / scale;
    scale = next;
    panX = x - worldX * scale;
    panY = y - worldY * scale;
    applyTransform();
  }

  async function load() {
    statusLine('正在读取工作脉络…');
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    try {
      const response = await fetch('/lifecycle-map/data', { headers, cache: 'no-store', credentials: 'omit' });
      if (response.status === 401 || response.status === 403) {
        if (response.status === 401) token = null;
        authPanel.hidden = false;
        nodes = [];
        edges = [];
        selectedId = null;
        $('version').textContent = '版本 ···';
        render();
        $('empty-state').hidden = true;
        statusLine(response.status === 401 ? '需要成员、审查者或管理员令牌' : '该令牌没有读取工作脉络图的权限');
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!Array.isArray(data.intents) || !Array.isArray(data.tasks) || !Array.isArray(data.decisions)) {
        throw new Error('无效的工作脉络数据');
      }
      authPanel.hidden = true;
      buildGraph(data);
      $('version').textContent = `版本 ${String(data.version || '未初始化').slice(0, 10)}`;
      render();
      initialView();
    } catch (error) { statusLine(`读取失败：${error.message}`); }
  }

  viewport.addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.target.closest('.decision-node, .auth-panel')) return;
    panning = { pointerId: event.pointerId, x: event.clientX, y: event.clientY,
      startX: panX, startY: panY };
    viewport.setPointerCapture(event.pointerId);
    viewport.classList.add('is-panning');
  });
  viewport.addEventListener('pointermove', event => {
    if (!panning || event.pointerId !== panning.pointerId) return;
    panX = panning.startX + event.clientX - panning.x;
    panY = panning.startY + event.clientY - panning.y;
    applyTransform();
  });
  const stopPan = event => {
    if (!panning || event.pointerId !== panning.pointerId) return;
    panning = null;
    viewport.classList.remove('is-panning');
  };
  viewport.addEventListener('pointerup', stopPan);
  viewport.addEventListener('pointercancel', stopPan);
  viewport.addEventListener('wheel', event => {
    if (event.target.closest('.decision-node, .auth-panel')) return;
    event.preventDefault();
    const bounds = viewport.getBoundingClientRect();
    zoom(event.deltaY < 0 ? 1.12 : 1 / 1.12, event.clientX - bounds.left, event.clientY - bounds.top);
  }, { passive: false });
  viewport.addEventListener('keydown', event => {
    const step = 44;
    if (event.key === 'ArrowLeft') panX += step;
    else if (event.key === 'ArrowRight') panX -= step;
    else if (event.key === 'ArrowUp') panY += step;
    else if (event.key === 'ArrowDown') panY -= step;
    else return;
    event.preventDefault();
    applyTransform();
  });

  $('fit').addEventListener('click', fit);
  $('zoom-in').addEventListener('click', () => zoom(1.2));
  $('zoom-out').addEventListener('click', () => zoom(1 / 1.2));
  $('reset-layout').addEventListener('click', () => { customPositions = {}; savePositions(); render(); initialView(); });
  $('refresh').addEventListener('click', load);
  window.addEventListener('resize', () => {
    const mobile = matchMedia('(max-width: 760px)').matches;
    if (mobile === mobileViewport) return;
    mobileViewport = mobile;
    initialView();
  });
  search.addEventListener('input', () => { render(); initialView(); });
  kindFilter.addEventListener('change', () => { render(); initialView(); });
  authPanel.addEventListener('submit', event => {
    event.preventDefault();
    token = $('bearer-token').value.trim();
    $('bearer-token').value = '';
    if (token) void load();
  });

  void load();
})();
