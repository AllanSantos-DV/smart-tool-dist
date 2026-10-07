#!/usr/bin/env node
// Uso: node wikipedia-search.mjs "consulta" [--num 8] [--lang pt]
// Saída: JSON no stdout — [{ title, url, snippet }, ...]
// API oficial da Wikipedia (MediaWiki `action=query&list=search`), não scraping de SERP --
// fonte de índice genuinamente diferente dos demais adaptadores (enciclopédica, sem
// desafio anti-bot), mas só cobre conteúdo enciclopédico -- não serve pra cotação/notícia
// do dia, por exemplo.
import { parseArgs, stripTags } from './adapter-common.mjs';

async function main() {
  const { query, num, lang } = parseArgs(process.argv.slice(2),
    'Usage: node wikipedia-search.mjs "query" [--num 8] [--lang pt]', { num: 8, lang: 'pt' });

  const url = `https://${lang}.wikipedia.org/w/api.php?` + new URLSearchParams({
    action: 'query', list: 'search', format: 'json', srlimit: String(num), srsearch: query,
  });
  const res = await fetch(url, {
    headers: { 'user-agent': process.env.SMART_TOOL_USER_AGENT || 'SmartTool (local MCP search tool)' },
    signal: AbortSignal.timeout(15000),
  });
  if (!res.ok) {
    throw new Error(`Wikipedia answered HTTP ${res.status}`);
  }
  const data = await res.json();
  const hits = data?.query?.search ?? [];
  if (hits.length === 0) {
    throw new Error(`No Wikipedia (${lang}) result for this query.`);
  }

  const results = hits.slice(0, num).map((h) => ({
    title: h.title,
    url: `https://${lang}.wikipedia.org/wiki/${encodeURIComponent(h.title.replace(/ /g, '_'))}`,
    snippet: stripTags(h.snippet || ''),
  }));
  process.stdout.write(JSON.stringify(results));
}

// Nunca process.exit() aqui: ver mesma nota em ddg-search.mjs (handle assíncrono do
// AbortSignal.timeout ainda pendente na hora de um exit() imediato crasharia o libuv
// no Windows) -- só marca o exit code e deixa o event loop drenar sozinho.
main().catch((err) => {
  console.error(String(err?.message ?? err));
  process.exitCode = 1;
});
