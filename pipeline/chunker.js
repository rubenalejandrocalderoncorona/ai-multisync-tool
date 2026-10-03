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
