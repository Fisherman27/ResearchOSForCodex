const $ = id => document.getElementById(id);
const phases = ['research', 'literature', 'finalization', 'completed'];
let selected = null, timer = null, pollSeconds = 8, lastPayload = '', requestToken = 0, currentWorkspace = '';

function esc(v) { return String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function label(v) { return ({research:'检索建库',literature:'文献循环',finalization:'终段研究',completed:'已完成',awaiting_external_agent:'等待角色智能体',awaiting_stage_agent:'等待终段智能体',awaiting_research_agent:'等待检索智能体',running:'运行中',failed:'失败',blocked_research:'研究受阻',ready:'就绪',active:'工作中',unknown:'未知',scheduler_decided:'Scheduler 已决策',reading_completed:'论文阅读完成',reading_committed:'阅读结果已入库',stage_committed:'终段产物已提交',work_order_emitted:'工单已发布',relation_review_completed:'关系审阅完成',corpus_initialized:'语料库已初始化'})[v] || v || '未知'; }
function statusClass(s, stale) { return stale ? 'stale' : s === 'completed' ? 'done' : ['failed','blocked_research'].includes(s) ? 'fail' : s?.startsWith('awaiting') ? 'wait' : 'live'; }
function dt(v) { if (!v) return '—'; const d = new Date(v); return Number.isNaN(d.valueOf()) ? '—' : d.toLocaleString('zh-CN', {month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); }
function ago(seconds) { if (seconds == null) return '尚未写入时间'; if (seconds < 60) return '刚刚更新'; if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前更新`; if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前更新`; return `${Math.floor(seconds / 86400)} 天前更新`; }
function empty(text) { return `<p class="muted empty-row">${esc(text)}</p>`; }
async function json(url) { const r = await fetch(url, {cache:'no-store'}); if (!r.ok) throw Error(`${r.status} ${await r.text()}`); return r.json(); }
async function postJson(url, value) { const r = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(value), cache:'no-store'}); if (!r.ok) { let message = await r.text(); try { message = JSON.parse(message).message || message; } catch (_) {} throw Error(message); } return r.json(); }

function setConnection(kind, text) {
  $('connectionText').textContent = text;
  $('connectionDot').className = kind;
}

function runList(rows) {
  $('runs').innerHTML = rows.length ? rows.map(r => `<button class="run ${r.run_id === selected ? 'active' : ''}" data-id="${esc(r.run_id)}">
    <span class="run-line"><b>${esc(r.research_question || r.run_id)}</b><i class="mini ${statusClass(r.status, r.stale)}"></i></span>
    <small>${esc(label(r.phase))} · ${esc(r.paper_counts?.completed || 0)} 篇已读</small>
  </button>`).join('') : empty('尚无研究运行');
  document.querySelectorAll('.run').forEach(button => button.onclick = () => selectRun(button.dataset.id));
}

function renderPipeline(r) {
  let current = Math.max(0, phases.indexOf(r.phase));
  $('pipeline').innerHTML = phases.map((p, i) => `<div class="step ${i < current || r.phase === 'completed' ? 'done' : i === current ? 'active' : ''}">
    <span>${String(i + 1).padStart(2, '0')}</span><div><b>${label(p)}</b><small>${['关键词发散、检索与去重','调度、深读与关系审阅','八阶段生成与契约审核','成果与证据完整落盘'][i]}</small></div>
  </div>`).join('');
}

function renderAgents(r) {
  const waiting = new Set(r.pending_orders.map(o => (o.agent_id || '').split('.')[0]));
  const sessions = r.sessions || [];
  const roles = [
    ['lead','Lead','交互与流程协调'], ['research','Research','检索与语料收集'],
    ['organization','Organization','知识库与证据'], ['scheduler','Scheduler','阅读调度与研究判断'],
    ['read','Read','论文深读'], ['finalizer','Finalizer','终段成果生成']
  ];
  $('sessionCount').textContent = `${sessions.length} 个持久 Session`;
  $('agents').innerHTML = roles.map(([id, name, desc]) => {
    const bound = sessions.filter(s => s.role_id === id);
    const isWaiting = waiting.has(id);
    const state = isWaiting ? '等待回答' : bound.some(s => s.last_error) ? '存在错误' : bound.length ? (r.terminal ? '本轮已完成' : 'Session 已绑定') : id === 'lead' ? '当前对话' : '尚未唤醒';
    const cls = isWaiting ? 'live' : bound.some(s => s.last_error) ? 'fail' : (bound.length || id === 'lead') ? 'done' : '';
    const count = id === 'read' && bound.length ? ` · ${bound.length} 组` : '';
    return `<div class="agent ${cls}"><i></i><div><b>${name}${count}</b><small>${desc}</small></div><span>${state}</span></div>`;
  }).join('');
}

