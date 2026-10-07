#!/usr/bin/env node
// Uso: node open-websearch-adapter.mjs "consulta" [--num 8] [--engine duckduckgo]
// Saída: JSON no stdout — [{ title, url, snippet }, ...]
// Wrapper fino sobre o pacote npm `open-websearch` (scraping multi-engine, sem chave de
// API) para expor o mesmo contrato dos demais adaptadores de web_search do Smart Tool
// (ver web_search_adapters.py no smart-tool) — o CLI do pacote devolve um envelope
// {data:{results:[...],partialFailures:[...]}} com campo `description`, não o array
// {title,url,snippet} que este contrato exige.
import { execFileSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import { parseArgs } from './adapter-common.mjs';

// `npm root -g`/`npx` são `.cmd` no Windows -- `execFileSync` sem shell não invoca (EINVAL),
// e com shell:true o Node concatena argumentos sem escapar (DEP0190). O pacote fica
// instalado ao lado deste script por `install_runtime.py`; a chamada usa o arquivo JS
// diretamente com `process.execPath`. `OPEN_WEBSEARCH_CLI` permite diagnóstico local.
const LOCAL_DIR = path.dirname(fileURLToPath(import.meta.url));
const OPEN_WEBSEARCH_CLI = process.env.OPEN_WEBSEARCH_CLI || path.join(
  LOCAL_DIR, 'node_modules', 'open-websearch', 'build', 'index.js',
);

function main() {
  const { query, num, engine } = parseArgs(process.argv.slice(2),
    'Usage: node open-websearch-adapter.mjs "query" [--num 8] [--engine name]', { num: 8, engine: 'duckduckgo' });

  if (!existsSync(OPEN_WEBSEARCH_CLI)) {
    throw new Error(`open-websearch is not installed (${OPEN_WEBSEARCH_CLI} does not exist); run: python install_runtime.py`);
  }

  // O banner de log do pacote vai pro stderr (confirmado ao vivo); stdout fica só com o
  // JSON do `--json`, então captar só stdout aqui não mistura texto solto com o parse.
  let raw;
  try {
    raw = execFileSync(
      process.execPath,
      [OPEN_WEBSEARCH_CLI, 'search', query, '--engine', engine, '--limit', String(num), '--json'],
      { encoding: 'utf-8', timeout: 25000 },
    );
  } catch (err) {
    const detail = (err.stderr || '').toString().trim();
    throw new Error(detail || String(err.message || err));
  }

  const envelope = JSON.parse(raw);
  if (envelope.error) {
    throw new Error(String(envelope.error));
  }

  const results = (envelope.data?.results ?? []).slice(0, num).map((r) => ({
    title: r.title ?? '',
    url: r.url ?? '',
    snippet: r.description ?? '',
  }));

  if (results.length === 0) {
    const failures = envelope.data?.partialFailures ?? [];
    const reason = failures.length
      ? failures.map((f) => `${f.engine}: ${f.message}`).join('; ')
      : 'no results';
    throw new Error(`No result extracted (${engine}): ${reason}`);
  }

  process.stdout.write(JSON.stringify(results));
}

// Nunca process.exit() aqui: execFileSync já é síncrono (sem handle assíncrono pendente
// no momento do catch), mas o padrão do projeto (ver ddg-search.mjs) é sempre
// process.exitCode em vez de exit() com stdout/stderr em pipe.
try {
  main();
} catch (err) {
  console.error(String(err?.message ?? err));
  process.exitCode = 1;
}
