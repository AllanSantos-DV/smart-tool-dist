#!/usr/bin/env node
// Uso: node ddg-search.mjs "consulta" [--num 8] [--region br-pt]
// Saída: JSON no stdout — [{ title, url, snippet }, ...]
import { parseArgs, stripTags } from './adapter-common.mjs';
import { BLOCK_STATUSES, requestHeaders, withIdentities } from './identity.mjs';

/**
 * Whether the page is DuckDuckGo's anomaly challenge: it carries the anomaly modal and no result block (a
 * legitimate search without results has no modal).
 */
function isChallengePage(html) {
  return !html.includes('class="result__a"') && /anomaly-modal|anomaly\.js|bots use DuckDuckGo too/i.test(html);
}

function realUrlFromDdgHref(href) {
  const match = /uddg=([^&]+)/.exec(href);
  if (!match) return href.startsWith('//') ? `https:${href}` : href;
  try {
    return decodeURIComponent(match[1]);
  } catch {
    return match[1];
  }
}

async function main() {
  const { query, num, region } = parseArgs(process.argv.slice(2),
    'Usage: node ddg-search.mjs "query" [--num 8] [--region br-pt]', { num: 8, region: null });

  const params = new URLSearchParams({ q: query });
  if (region) params.set('kl', region);
  const url = process.env.SMART_TOOL_DDG_HTML_URL || 'https://html.duckduckgo.com/html/';
  const origin = new URL(url).origin;

  // O formulário da própria página é um POST; o GET com parâmetros é o que o desafio de anomalia (HTTP 202) pega.
  const { html } = await withIdentities('duckduckgo', async (identity, remainingMs) => {
    const res = await fetch(url, {
      method: 'POST',
      headers: requestHeaders(identity, {
        'Accept-Language': 'pt-BR,pt;q=0.9,en;q=0.8',
        'Content-Type': 'application/x-www-form-urlencoded',
        Origin: origin,
        Referer: `${origin}/`,
        'Sec-Fetch-Site': 'same-origin',
      }),
      body: params.toString(),
      signal: AbortSignal.timeout(Math.max(1000, Math.min(10000, remainingMs))),
    });
    if (BLOCK_STATUSES.has(res.status)) {
      await res.arrayBuffer();
      return { blocked: `HTTP ${res.status}` };
    }
    if (!res.ok) {
      throw new Error(`DuckDuckGo answered HTTP ${res.status}`);
    }
    const body = await res.text();
    if (isChallengePage(body)) return { blocked: 'anomaly challenge page' };
    return { html: body };
  });

  // Um resultado patrocinado usa a mesma classe `result__a` no link de título que um
  // resultado orgânico — só o container (`<div class="result ... result--ad ...">`)
  // distingue os dois. Por isso a extração é por bloco (do início de um `<div
  // class="result ` até o início do próximo), não por duas regexes globais pareadas por
  // posição: título+snippet do mesmo bloco nunca desalinha com o resultado seguinte, e um
  // bloco com `result--ad` na própria classe é descartado antes de extrair.
  const BLOCK_START = '<div class="result ';
  const startPositions = [];
  for (let i = html.indexOf(BLOCK_START); i !== -1; i = html.indexOf(BLOCK_START, i + BLOCK_START.length)) {
    startPositions.push(i);
  }

  const titleRe = /<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>([\s\S]*?)<\/a>/;
  const snippetRe = /<a[^>]*class="result__snippet"[^>]*href="[^"]+"[^>]*>([\s\S]*?)<\/a>/;

  const results = [];
  for (let i = 0; i < startPositions.length && results.length < num; i++) {
    const start = startPositions[i];
    const end = i + 1 < startPositions.length ? startPositions[i + 1] : html.length;
    const block = html.slice(start, end);

    const classEnd = block.indexOf('"', BLOCK_START.length);
    const classAttr = block.slice(BLOCK_START.length, classEnd);
    if (classAttr.split(/\s+/).includes('result--ad')) continue;

    const titleMatch = titleRe.exec(block);
    if (!titleMatch) continue;
    const snippetMatch = snippetRe.exec(block);

    results.push({
      title: stripTags(titleMatch[2]),
      url: realUrlFromDdgHref(titleMatch[1]),
      snippet: snippetMatch ? stripTags(snippetMatch[1]) : '',
    });
  }

  if (results.length === 0) {
    throw new Error('No result extracted: DuckDuckGo may have blocked or changed the HTML. Try again or rephrase the query.');
  }

  process.stdout.write(JSON.stringify(results, null, 2));
}

// Nunca chamar process.exit() aqui: com stdout/stderr em pipe (caso do
// subprocess.run que consome este script), abortar o processo enquanto ainda
// há handles assíncronos pendentes (ex.: o timer do AbortSignal.timeout do
// fetch) pode disparar um crash nativo do libuv no Windows. Só marcar o
// exit code e deixar o event loop drenar sozinho.
main().catch((err) => {
  console.error(String(err?.message ?? err));
  process.exitCode = 1;
});