function renderReadings(r) {
  const papers = r.completed_papers || [];
  $('readingMeta').textContent = `${papers.length} 篇已完成`;
  $('readings').innerHTML = papers.length ? papers.slice().reverse().map(p => `<div class="paper">
    <span class="paper-check">✓</span><div><b>${esc(p.title || p.paper_id)}</b><small>${esc(p.paper_id)}${p.year ? ` · ${esc(p.year)}` : ''}${p.method_family ? ` · ${esc(p.method_family)}` : ''}</small></div>
  </div>`).join('') : empty('尚未产生经过验证的阅读结果');
}

function decisionText(d) {
  const decision = d.decision || d.action || d.status || d.outcome || '已完成一次调度判断';
  const reason = d.reason || d.rationale || d.notes || d.situation || '';
  return {decision, reason: typeof reason === 'string' ? reason : JSON.stringify(reason)};
}

function renderDecisions(r) {
  const rows = r.scheduler_decisions || [];
  $('decisionCount').textContent = `${rows.length} 次决策`;
  $('decisions').innerHTML = rows.length ? rows.slice().reverse().slice(0, 8).map((d, i) => {
    const text = decisionText(d);
    return `<details ${i === 0 ? 'open' : ''}><summary><span>循环 ${esc(d.cycle ?? rows.length - i - 1)}</span><b>${esc(label(text.decision))}</b></summary>${text.reason ? `<p>${esc(text.reason)}</p>` : ''}</details>`;
  }).join('') : empty('Scheduler 尚未写入调度判断');
}

function renderEvents(r) {
  const actorNames = {director:'Lead',lead:'Lead',research:'Research',organization:'Organization',scheduler:'Scheduler',read:'Read',literature:'Lead',writer:'Finalizer',system:'系统'};
  const actions = {scheduler_decided:'完成阅读调度判断',reading_completed:'完成一篇论文的阅读',reading_committed:'将阅读结果写入知识库',relation_review_completed:'完成论文与论点关系审阅',claim_relation_review_completed:'完成跨论文论点关系审阅',corpus_initialized:'完成语料库初始化',workflow_completed:'完成当前研究阶段',stage_committed:'提交一项终段研究成果',work_order_emitted:'收到新的研究任务',research_completed:'完成文献检索与语料整理',ideation_completed:'完成研究综述、缺口分析与方向设计',director_dispatch:'协调下一位智能体开始工作'};
  function sentence(e) {
    const rawActor = String(e.actor || e.agent_id || 'system').split('.')[0];
    const actor = actorNames[rawActor] || rawActor;
    let action = actions[e.event || e.type];
    if (!action && (e.event || e.type) === 'tool_called') {
      const tool = e.data?.tool || '任务';
      const toolActions = {export_evidence:'整理并导出研究证据',review_claim_relations:'审阅论文和论点关系',next_reading_tasks:'整理下一批阅读任务',propose_research_directions:'形成研究方向',run_search_pipeline:'执行文献检索'};
      action = toolActions[tool] || '完成一项内部研究操作';
    }
    return {actor, action: action || label(e.event || e.type)};
  }
  $('events').innerHTML = r.recent_events.length ? r.recent_events.slice().reverse().slice(0, 12).map(e => { const x = sentence(e); return `<div class="event"><i></i><div><b>${esc(x.actor)} · ${esc(x.action)}</b><small>${esc(dt(e.timestamp || e.created_at))}</small></div></div>`; }).join('') : empty('暂无可验证事件');
}

