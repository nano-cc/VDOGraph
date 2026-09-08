<template>
  <div class="kg-chat">
    <!-- 会话侧栏 -->
    <aside class="kg-chat-sidebar">
      <button class="kg-session-new" @click="newSession">＋ 新会话</button>
      <div class="kg-session-list">
        <div
            v-for="s in sessions"
            :key="s.id"
            class="kg-session-item"
            :class="{ active: s.id === sessionId }"
            @click="openSession(s)"
        >
          <span class="kg-session-title">{{ s.title }}</span>
          <button class="kg-session-del" title="删除会话" @click.stop="removeSession(s)">×</button>
        </div>
        <p v-if="!sessions.length" class="kg-session-empty">还没有会话，问一个问题试试</p>
      </div>
    </aside>

    <!-- 对话主区 -->
    <div class="kg-chat-main">
      <div class="kg-header">
        <h3>知识图谱问答</h3>
        <p class="kg-sub">多 Agent 编排 · 答案可溯源到视频片段 · 支持多轮对话</p>
      </div>

      <div ref="msgListEl" class="kg-msg-list" @click="onMsgListClick">
        <template v-for="(m, i) in messages" :key="i">
          <div class="kg-msg" :class="m.role">
            <div class="kg-msg-bubble">
              <span v-if="m.role === 'user'">{{ m.content }}</span>
              <div v-else class="markdown-body" v-html="renderMarkdown(m.content)"></div>
            </div>
          </div>
          <!-- 最后一条 assistant 的溯源（历史消息不存引用，只对当次回答展示） -->
          <div v-if="m.role === 'assistant' && i === messages.length - 1 && citations.length" class="kg-citations">
            <div class="kg-cite-title">溯源 · {{ citations.length }} 个来源片段</div>
            <div class="kg-cite-list">
              <div v-for="c in citations" :key="c.segment_id" class="kg-cite-item"
                   @click="$emit('play-citation', c)" title="点击播放该片段">
                <img v-if="c.frame_urls && c.frame_urls.length" :src="c.frame_urls[0]"
                     class="kg-cite-frame" alt="片段证据帧" loading="lazy" />
                <div class="kg-cite-body">
                  <div class="kg-cite-time">媒体 {{ c.media_id }} · {{ formatMs(c.start_ms) }} - {{ formatMs(c.end_ms) }}</div>
                  <div class="kg-cite-text">{{ c.transcript_excerpt }}</div>
                </div>
              </div>
            </div>
          </div>
        </template>

        <!-- Agent 思考过程（进行中实时展示） -->
        <div v-if="loading && events.length" class="kg-thinking">
          <div class="kg-thinking-title">Agent 思考过程</div>
          <div v-for="(ev, i) in events" :key="i" class="kg-event" :class="ev.type">
            <template v-if="ev.type === 'subagent_start'">
              <span class="kg-event-icon">🤖</span>
              <span class="kg-event-name">子 Agent · {{ ev.subagent }}</span>
              <span class="kg-event-query">{{ ev.task }}</span>
            </template>
            <template v-else-if="ev.type === 'subagent_end'">
              <div class="kg-event-result">🤖 子 Agent 完成：{{ ev.result_preview }}</div>
            </template>
            <template v-else-if="ev.type === 'tool_start'">
              <span class="kg-event-icon">{{ ev.tool === 'eval' ? '🧮' : '🔍' }}</span>
              <span class="kg-event-name">{{ ev.tool }}</span>
              <span class="kg-event-query">{{ ev.query }}</span>
            </template>
            <template v-else-if="ev.type === 'tool_end'">
              <div class="kg-event-result">{{ ev.result_preview }}</div>
            </template>
          </div>
        </div>
        <div v-if="loading" class="kg-msg assistant">
          <div class="kg-msg-bubble kg-typing">思考中…</div>
        </div>
      </div>

      <p v-if="error" class="kg-error" role="alert">{{ error }}</p>

      <div class="kg-input-row">
        <input
            v-model="question"
            type="text"
            class="kg-input"
            placeholder="问点什么，支持追问（多轮上下文）"
            aria-label="知识图谱问答输入"
            :disabled="loading"
            @keyup.enter="ask"
        />
        <button class="kg-ask-btn" :disabled="loading || !question.trim()" @click="ask">
          {{ loading ? '思考中…' : '提问' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup>
import { computed, nextTick, onMounted, ref } from 'vue'
import { apiRequest, hasAuthToken } from './api'
import { renderMarkdown } from './markdown'

const emit = defineEmits(['play-citation'])

// #74：答案内嵌引用锚点点击。〔媒体X mm:ss〕→ 在 citations 中按 media_id+时间范围匹配片段播放
// 校验机制：匹配不到任何 citation 的锚点不响应（退化纯文本），杜绝幻觉锚点产生断链
function onMsgListClick(event) {
  const link = event.target.closest('a[href^="#video-cite="]')
  if (!link) return
  event.preventDefault()
  const m = link.getAttribute('href').match(/^#video-cite=(\d+):(\d+)$/)
  if (!m) return
  const mediaId = Number(m[1])
  const ms = Number(m[2]) * 1000
  const list = citations.value || []
  // 优先：同媒体且时点落在片段时间范围内；兜底：同媒体最近片段
  let target = list.find(c => Number(c.media_id) === mediaId && c.start_ms <= ms && ms <= c.end_ms)
  if (!target) {
    const sameMedia = list.filter(c => Number(c.media_id) === mediaId)
    if (sameMedia.length) {
      target = sameMedia.reduce((best, c) =>
        Math.abs(c.start_ms - ms) < Math.abs(best.start_ms - ms) ? c : best)
    }
  }
  // 历史会话没有 citations 也能播：直接用锚点的 媒体+时点 构造播放请求
  if (!target) target = { media_id: mediaId, start_ms: ms }
  emit('play-citation', target)
}

const sessions = ref([])
const sessionId = ref(null)
const messages = ref([])  // [{role, content}]
const question = ref('')
const loading = ref(false)
const error = ref('')
const events = ref([])      // 当次回答的思考过程事件流
const citations = ref([])   // 当次回答的溯源
const msgListEl = ref(null)

function formatMs(ms) {
  const totalSeconds = Math.floor((ms || 0) / 1000)
  return `${Math.floor(totalSeconds / 60)}:${String(totalSeconds % 60).padStart(2, '0')}`
}

async function scrollBottom() {
  await nextTick()
  if (msgListEl.value) msgListEl.value.scrollTop = msgListEl.value.scrollHeight
}

async function loadSessions() {
  if (!hasAuthToken()) return
  try {
    const res = await apiRequest('/kg/session/list')
    if (res.ok) sessions.value = await res.json()
  } catch { /* 列表失败不阻塞问答 */ }
}

async function newSession() {
  sessionId.value = null
  messages.value = []
  events.value = []
  citations.value = []
  error.value = ''
}

async function openSession(s) {
  sessionId.value = s.id
  events.value = []
  citations.value = []
  error.value = ''
  const res = await apiRequest(`/kg/session/messages?sessionId=${s.id}`)
  if (res.ok) {
    messages.value = await res.json()
    scrollBottom()
  }
}

async function removeSession(s) {
  await apiRequest(`/kg/session/delete?sessionId=${s.id}`, { method: 'DELETE' })
  if (sessionId.value === s.id) newSession()
  await loadSessions()
}

async function ensureSession() {
  if (sessionId.value) return sessionId.value
  const res = await apiRequest('/kg/session/create', { method: 'POST' })
  if (!res.ok) throw new Error('创建会话失败')
  const s = await res.json()
  sessionId.value = s.id
  loadSessions()
  return s.id
}

async function ask() {
  const q = question.value.trim()
  if (!q || loading.value) return
  if (!hasAuthToken()) {
    error.value = '请先登录'
    return
  }
  loading.value = true
  error.value = ''
  events.value = []
  citations.value = []
  messages.value.push({ role: 'user', content: q })
  question.value = ''
  scrollBottom()

  try {
    const sid = await ensureSession()
    const token = localStorage.getItem('authToken')
    const res = await fetch(
      `/kg/ask/stream?question=${encodeURIComponent(q)}&sessionId=${sid}`,
      { method: 'POST', headers: { Authorization: `Bearer ${token}` } }
    )
    if (!res.ok) throw new Error(`问答请求失败 (${res.status})`)

    const reader = res.body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    let answer = ''
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop()
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        let event
        try { event = JSON.parse(line.slice(6)) } catch { continue }
        if (event.type === 'final') {
          answer = event.answer || ''
          citations.value = event.citations || []
        } else if (event.type === 'error') {
          error.value = event.message || '问答失败'
        } else {
          events.value.push(event)
          scrollBottom()
        }
      }
    }
    if (answer) messages.value.push({ role: 'assistant', content: answer })
    loadSessions()  // 标题/时间刷新
  } catch (e) {
    error.value = e.message || '问答请求失败'
  } finally {
    loading.value = false
    scrollBottom()
  }
}

onMounted(loadSessions)
</script>

<style scoped>
/* #76：对齐全局暗色科技风（原来用的是浅色 element-plus 默认色，与 app 暗色背景冲突） */
.kg-chat { display: flex; gap: 16px; min-height: 420px; }
.kg-chat-sidebar { width: 200px; flex-shrink: 0; display: flex; flex-direction: column; gap: 8px; }
.kg-session-new { padding: 8px; border: 1px dashed var(--border-tech, #2a2f3a); border-radius: 8px; background: none; cursor: pointer; color: var(--text-sub, #9ca3af); transition: all 0.2s; }
.kg-session-new:hover { border-color: var(--accent-lime, #c5f946); color: var(--accent-lime, #c5f946); }
.kg-session-list { display: flex; flex-direction: column; gap: 4px; overflow-y: auto; max-height: 480px; }
.kg-session-item { display: flex; align-items: center; justify-content: space-between; padding: 8px 10px; border-radius: 8px; cursor: pointer; font-size: 13px; color: var(--text-sub, #9ca3af); transition: background 0.15s; }
.kg-session-item:hover { background: rgba(255, 255, 255, 0.05); }
.kg-session-item.active { background: rgba(197, 249, 70, 0.12); color: var(--accent-lime, #c5f946); }
.kg-session-title { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
.kg-session-del { border: none; background: none; color: var(--text-sub, #6b7280); cursor: pointer; font-size: 14px; padding: 0 4px; }
.kg-session-del:hover { color: #ef4444; }
.kg-session-empty { font-size: 12px; color: var(--text-sub, #6b7280); text-align: center; padding: 12px 0; }
.kg-chat-main { flex: 1; display: flex; flex-direction: column; min-width: 0; }
.kg-header h3 { color: var(--text-main, #e5e7eb); margin: 0 0 4px; }
.kg-sub { color: var(--text-sub, #9ca3af); font-size: 12px; margin: 0 0 10px; }
.kg-msg-list { flex: 1; overflow-y: auto; max-height: 520px; display: flex; flex-direction: column; gap: 10px; padding: 8px 4px; }
.kg-msg { display: flex; }
.kg-msg.user { justify-content: flex-end; }
.kg-msg-bubble { max-width: 78%; padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.6; }
.kg-msg.user .kg-msg-bubble { background: rgba(197, 249, 70, 0.16); color: var(--text-main, #e5e7eb); border: 1px solid rgba(197, 249, 70, 0.35); border-bottom-right-radius: 4px; }
.kg-msg.assistant .kg-msg-bubble { background: rgba(255, 255, 255, 0.05); color: var(--text-main, #d1d5db); border: 1px solid var(--border-tech, #2a2f3a); border-bottom-left-radius: 4px; }
.kg-typing { color: var(--text-sub, #9ca3af); }

/* #74：内嵌引用锚点（〔媒体X mm:ss〕）样式，与全局 #video-t= 锚点一致 */
.kg-msg-bubble :deep(a[href^="#video-cite="]) { display: inline-block; padding: 0 6px; margin: 0 2px; border: 1px solid rgba(197, 249, 70, 0.45); border-radius: 4px; color: var(--accent-lime, #c5f946); text-decoration: none; font-size: 12px; cursor: pointer; }
.kg-msg-bubble :deep(a[href^="#video-cite="]:hover) { background: var(--accent-lime, #c5f946); color: #0b0c10; }
.kg-msg-bubble :deep(a[href^="#video-t="]) { display: inline-block; padding: 0 6px; margin: 0 2px; border: 1px solid rgba(197, 249, 70, 0.45); border-radius: 4px; color: var(--accent-lime, #c5f946); text-decoration: none; font-size: 12px; }

/* #76：markdown 排版修复（列表缩进/边距/代码块/引用块） */
.kg-msg-bubble :deep(.markdown-body) { line-height: 1.7; }
.kg-msg-bubble :deep(.markdown-body p) { margin: 6px 0; }
.kg-msg-bubble :deep(.markdown-body ul),
.kg-msg-bubble :deep(.markdown-body ol) { margin: 6px 0; padding-left: 22px; }
.kg-msg-bubble :deep(.markdown-body li) { margin: 3px 0; }
.kg-msg-bubble :deep(.markdown-body li > ul),
.kg-msg-bubble :deep(.markdown-body li > ol) { margin: 2px 0; padding-left: 18px; }
.kg-msg-bubble :deep(.markdown-body h1),
.kg-msg-bubble :deep(.markdown-body h2),
.kg-msg-bubble :deep(.markdown-body h3),
.kg-msg-bubble :deep(.markdown-body h4) { margin: 10px 0 6px; color: var(--text-main, #e5e7eb); }
.kg-msg-bubble :deep(.markdown-body code) { background: rgba(255, 255, 255, 0.08); padding: 1px 5px; border-radius: 4px; font-size: 13px; }
.kg-msg-bubble :deep(.markdown-body pre) { background: rgba(0, 0, 0, 0.35); padding: 10px 12px; border-radius: 8px; overflow-x: auto; border: 1px solid var(--border-tech, #2a2f3a); }
.kg-msg-bubble :deep(.markdown-body pre code) { background: none; padding: 0; }
.kg-msg-bubble :deep(.markdown-body blockquote) { margin: 6px 0; padding: 4px 12px; border-left: 3px solid var(--accent-lime, #c5f946); color: var(--text-sub, #9ca3af); }
.kg-msg-bubble :deep(.markdown-body table) { border-collapse: collapse; margin: 8px 0; }
.kg-msg-bubble :deep(.markdown-body th),
.kg-msg-bubble :deep(.markdown-body td) { border: 1px solid var(--border-tech, #2a2f3a); padding: 5px 10px; font-size: 13px; }

/* 溯源卡片（暗色） */
.kg-citations { margin: 4px 0 10px; padding: 10px 12px; background: rgba(255, 255, 255, 0.03); border: 1px solid var(--border-tech, #2a2f3a); border-radius: 10px; }
.kg-cite-title { font-size: 12px; color: var(--accent-lime, #c5f946); margin-bottom: 8px; }
.kg-cite-list { display: flex; flex-direction: column; gap: 8px; }
.kg-cite-item { display: flex; gap: 10px; padding: 8px; border-radius: 8px; cursor: pointer; transition: background 0.15s; border: 1px solid transparent; }
.kg-cite-item:hover { background: rgba(197, 249, 70, 0.08); border-color: rgba(197, 249, 70, 0.3); }
.kg-cite-frame { width: 96px; height: 54px; object-fit: cover; border-radius: 6px; flex-shrink: 0; }
.kg-cite-time { font-size: 11px; color: var(--accent-lime, #c5f946); font-variant-numeric: tabular-nums; }
.kg-cite-text { font-size: 12px; color: var(--text-sub, #9ca3af); margin-top: 2px; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

/* Agent 思考过程（暗色） */
.kg-thinking { margin: 4px 0; padding: 10px 12px; background: rgba(255, 255, 255, 0.03); border: 1px dashed var(--border-tech, #2a2f3a); border-radius: 10px; }
.kg-thinking-title { font-size: 12px; color: var(--text-sub, #9ca3af); margin-bottom: 6px; }
.kg-event { font-size: 12px; color: var(--text-sub, #9ca3af); padding: 3px 0; display: flex; gap: 6px; align-items: baseline; }
.kg-event-name { color: var(--text-main, #d1d5db); }
.kg-event-query { color: var(--text-sub, #6b7280); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.kg-event-result { color: var(--text-sub, #9ca3af); font-size: 12px; padding: 3px 0; }

.kg-error { color: #ef4444; font-size: 13px; margin: 6px 0; }
.kg-input-row { display: flex; gap: 10px; margin-top: 10px; }
.kg-input { flex: 1; padding: 10px 14px; background: rgba(255, 255, 255, 0.04); border: 1px solid var(--border-tech, #2a2f3a); border-radius: 10px; color: var(--text-main, #e5e7eb); font-size: 14px; outline: none; transition: border-color 0.2s; }
.kg-input:focus { border-color: var(--accent-lime, #c5f946); }
.kg-input::placeholder { color: var(--text-sub, #6b7280); }
.kg-ask-btn { padding: 10px 22px; background: var(--accent-lime, #c5f946); color: #0b0c10; border: none; border-radius: 10px; font-weight: 600; cursor: pointer; transition: opacity 0.2s; }
.kg-ask-btn:disabled { opacity: 0.45; cursor: not-allowed; }
</style>
