// Partes comuns dos adaptadores de busca em Node: texto de HTML e argumentos de linha de comando.

const ENTITIES = {
  amp: '&', lt: '<', gt: '>', quot: '"', '#39': "'", '#x27': "'", nbsp: ' ',
};

function decodeEntities(s) {
  return s.replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z0-9#]+);/g, (m, code) => {
    if (code[0] === '#') {
      const num = code[1] === 'x' || code[1] === 'X'
        ? parseInt(code.slice(2), 16)
        : parseInt(code.slice(1), 10);
      return Number.isNaN(num) ? m : String.fromCodePoint(num);
    }
    return ENTITIES[code] ?? m;
  });
}

export function stripTags(html) {
  return decodeEntities(html.replace(/<[^>]+>/g, '')).trim();
}

// `defaults` lists the accepted --flags; a numeric default makes the flag numeric.
export function parseArgs(argv, usage, defaults) {
  const values = { ...defaults };
  const queryParts = [];
  for (let i = 0; i < argv.length; i++) {
    const flag = argv[i].startsWith('--') ? argv[i].slice(2) : null;
    if (flag !== null && flag in defaults && i + 1 < argv.length) {
      const raw = argv[++i];
      values[flag] = typeof defaults[flag] === 'number' ? Number(raw) || defaults[flag] : raw;
    } else {
      queryParts.push(argv[i]);
    }
  }
  if (queryParts.length === 0) {
    throw new Error(usage);
  }
  return { query: queryParts.join(' '), ...values };
}
