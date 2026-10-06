(() => {
  'use strict';

  const $ = id => document.getElementById(id);
  const viewport = $('viewport');
  const world = $('world');
  const nodesLayer = $('nodes');
  const edgesLayer = $('edges');
  const list = $('decision-list');
  const detail = $('detail');
  const authPanel = $('auth-panel');
  const search = $('search');
  const statusFilter = $('status-filter');
  const layoutKey = 'backbone:decision-map:layout:v1';
  const cardWidth = 258;
  const cardHeight = 148;
  const labels = {
    proposed: '待审查', accepted: '已接受', superseded: '已替代', reverted: '已撤回'
  };

  let decisions = [];
  let visible = [];
  let automaticPositions = new Map();
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
      const value = JSON.parse(localStorage.getItem(layoutKey) || '{}');
      if (value === null || typeof value !== 'object' || Array.isArray(value)) return {};
      const valid = {};
      for (const [id, position] of Object.entries(value)) {
        if (position && Number.isFinite(position.x) && Number.isFinite(position.y) &&
            Math.abs(position.x) <= 100000 && Math.abs(position.y) <= 100000) {
          valid[id] = { x: position.x, y: position.y };
        }
      }
      return valid;
    } catch { return {}; }
  }

  function savePositions() {
    try { localStorage.setItem(layoutKey, JSON.stringify(customPositions)); } catch { /* layout is optional */ }
  }

  function el(tag, className, value) {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (value !== undefined) element.textContent = String(value);
    return element;
  }

  function safeStatus(status) {
    return Object.hasOwn(labels, status) ? status : 'proposed';
  }

  function statusLine(message) { $('status-line').textContent = message; }

  function positionOf(id) {
    return customPositions[id] || automaticPositions.get(id) || { x: 80, y: 80 };
  }

  function autoLayout() {
    const byId = new Map(decisions.map(decision => [decision.id, decision]));
    const children = new Map(decisions.map(decision => [decision.id, []]));
    for (const decision of decisions) {
      if (byId.has(decision.supersedes) && decision.supersedes !== decision.id) {
        children.get(decision.supersedes).push(decision);
      }
    }
    const order = (a, b) => String(a.created_at || '').localeCompare(String(b.created_at || '')) || a.id.localeCompare(b.id);
    for (const group of children.values()) group.sort(order);
    const roots = decisions.filter(decision => !byId.has(decision.supersedes) || decision.supersedes === decision.id).sort(order);
    const visited = new Set();
    const positions = new Map();
    let nextY = 68;
    function place(decision, depth) {
      if (visited.has(decision.id)) return positions.get(decision.id)?.y ?? nextY;
      visited.add(decision.id);
      const descendantYs = children.get(decision.id).filter(child => !visited.has(child.id)).map(child => place(child, depth + 1));
      const y = descendantYs.length ? (descendantYs[0] + descendantYs[descendantYs.length - 1]) / 2 : nextY;
      if (!descendantYs.length) nextY += 188;
      positions.set(decision.id, { x: 74 + depth * 342, y });
      return y;
    }
    for (const root of roots) place(root, 0);
    for (const decision of decisions.sort(order)) if (!visited.has(decision.id)) place(decision, 0);
    automaticPositions = positions;
  }

  function filteredDecisions() {
    const query = search.value.trim().toLocaleLowerCase();
    const status = statusFilter.value;
    return decisions.filter(decision => {
      if (status !== 'all' && decision.status !== status) return false;
      if (!query) return true;
      return [decision.id, decision.summary, decision.author, decision.decision_type]
        .some(value => String(value || '').toLocaleLowerCase().includes(query));
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
    const bend = Math.max(36, Math.abs(x2 - x1) * .45);
    return `M ${x1} ${y1} C ${x1 + bend} ${y1}, ${x2 - bend} ${y2}, ${x2} ${y2}`;
  }

  function drawEdges() {
    edgesLayer.replaceChildren();
    const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
    marker.setAttribute('id', 'decision-arrow');
    marker.setAttribute('viewBox', '0 0 8 8');
    marker.setAttribute('refX', '7');
    marker.setAttribute('refY', '4');
    marker.setAttribute('markerWidth', '7');
    marker.setAttribute('markerHeight', '7');
    marker.setAttribute('orient', 'auto-start-reverse');
    const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    arrow.setAttribute('d', 'M 0 0 L 8 4 L 0 8 z');
    arrow.setAttribute('fill', '#ada79e');
    marker.append(arrow);
    const defs = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    defs.append(marker);
    edgesLayer.append(defs);
    const shown = new Set(visible.map(decision => decision.id));
    for (const decision of visible) {
      if (!shown.has(decision.supersedes) || decision.supersedes === decision.id) continue;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', edgePath(positionOf(decision.supersedes), positionOf(decision.id)));
      path.setAttribute('fill', 'none');
      path.setAttribute('stroke', selectedId === decision.id || selectedId === decision.supersedes ? '#9b5e24' : '#ada79e');
      path.setAttribute('stroke-width', selectedId === decision.id || selectedId === decision.supersedes ? '2.5' : '1.8');
      path.setAttribute('marker-end', 'url(#decision-arrow)');
      edgesLayer.append(path);
    }
  }

  function renderList() {
    list.replaceChildren();
    const sorted = [...visible].sort((a, b) => String(b.created_at || '').localeCompare(String(a.created_at || '')) || a.id.localeCompare(b.id));
    for (const decision of sorted) {
      const item = el('button', `list-item${decision.id === selectedId ? ' is-selected' : ''}`);
      item.type = 'button';
      item.append(el('span', 'list-title', decision.summary), el('span', 'list-meta', `${labels[safeStatus(decision.status)]} · ${decision.author}`));
      item.addEventListener('click', () => { select(decision.id); focusNode(decision.id); });
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

  function renderDetail() {
    detail.replaceChildren();
    const decision = decisions.find(item => item.id === selectedId);
    if (!decision) {
      detail.append(el('p', 'detail-placeholder', '选择一项决策，查看理由、关联意图和撤回记录。'));
      return;
    }
    const status = safeStatus(decision.status);
    detail.append(el('h3', 'detail-title', decision.summary));
    detail.append(el('span', `detail-status status-${status}`, labels[status]));
    detail.append(section('决策 ID', decision.id, true));
    detail.append(section('类型与作者', `${decision.decision_type} · ${decision.author}`));
    detail.append(section('理由', decision.rationale));
    detail.append(section('创建时间', decision.created_at));
    detail.append(section('前序决策', decision.supersedes, true));
    const successors = decisions.filter(item => item.supersedes === decision.id).map(item => item.id);
    detail.append(listSection('后续决策', successors));
    detail.append(listSection('关联意图', decision.related_intents));
    detail.append(listSection('影响符号', decision.affected_symbols));
    detail.append(listSection('移除符号', decision.removes_symbols));
    detail.append(listSection('依赖符号', decision.depends_on));
    if (decision.reversion) {
      detail.append(section('撤回理由', decision.reversion.rationale));
      detail.append(section('撤回者与审阅版本', `${decision.reversion.author} · ${decision.reversion.reviewed_version}`, true));
    }
  }

  function select(id) {
    selectedId = id;
    for (const node of nodesLayer.children) {
      node.classList.toggle('is-selected', node.dataset.id === id);
      node.setAttribute('aria-pressed', node.dataset.id === id ? 'true' : 'false');
    }
    drawEdges();
    renderList();
    renderDetail();
  }

  function focusNode(id) {
    const position = positionOf(id);
    panX = viewport.clientWidth / 2 - (position.x + cardWidth / 2) * scale;
    panY = viewport.clientHeight / 2 - (position.y + cardHeight / 2) * scale;
    applyTransform();
  }

  function nodeFor(decision) {
    const status = safeStatus(decision.status);
    const node = el('button', `decision-node status-node-${status}`);
    node.type = 'button';
    node.dataset.id = decision.id;
    node.setAttribute('aria-label', `${decision.summary}，${labels[status]}`);
    node.setAttribute('aria-pressed', decision.id === selectedId ? 'true' : 'false');
    if (decision.id === selectedId) node.classList.add('is-selected');
    const top = el('div', 'node-top');
    top.append(el('span', 'node-kind', decision.decision_type), el('span', `node-status status-${status}`, labels[status]));
    const footer = el('div', 'node-footer');
    footer.append(el('span', '', decision.author), el('span', '', decision.created_at ? String(decision.created_at).slice(0, 10) : ''));
    node.append(top, el('span', 'node-title', decision.summary), footer);
    const position = positionOf(decision.id);
    node.style.left = `${position.x}px`;
    node.style.top = `${position.y}px`;
    let drag = null;
    node.addEventListener('pointerdown', event => {
      if (event.button !== 0) return;
      event.stopPropagation();
      const start = positionOf(decision.id);
      drag = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, startX: start.x, startY: start.y, moved: false };
      node.setPointerCapture(event.pointerId);
    });
    node.addEventListener('pointermove', event => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const dx = event.clientX - drag.x;
      const dy = event.clientY - drag.y;
      if (Math.hypot(dx, dy) > 4) drag.moved = true;
      if (!drag.moved) return;
      const next = { x: Math.round(drag.startX + dx / scale), y: Math.round(drag.startY + dy / scale) };
      customPositions[decision.id] = next;
      node.style.left = `${next.x}px`;
      node.style.top = `${next.y}px`;
      node.classList.add('is-dragging');
      drawEdges();
    });
    const finish = event => {
      if (!drag || event.pointerId !== drag.pointerId) return;
      const moved = drag.moved;
      drag = null;
      node.classList.remove('is-dragging');
      if (moved) savePositions();
      else select(decision.id);
    };
    node.addEventListener('pointerup', finish);
    node.addEventListener('pointercancel', finish);
    node.addEventListener('click', event => { if (event.detail === 0) select(decision.id); });
    return node;
  }

  function render() {
    visible = filteredDecisions();
    $('decision-count').textContent = `${visible.length}/${decisions.length}`;
    if (selectedId && !visible.some(decision => decision.id === selectedId)) selectedId = null;
    nodesLayer.replaceChildren(...visible.map(nodeFor));
    const positions = visible.map(decision => positionOf(decision.id));
    const width = Math.max(1000, ...positions.map(position => position.x + cardWidth + 80));
    const height = Math.max(800, ...positions.map(position => position.y + cardHeight + 80));
    world.style.width = `${width}px`;
    world.style.height = `${height}px`;
    edgesLayer.setAttribute('width', width);
    edgesLayer.setAttribute('height', height);
    drawEdges();
    renderList();
    renderDetail();
    $('empty-state').hidden = visible.length > 0 || !authPanel.hidden;
    $('empty-state').querySelector('p').textContent = decisions.length
      ? '调整搜索词或状态筛选，查看其他决策。'
      : '创建决策后，这里会显示它与前序决策的关系。';
    statusLine(visible.length ? `显示 ${visible.length} 项决策` : '没有匹配的决策');
  }

  function clearData() {
    decisions = [];
    selectedId = null;
    $('version').textContent = '版本 ···';
    render();
  }

  function fit() {
    if (!visible.length) return;
    const points = visible.map(decision => positionOf(decision.id));
    const minX = Math.min(...points.map(point => point.x));
    const minY = Math.min(...points.map(point => point.y));
    const maxX = Math.max(...points.map(point => point.x + cardWidth));
    const maxY = Math.max(...points.map(point => point.y + cardHeight));
    scale = Math.min(1.15, Math.max(.35, Math.min((viewport.clientWidth - 80) / (maxX - minX), (viewport.clientHeight - 80) / (maxY - minY))));
    panX = (viewport.clientWidth - (maxX - minX) * scale) / 2 - minX * scale;
    panY = (viewport.clientHeight - (maxY - minY) * scale) / 2 - minY * scale;
    applyTransform();
  }

  function initialView() {
    if (matchMedia('(max-width: 760px)').matches && visible.length) {
      scale = .85;
      focusNode(visible[0].id);
    } else {
      fit();
    }
  }

  function zoom(factor, x = viewport.clientWidth / 2, y = viewport.clientHeight / 2) {
    const next = Math.max(.35, Math.min(2.5, scale * factor));
    const worldX = (x - panX) / scale;
    const worldY = (y - panY) / scale;
    scale = next;
    panX = x - worldX * scale;
    panY = y - worldY * scale;
    applyTransform();
  }

  async function load() {
    statusLine('正在读取决策…');
    const headers = token ? { Authorization: `Bearer ${token}` } : {};
    try {
      const response = await fetch('/decision-map/data', { headers, cache: 'no-store', credentials: 'omit' });
      if (response.status === 401) {
        token = null;
        authPanel.hidden = false;
        clearData();
        $('empty-state').hidden = true;
        statusLine('需要成员、审查者或管理员令牌');
        return;
      }
      if (response.status === 403) {
        authPanel.hidden = false;
        clearData();
        $('empty-state').hidden = true;
        statusLine('该令牌没有读取决策脉络图的权限');
        return;
      }
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      if (!Array.isArray(data.decisions)) throw new Error('无效的决策数据');
      authPanel.hidden = true;
      decisions = data.decisions.filter(item => item && typeof item.id === 'string');
      $('version').textContent = `版本 ${String(data.version || '未初始化').slice(0, 10)}`;
      autoLayout();
      render();
      const linkedId = new URLSearchParams(window.location.search).get('decision');
      if (linkedId && visible.some(item => item.id === linkedId)) {
        select(linkedId);
        focusNode(linkedId);
      } else initialView();
    } catch (error) {
      statusLine(`读取失败：${error.message}`);
    }
  }

  viewport.addEventListener('pointerdown', event => {
    if (event.button !== 0 || event.target.closest('.decision-node, .auth-panel')) return;
    panning = { pointerId: event.pointerId, x: event.clientX, y: event.clientY, startX: panX, startY: panY };
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
  search.addEventListener('input', () => { render(); fit(); });
  statusFilter.addEventListener('change', () => { render(); fit(); });
  authPanel.addEventListener('submit', event => {
    event.preventDefault();
    token = $('bearer-token').value.trim();
    $('bearer-token').value = '';
    if (token) void load();
  });

  void load();
})();
