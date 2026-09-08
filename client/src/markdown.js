import { marked } from 'marked'

const ALLOWED_TAGS = new Set([
  'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'P', 'BR', 'HR', 'BLOCKQUOTE',
  'UL', 'OL', 'LI', 'STRONG', 'EM', 'DEL', 'CODE', 'PRE', 'A',
  'TABLE', 'THEAD', 'TBODY', 'TR', 'TH', 'TD'
])

export function renderMarkdown(markdown) {
  if (!markdown) return ''

  let cleanText = markdown.replace(/<think>[\s\S]*?<\/think>/gi, '')
  if (cleanText.includes('</think>')) cleanText = cleanText.split('</think>').pop()
  if (!cleanText.trim()) cleanText = markdown
  cleanText = linkVideoTimestamps(cleanText)
  cleanText = linkKgCitations(cleanText)

  const template = document.createElement('template')
  template.innerHTML = marked.parse(cleanText)
  template.content.querySelectorAll('*').forEach(sanitizeNode)
  return template.innerHTML
}

function sanitizeNode(node) {
  if (!ALLOWED_TAGS.has(node.tagName)) {
    node.replaceWith(document.createTextNode(node.textContent || ''))
    return
  }

  for (const attribute of [...node.attributes]) {
    const allowed = node.tagName === 'A'
      && (attribute.name === 'href' || attribute.name === 'title')
    if (!allowed) node.removeAttribute(attribute.name)
  }
  if (node.tagName !== 'A') return

  const href = node.getAttribute('href') || ''
  if (!/^(https?:|mailto:|\/|#)/i.test(href)) node.removeAttribute('href')
  node.setAttribute('rel', 'noopener noreferrer')
  if (!href.startsWith('#video-t=') && !href.startsWith('#video-cite=')) node.setAttribute('target', '_blank')
}

function linkVideoTimestamps(markdown) {
  return markdown.replace(/\[((?:\d{1,2}:)?\d{1,2}:\d{2})\](?!\()/g, (match, timestamp) => {
    const parts = timestamp.split(':').map(Number)
    const seconds = parts.length === 3
      ? parts[0] * 3600 + parts[1] * 60 + parts[2]
      : parts[0] * 60 + parts[1]
    return `[${timestamp}](#video-t=${seconds})`
  })
}

// #74：KG 问答内嵌引用锚点。〔媒体X mm:ss〕或〔媒体X mm:ss-mm:ss〕→ #video-cite=X:SS 链接
// （LLM 两种格式都会产出，范围格式取起始时点匹配片段）
function linkKgCitations(markdown) {
  return markdown.replace(/〔媒体(\d+)\s+(\d{1,2}):(\d{2})(?:\s*-\s*(\d{1,2}):(\d{2}))?〕/g,
    (match, mediaId, mm, ss) => {
      const seconds = Number(mm) * 60 + Number(ss)
      return `[${match}](#video-cite=${mediaId}:${seconds})`
    })
}
