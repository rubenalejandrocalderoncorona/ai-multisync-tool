'use strict';
/** A local OpenAI-compatible server with scripted answers for every pipeline stage. Used by CLI and integration tests. */
const http = require('node:http');
const { embedText, passJudge, CODE_FACTS, PLAN, GAR_FACTS } = require('./helpers');

function startFakeOpenAI(judgeResult = passJudge) {
  const hits = { chat: 0, embed: 0, judge: 0 };
  const server = http.createServer((req, res) => {
    let body = '';
    req.on('data', (c) => (body += c));
    req.on('end', () => {
      const j = JSON.parse(body);
      res.setHeader('Content-Type', 'application/json');
      if (req.url.endsWith('/embeddings')) {
        hits.embed++;
        return res.end(JSON.stringify({ data: j.input.map((t, index) => ({ index, embedding: embedText(t) })) }));
      }
      hits.chat++;
      const sys = j.messages[0].content;
      const user = j.messages[1]?.content || '';
      let content;
      if (sys.includes('HYPOTHETICAL documentation')) { hits.garFacts = (hits.garFacts || 0) + 1; content = JSON.stringify(GAR_FACTS); }
      else if (sys.includes('You are a code analyst')) { hits.analyze = (hits.analyze || 0) + 1; content = JSON.stringify(CODE_FACTS); }
      else if (sys.includes('You are a documentation planner')) { hits.plan = (hits.plan || 0) + 1; content = JSON.stringify(PLAN); }
      else if (sys.includes('You are the JUDGE')) { hits.judge++; content = JSON.stringify(judgeResult); }
      else if (sys.includes('ONE short paragraph')) content = 'The project exposes an alert API on port 8081.';
      else if (sys.includes('single best template')) content = 'DEFAULT';
      else if (sys.includes('Classify the document')) content = 'features';
      else if (sys.includes('markdown body of the page')) content = '## Overview\n\nThe project exposes an alert API on port 8081 and supports email, slack and sms channels.\n\n## Run\n\nRun the binary and set the port.';
      else content = user;
      res.end(JSON.stringify({ choices: [{ message: { content } }] }));
    });
  });
  return new Promise((resolve) => server.listen(0, '127.0.0.1', () => resolve({ server, hits, url: `http://127.0.0.1:${server.address().port}` })));
}

module.exports = { startFakeOpenAI };