function renderArtifacts(r) {
  const viz = r.artifacts.find(a => a.name === 'paper_index_graph.html');
  const files = r.artifacts.filter(a => a !== viz);
  const vizButton = viz ? `<a class="viz-button" target="_blank" rel="noopener" href="/api/file?path=${encodeURIComponent(r.run_source + '/' + viz.path)}"><span>打开论文与论点关系图</span><small>交互时间轴 · 动态演示 ↗</small></a>` : '';
  const list = files.length ? files.slice().reverse().map(a => `<a class="artifact" target="_blank" rel="noopener" href="/api/file?path=${encodeURIComponent(r.run_source + '/' + a.path)}"><span>${esc(a.name)}</span><small>${Math.max(1, Math.round(a.size / 1024))} KB ↗</small></a>`).join('') : '';
  $('artifacts').innerHTML = vizButton + list || empty('产物会在通过关卡后出现');
}

const docLabels = {narrative_review:'综述正文',summary:'综合结论',synthesis:'综合判断',scope_and_coverage:'范围与覆盖',problem_landscape:'问题版图',method_evolution:'方法演进',major_findings:'主要发现',agreements:'主要共识',consensus:'主要共识',disagreements:'关键分歧',unknowns:'尚未解决',limitations_and_unknowns:'局限与未知',gaps:'研究缺口',selected_gap:'优先缺口',priority_gap:'优先缺口',prioritization_rationale:'优先级理由',evidence_refs:'证据索引',limitations:'局限',problems:'问题版图',method_families:'方法谱系',standing_evidence:'已有证据',why_not_solved:'尚未解决的原因'};
function structured(value, depth = 0) {
  if (value == null || value === '') return '';
  if (Array.isArray(value)) return value.length ? `<ul>${value.map(x => `<li>${typeof x === 'object' ? structured(x, depth + 1) : esc(x)}</li>`).join('')}</ul>` : '';
  if (typeof value !== 'object') return String(value).split(/\n\s*\n/).filter(Boolean).map(part => `<p>${esc(part)}</p>`).join('');
  return Object.entries(value).filter(([,v]) => v != null && v !== '' && (!Array.isArray(v) || v.length)).map(([k,v]) => `<section class="doc-section"><h3>${esc(docLabels[k] || k.replaceAll('_',' '))}</h3>${structured(v, depth + 1)}</section>`).join('');
}

function renderResearchInsights(r) {
  $('literatureReview').innerHTML = structured(r.literature_review) || empty('Scheduler 完成跨论文综合后将在这里显示详细综述。');
  $('researchGaps').innerHTML = structured(r.research_gaps) || empty('研究缺口尚未形成或尚未通过证据审核。');
}

function renderIdeas(r) {
  const ideas = r.ideation?.proposals || [];
  $('ideas').innerHTML = ideas.length ? `<div class="idea-label">Scheduler 提出的研究方向</div>${ideas.slice(0, 3).map(x => `<details><summary>${esc(x.title || x.proposal_id)}</summary><p>${esc(x.research_question || x.core_hypothesis || '')}</p></details>`).join('')}` : '';
}

function render(r) {
  const payload = JSON.stringify(r);
  if (payload === lastPayload) return;
  lastPayload = payload;
  $('empty').hidden = true; $('content').hidden = false;
  $('title').textContent = r.research_question || r.run_id;
  $('runId').textContent = r.run_id;
  $('status').textContent = r.stale ? '状态待确认' : label(r.status);
  $('status').className = `status ${statusClass(r.status, r.stale)}`;
  $('freshness').textContent = ago(r.age_seconds);
  $('staleBanner').hidden = !r.stale;
  $('phase').textContent = label(r.phase);
  $('stage').textContent = r.current_stage ? `当前终段：${r.current_stage}` : (r.terminal ? '流程已闭环' : '等待阶段推进');
  $('corpusCount').textContent = r.paper_counts.corpus || 0;
  $('readCount').textContent = r.paper_counts.corpus ? `${r.paper_counts.completed} / ${r.paper_counts.corpus}` : r.paper_counts.completed;
  $('cycle').textContent = r.cycle ? `已完成 ${r.cycle} 轮调度` : '尚未进入阅读循环';
  $('orderCount').textContent = r.pending_orders.length;
  $('relationCount').textContent = r.relation_review?.status === 'completed' ? `已审阅 ${r.relation_review.relations_submitted || 0} 条关系` : '关系审阅未完成';
  $('nextStep').textContent = r.next_step;
  $('updated').textContent = r.updated_at ? `磁盘记录 ${dt(r.updated_at)}` : '尚无更新时间';
  $('reportTitle').textContent = r.report_title || (r.summary ? '经过契约审核的研究结论' : '成果正在形成');
  $('summary').textContent = r.summary || '研究完成后，这里会显示经过审核的成果摘要。';
  renderPipeline(r); renderAgents(r); renderReadings(r); renderDecisions(r); renderResearchInsights(r); renderEvents(r); renderArtifacts(r); renderIdeas(r);
}

