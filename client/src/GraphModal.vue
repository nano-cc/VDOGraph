<template>
  <div v-if="visible" class="graph-backdrop" @click="$emit('close')">
    <div class="graph-modal" @click.stop>
      <div class="graph-header">
        <span class="graph-title">{{ title }}</span>
        <span v-if="stats" class="graph-stats">{{ stats }}</span>
        <button class="graph-close" @click="$emit('close')" aria-label="关闭">✕</button>
      </div>

      <div class="graph-body">
        <!-- #85 社区划分面板（仅全局图谱）：社区列表 + 描述，点击高亮图中成员 -->
        <aside v-if="!mediaId && communities.length" class="community-panel">
          <div class="community-panel-title">社区划分 · {{ communities.length }} 个主题簇</div>
          <div class="community-list">
            <div
                v-for="(c, i) in communities"
                :key="c.id"
                class="community-item"
                :class="{ active: activeCommunity === c.id }"
                @click="focusCommunity(c)"
            >
              <div class="community-head">
                <span class="community-dot" :style="{ background: colorOf(c.id) }"></span>
                <span class="community-name">社区 {{ i + 1 }}</span>
                <span class="community-size">{{ c.entity_count }} 实体</span>
              </div>
              <div class="community-summary">{{ c.summary || '（暂无摘要）' }}</div>
            </div>
          </div>
        </aside>

        <div class="graph-canvas">
          <!-- vis-network 专用容器：Vue 永不碰它的子节点（否则响应式更新会拔掉 vis 的 canvas） -->
          <div ref="container" class="graph-net"></div>
          <div v-if="loading" class="graph-hint">图谱加载中…</div>
          <div v-else-if="empty" class="graph-hint">该范围还没有图谱数据（先完成一次图谱构建）</div>
        </div>
      </div>

      <div class="graph-footer">
        <span class="graph-tip">滚轮缩放 · 拖拽平移 · 悬停节点看描述{{ communities.length ? ' · 点左侧社区聚焦成员' : '' }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { nextTick, ref, watch, onUnmounted } from 'vue'
import { Network } from 'vis-network'
import { apiRequest } from './api'

const props = defineProps({
  visible: Boolean,
  mediaId: { type: Number, default: null },  // null = 全局图谱
  title: { type: String, default: '知识图谱' },
})
defineEmits(['close'])

const container = ref(null)
const loading = ref(false)
const empty = ref(false)
const stats = ref('')
const communities = ref([])
const activeCommunity = ref(null)
let network = null
let nodeCommunity = {}  // 节点 id -> 社区 id（聚焦用）

// 社区 id → 稳定的柔和高亮色（同社区同色）
const PALETTE = ['#c5f946', '#60a5fa', '#f472b6', '#fbbf24', '#34d399', '#a78bfa', '#f87171', '#22d3ee', '#fb923c', '#a3e635']
function colorOf(community) {
  if (!community) return '#9ca3af'
  let h = 0
  for (const ch of String(community)) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return PALETTE[h % PALETTE.length]
}

function focusCommunity(c) {
  if (!network) return
  if (activeCommunity.value === c.id) {
    activeCommunity.value = null
    network.selectNodes([])
    network.fit({ animation: true })
    return
  }
  activeCommunity.value = c.id
  const members = Object.keys(nodeCommunity).filter(id => nodeCommunity[id] === c.id)
  if (members.length) {
    network.selectNodes(members, true)
    network.focus(members[0], { scale: 1.0, animation: true })
  }
}

async function load() {
  loading.value = true
  empty.value = false
  stats.value = ''
  communities.value = []
  activeCommunity.value = null
  try {
    const url = props.mediaId ? `/kg/graph?mediaId=${props.mediaId}` : '/kg/graph'
    const res = await apiRequest(url)
    if (!res.ok) throw new Error('加载失败')
    const body = await res.json()
    const data = body.data || body

    nodeCommunity = {}
    const nodes = (data.nodes || []).map(n => {
      nodeCommunity[n.id] = n.community
      return {
        id: n.id,
        label: n.name,
        title: `${n.name}（${n.type}）\n${(n.weight || 1)} 次提及`,
        value: n.weight || 1,
        color: colorOf(n.community),
      }
    })
    const edges = (data.edges || []).map(e => ({
      from: e.source,
      to: e.target,
      title: e.description || '',
      width: Math.max(1, Math.min(4, (e.strength || 5) / 3)),
      color: { color: 'rgba(255,255,255,0.18)', highlight: 'rgba(197,249,70,0.6)' },
    }))
    if (!nodes.length) { empty.value = true; return }
    stats.value = `${nodes.length} 实体 · ${edges.length} 关系${data.truncated ? '（已按重要性截断）' : ''}`

    await nextTick()
    if (network) { network.destroy(); network = null }
    network = new Network(container.value, { nodes, edges }, {
      autoResize: true,
      nodes: {
        shape: 'dot',
        scaling: { min: 6, max: 26 },
        font: { color: '#e5e7eb', size: 13, face: 'Noto Sans SC, sans-serif', strokeWidth: 3, strokeColor: '#0b0c10' },
        borderWidth: 0,
      },
      edges: { smooth: { type: 'continuous' } },
      interaction: { hover: true, tooltipDelay: 120, zoomView: true, dragView: true },
      physics: {
        solver: 'forceAtlas2Based',
        forceAtlas2Based: { gravitationalConstant: -60, springLength: 120, damping: 0.5 },
        stabilization: { iterations: 200 },
      },
    })

    // 全局模式：加载社区划分面板
    if (!props.mediaId) {
      try {
        const cres = await apiRequest('/kg/communities')
        if (cres.ok) {
          const cbody = await cres.json()
          communities.value = (cbody.data || cbody).communities || []
        }
      } catch { /* 社区面板加载失败不阻塞图谱 */ }
    }
  } catch (e) {
    console.error('[GraphModal] load/render failed:', e)
    empty.value = true
  } finally {
    loading.value = false
  }
}

watch(() => props.visible, (v) => { if (v) load() })
onUnmounted(() => { if (network) network.destroy() })
</script>

<style scoped>
.graph-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.72); backdrop-filter: blur(6px); z-index: 300; display: flex; align-items: center; justify-content: center; }
.graph-modal { width: min(1280px, 95vw); height: min(780px, 92vh); background: #101218; border: 1px solid var(--border-tech, #2a2f3a); border-radius: 14px; display: flex; flex-direction: column; overflow: hidden; box-shadow: 0 24px 64px rgba(0,0,0,0.5); }
.graph-header { display: flex; align-items: center; gap: 12px; padding: 14px 18px; border-bottom: 1px solid var(--border-tech, #2a2f3a); }
.graph-title { font-size: 15px; font-weight: 600; color: var(--text-main, #e5e7eb); }
.graph-stats { font-size: 12px; color: var(--accent-lime, #c5f946); font-variant-numeric: tabular-nums; }
.graph-close { margin-left: auto; background: none; border: none; color: var(--text-sub, #9ca3af); font-size: 16px; cursor: pointer; padding: 4px 8px; }
.graph-close:hover { color: #ef4444; }

.graph-body { flex: 1; display: flex; min-height: 0; }

/* #85 社区面板 */
.community-panel { width: 300px; flex-shrink: 0; border-right: 1px solid var(--border-tech, #2a2f3a); display: flex; flex-direction: column; min-height: 0; }
.community-panel-title { padding: 12px 14px 8px; font-size: 12px; color: var(--accent-lime, #c5f946); border-bottom: 1px dashed var(--border-tech, #2a2f3a); }
.community-list { flex: 1; overflow-y: auto; padding: 8px; display: flex; flex-direction: column; gap: 6px; }
.community-item { padding: 8px 10px; border-radius: 8px; border: 1px solid transparent; cursor: pointer; transition: all 0.15s; }
.community-item:hover { background: rgba(255, 255, 255, 0.04); }
.community-item.active { background: rgba(197, 249, 70, 0.08); border-color: rgba(197, 249, 70, 0.35); }
.community-head { display: flex; align-items: center; gap: 7px; margin-bottom: 4px; }
.community-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.community-name { font-size: 13px; font-weight: 600; color: var(--text-main, #e5e7eb); }
.community-size { margin-left: auto; font-size: 11px; color: var(--text-sub, #9ca3af); font-variant-numeric: tabular-nums; }
.community-summary { font-size: 12px; color: var(--text-sub, #9ca3af); line-height: 1.55; display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }

.graph-canvas { flex: 1; position: relative; background: radial-gradient(ellipse at center, #14161d 0%, #0b0c10 100%); min-width: 0; }
.graph-net { position: absolute; inset: 0; }
.graph-hint { position: absolute; inset: 0; display: flex; align-items: center; justify-content: center; color: var(--text-sub, #9ca3af); font-size: 14px; z-index: 2; pointer-events: none; }
.graph-footer { padding: 8px 18px; border-top: 1px solid var(--border-tech, #2a2f3a); }
.graph-tip { font-size: 11px; color: var(--text-sub, #6b7280); }
</style>
