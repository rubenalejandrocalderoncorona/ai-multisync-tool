'use strict';
const crypto = require('crypto');

/** Split Markdown on headings, then pack paragraphs up to maxChars. */
function chunkMarkdown(content, { maxChars = 1200 } = {}) {
  const body = content.replace(/^---\n[\s\S]*?\n---\n/, '');
  const sections = body.split(/^(?=#{1,4}\s)/m).filter((s) => s.trim());
  const chunks = [];
  for (const section of sections) {
    const heading = (section.match(/^#{1,4}\s+(.+)$/m) || [])[1] || '';
    let buf = '';
    for (const para of section.split(/\n{2,}/)) {
      if (buf && buf.length + para.length > maxChars) {
        chunks.push({ heading, text: buf.trim() });
        buf = '';
      }
      buf += (buf ? '\n\n' : '') + para;
    }
    if (buf.trim()) chunks.push({ heading, text: buf.trim() });
  }
  return chunks;
}

/** Deterministic UUID-shaped id so re-indexing a chunk upserts instead of duplicating. */
function pointId(repo, filePath, idx) {
  const h = crypto.createHash('sha1').update(`${repo}\0${filePath}\0${idx}`).digest('hex');
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20, 32)}`;
}

module.exports = { chunkMarkdown, pointId };

/**
 * Split source code into overlapping line windows, preferring to cut at blank lines so functions
 * stay together. Each chunk is prefixed with its location so a retrieved chunk is self-describing.
 */
function chunkCode(filePath, content, { maxLines = 80, overlap = 10, maxChars = 3200 } = {}) {
  const lines = content.split('\n');
  const chunks = [];
  let start = 0;
  while (start < lines.length) {
    let end = Math.min(start + maxLines, lines.length);
    if (end < lines.length) {
      // look back up to 15 lines for a blank line to cut on
      for (let i = end; i > Math.max(start + maxLines - 15, start + 1); i--) {
        if (!lines[i - 1].trim()) { end = i; break; }
      }
    }
    let text = lines.slice(start, end).join('\n');
    if (text.length > maxChars) text = `${text.slice(0, maxChars)}\n[truncated]`;
    if (text.trim()) chunks.push({ start: start + 1, end, text: `FILE ${filePath} lines ${start + 1}-${end}\n${text}` });
    if (end >= lines.length) break;
    start = Math.max(end - overlap, start + 1);
  }
  return chunks;
}

module.exports.chunkCode = chunkCode;