async function selectRun(id, force = false) {
  selected = id;
  const token = ++requestToken;
  try {
    const r = await json('/api/runs/' + encodeURIComponent(id));
    if (token !== requestToken) return;
    if (force) lastPayload = '';
    render(r); setConnection('online', '已连接');
    document.querySelectorAll('.run').forEach(x => x.classList.toggle('active', x.dataset.id === selected));
  } catch (error) {
    setConnection('offline', '连接中断');
    $('freshness').textContent = '保留上次成功状态';
  }
}

async function refreshWorkspace(force = false) {
  try {
    setConnection('loading', '正在同步…');
    const w = await json('/api/workspace');
    pollSeconds = Math.max(5, Number(w.poll_seconds) || 8);
    if (currentWorkspace && currentWorkspace !== w.workspace) {
      selected = null; lastPayload = ''; requestToken++;
    }
    currentWorkspace = w.workspace;
    $('workspacePath').textContent = w.workspace;
    $('workspacePath').title = w.workspace;
    $('sidebarWorkspace').textContent = w.workspace_name || w.workspace.split('/').filter(Boolean).pop();
    $('sidebarWorkspace').title = w.workspace;
    runList(w.runs);
    if (!selected && w.runs[0]) selected = w.runs[0].run_id;
    if (selected) await selectRun(selected, force);
    else setConnection('online', '已连接');
    schedule();
  } catch (error) {
    setConnection('offline', '连接中断');
    $('freshness').textContent = '保留上次成功状态';
    schedule();
  }
}

function renderWorkspaceChoices(value) {
  const rows = value.workspaces || [];
  $('workspaceChoices').innerHTML = rows.length ? rows.map(row => `<button type="button" class="workspace-choice ${row.current ? 'current' : ''}" data-path="${esc(row.path)}"><span><b>${esc(row.name)}</b><small>${esc(row.path)}</small></span><em>${row.current ? '当前' : `${row.run_count} 个运行`}</em></button>`).join('') : empty('没有检测到其他 ResearchOS 工作目录');
  document.querySelectorAll('.workspace-choice').forEach(button => button.onclick = () => {
    $('workspaceInput').value = button.dataset.path;
    document.querySelectorAll('.workspace-choice').forEach(item => item.classList.toggle('selected', item === button));
    $('workspaceError').textContent = '';
  });
}

async function openWorkspaceDialog() {
  $('workspaceError').textContent = '';
  $('workspaceInput').value = currentWorkspace;
  $('workspaceChoices').innerHTML = empty('正在检测可用工作目录…');
  $('workspaceDialog').showModal();
  try { renderWorkspaceChoices(await json('/api/workspaces')); }
  catch (error) { $('workspaceError').textContent = '无法读取工作目录列表：' + error.message; }
}

async function switchWorkspace() {
  const path = $('workspaceInput').value.trim();
  if (!path) { $('workspaceError').textContent = '请输入 ResearchOS 项目根目录。'; return; }
  $('confirmWorkspace').disabled = true;
  $('workspaceError').textContent = '';
  try {
    const result = await postJson('/api/workspace', {workspace:path});
    currentWorkspace = result.workspace; selected = null; lastPayload = ''; requestToken++;
    $('workspaceDialog').close();
    await refreshWorkspace(true);
  } catch (error) {
    $('workspaceError').textContent = error.message || '切换失败，请检查目录。';
  } finally { $('confirmWorkspace').disabled = false; }
}

function schedule() { clearTimeout(timer); timer = setTimeout(() => { if (!document.hidden) refreshWorkspace(); else schedule(); }, pollSeconds * 1000); }
$('refresh').onclick = () => refreshWorkspace(true);
$('switchWorkspace').onclick = openWorkspaceDialog;
$('confirmWorkspace').onclick = event => { event.preventDefault(); switchWorkspace(); };
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshWorkspace(true); });
document.addEventListener('keydown', e => { if (e.key.toLowerCase() === 'r' && !['INPUT','TEXTAREA'].includes(document.activeElement.tagName)) refreshWorkspace(true); });
refreshWorkspace();
