"""Interface language: Brazilian Portuguese or English, following the Windows display language until the user picks
one with the language button (config `language`).

Strings are written in English in the code and the English text is the catalog key (the gettext convention): a
missing Portuguese entry shows the English text instead of breaking the screen, and tests/test_i18n.py fails when a
user-visible string has no entry. The same catalog serves Python (tray, notifications, update screen) and the web
pages, where PAGE_SCRIPT translates the rendered DOM and whatever the page scripts insert afterwards.

Agent-facing text (MCP tool answers, hook messages) stays in English: agents read it, users do not."""
import json
import os
import re
import sys

import config

ROOT = os.path.dirname(os.path.abspath(__file__))
LOCALES_DIR = os.path.join(ROOT, "locales")
PORTUGUESE_PRIMARY_LANGID = 0x16
_CATALOGS = {}


def system_language():
    """ "pt-BR" when the Windows display language is any Portuguese, else "en"."""
    if sys.platform != "win32":
        return "en"
    import ctypes
    return "pt-BR" if ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF == PORTUGUESE_PRIMARY_LANGID else "en"


def language(cfg=None):
    """The language the interface shows now: the user's choice, else the Windows display language."""
    return config.language(cfg) or system_language()


def set_language(value):
    """Saves the user's choice ("pt-BR" or "en") and returns it; raises ValueError for any other value."""
    if value not in config.LANGUAGES:
        raise ValueError(f"Unknown language {value!r}: use one of {', '.join(config.LANGUAGES)}.")
    config.update_config(lambda cfg: cfg.__setitem__("language", value))
    return value


def catalog(lang):
    """English text -> translated text for lang; English itself has an empty catalog. Raises when the locale file is
    missing or malformed: a broken install must not pass for an untranslated one."""
    if lang == "en":
        return {}
    if lang not in _CATALOGS:
        with open(os.path.join(LOCALES_DIR, f"{lang}.json"), encoding="utf-8") as stream:
            data = json.load(stream)
        messages = data["messages"]
        if not isinstance(messages, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in messages.items()):
            raise ValueError(f"locales/{lang}.json: 'messages' must map text to text.")
        _CATALOGS[lang] = messages
    return _CATALOGS[lang]


_PLACEHOLDER = re.compile(r"\{(\w*)\}")
_WORD = re.compile(r"[A-Za-z]")
_SENTENCE_BREAK = re.compile(r"(?<=[.!?…])\s+(?=[A-Z])")


def _normal(text):
    return " ".join(text.split())


def _fragment_key(key):
    """Keys that may be replaced inside a longer text: two words or more, or one word ending in punctuation."""
    return not _PLACEHOLDER.search(key) and (" " in key or (len(key) >= 4 and key[-1] in ".:!?…"))


def _bounded(fragment):
    """Regex of a phrase that never matches inside a word or a file name: a boundary before it when it starts with a
    letter, after it when it ends with a letter or sentence punctuation ("Saved." is not "Saved.txt"); phrases cut
    from a sentence may start or end with other punctuation ("Accept selected (" before a count)."""
    return ((r"(?<!\w)" if re.match(r"\w", fragment) else "") + re.escape(fragment)
            + (r"(?!\w)" if re.search(r"[\w.:!?…]$", fragment) else ""))


class Translator:
    """Translates finished English text with one catalog: the whole text, then a template with placeholders
    ("Updated from {old} to {new}."), then each sentence on its own when there are several, then known phrases inside
    it. PAGE_SCRIPT runs the same steps."""

    def __init__(self, messages):
        self.messages = {_normal(k): v for k, v in messages.items()}
        self.templates = []
        for key, value in self.messages.items():
            literal = _PLACEHOLDER.sub("", key)
            if _PLACEHOLDER.search(key) and len(_WORD.findall(literal)) >= 3:
                parts = _PLACEHOLDER.split(key)
                pattern = "".join(re.escape(part) if i % 2 == 0 else f"(?P<p{i // 2}>.+?)" for i, part in enumerate(parts))
                names = parts[1::2]
                self.templates.append((re.compile(f"^{pattern}$", re.S), names, value))
        self.templates.sort(key=lambda item: -len(item[0].pattern))
        fragments = sorted((k for k in self.messages if _fragment_key(k)), key=len, reverse=True)
        self.fragments = (re.compile("|".join(_bounded(f) for f in fragments)) if fragments else None)

    def __call__(self, text):
        """text translated, or text unchanged when nothing in the catalog matches it."""
        key = _normal(text)
        if not key or not self.messages:
            return text
        if key in self.messages:
            return self.messages[key]
        for pattern, names, value in self.templates:
            found = pattern.match(key)
            if found:
                values = {name: found.group(f"p{i}") for i, name in enumerate(names)}
                return _PLACEHOLDER.sub(lambda m: self(values.get(m.group(1), m.group(0))), value)
        sentences = _SENTENCE_BREAK.split(key)
        if len(sentences) > 1:
            return " ".join(self(sentence) for sentence in sentences)
        if self.fragments:
            return self.fragments.sub(lambda m: self.messages[m.group(0)], text)
        return text


_TRANSLATORS = {}


def translator(lang=None):
    """The Translator of lang (default: the current interface language)."""
    lang = lang or language()
    if lang not in _TRANSLATORS:
        _TRANSLATORS[lang] = Translator(catalog(lang))
    return _TRANSLATORS[lang]


def t(text, lang=None, **values):
    """text (an English catalog key) in the interface language, with its {placeholders} filled from values."""
    translated = translator(lang)(text)
    return translated.format(**values) if values else translated


def N_(text):
    """Marks text as a catalog key without translating it yet (gettext_noop): for tables built at import time,
    translated with t() when shown."""
    return text


def page_script(token, lang=None):
    """The <script> the web pages carry in <head>: catalog, current language, the setup token the language button
    sends, translator and language button."""
    lang = lang or language()
    data = {"lang": lang, "token": token, "messages": catalog(lang)}
    return "<script>window.SMART_TOOL_I18N=" + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + ";</script>\n" \
        + "<script>" + PAGE_SCRIPT + "</script>"


PAGE_SCRIPT = r"""
(()=>{
const I=window.SMART_TOOL_I18N,PH=/\{(\w*)\}/g,SKIP=new Set(['SCRIPT','STYLE','CODE','PRE','TEXTAREA','KBD','SAMP','svg','SVG']);
const INLINE=new Set(['STRONG','EM','B','I','A','CODE','BR','SPAN','SMALL','KBD','ABBR','SUP','SUB','U','MARK']);
const ATTRS=['title','placeholder','aria-label','alt'];
const norm=s=>s.replace(/\s+/g,' ').trim();
const esc=s=>s.replace(/[.*+?^${}()|[\]\\]/g,'\\$&');
const messages={};for(const [k,v] of Object.entries(I.messages))messages[norm(k)]=v;
const templates=[];
for(const [k,v] of Object.entries(messages)){if(!/\{\w*\}/.test(k)||(k.replace(PH,'').match(/[A-Za-z]/g)||[]).length<3)continue;
  const parts=k.split(PH),names=[];let src='';parts.forEach((p,i)=>{if(i%2){names.push(p);src+='(.+?)'}else src+=esc(p)});
  templates.push([new RegExp('^'+src+'$','s'),names,v])}
templates.sort((a,b)=>b[0].source.length-a[0].source.length);
const frag=Object.keys(messages).filter(k=>!/\{\w*\}/.test(k)&&(k.includes(' ')||(k.length>=4&&/[.:!?…]$/.test(k)))).sort((a,b)=>b.length-a.length);
const bounded=f=>(/^\w/.test(f)?'(?<!\\w)':'')+esc(f)+(/[\w.:!?…]$/.test(f)?'(?!\\w)':'');
const fragRe=frag.length?new RegExp(frag.map(bounded).join('|'),'g'):null;
function tr(text){const key=norm(text);if(!key||I.lang==='en')return text;
  if(key in messages){const lead=text.match(/^\s*/)[0],tail=text.match(/\s*$/)[0];return lead+messages[key]+tail}
  for(const [re,names,v] of templates){const m=key.match(re);if(m){const vals={};names.forEach((n,i)=>vals[n]=m[i+1]);return v.replace(PH,(all,n)=>n in vals?tr(vals[n]):all)}}
  const sentences=key.split(/(?<=[.!?…])\s+(?=[A-Z])/);if(sentences.length>1)return text.match(/^\s*/)[0]+sentences.map(tr).join(' ')+text.match(/\s*$/)[0];
  return fragRe?text.replace(fragRe,m=>messages[m]):text}
const written=new WeakMap();
function skipped(el){return !el||!el.closest||el.closest('[data-no-i18n],script,style,noscript,code,pre,textarea,kbd,samp,svg')}
function translateElement(el){
  if(SKIP.has(el.tagName)||skipped(el))return false;
  for(const a of ATTRS){const v=el.getAttribute(a);if(v&&written.get(el)?.[a]!==v){const t=tr(v);if(t!==v){el.setAttribute(a,t);written.set(el,{...(written.get(el)||{}),[a]:t})}}}
  if(el.childElementCount&&[...el.children].every(c=>INLINE.has(c.tagName)&&!c.id&&!Object.keys(c.dataset).length)&&[...el.childNodes].some(n=>n.nodeType===3&&n.data.trim())){
    const key=norm(el.innerHTML);if(key in messages){el.innerHTML=messages[key];return true}}
  return false}
function translateText(node){if(skipped(node.parentElement)||written.get(node)===node.data)return;const t=tr(node.data);if(t!==node.data){node.data=t;written.set(node,t)}}
function walk(root){if(I.lang==='en')return;
  if(root.nodeType===3){translateText(root);return}
  if(root.nodeType!==1)return;
  if(translateElement(root))return;
  const it=document.createTreeWalker(root,NodeFilter.SHOW_ELEMENT|NodeFilter.SHOW_TEXT,{acceptNode:n=>n.nodeType===1&&(SKIP.has(n.tagName)||n.hasAttribute('data-no-i18n'))?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
  const nodes=[];for(let n=it.nextNode();n;n=it.nextNode())nodes.push(n);
  const done=new Set();
  for(const n of nodes){if([...done].some(d=>d.contains(n)))continue;if(n.nodeType===1){if(translateElement(n))done.add(n)}else translateText(n)}}
function applyTitle(){if(I.lang!=='en')document.title=tr(document.title)}
new MutationObserver(list=>{if(I.lang==='en')return;for(const m of list){
  if(m.type==='characterData')translateText(m.target);
  else if(m.type==='attributes')translateElement(m.target);
  else for(const n of m.addedNodes)walk(n)}}).observe(document.documentElement,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:ATTRS});
const nativeConfirm=window.confirm.bind(window),nativeAlert=window.alert.bind(window),nativePrompt=window.prompt.bind(window);
window.confirm=m=>nativeConfirm(tr(String(m)));window.alert=m=>nativeAlert(tr(String(m)));window.prompt=(m,d)=>nativePrompt(tr(String(m)),d);
window.smartToolTr=tr;
const GLOBE='<svg viewBox="0 0 24 24" width="16" height="16" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/></svg>';
function paintButton(b){b.querySelector('span').textContent=I.lang==='en'?'EN':'PT';
  const label=I.lang==='en'?'Mudar para português':'Switch to English';b.title=label;b.setAttribute('aria-label',label)}
function button(){const b=document.createElement('button');b.type='button';b.id='lang-toggle';b.setAttribute('data-no-i18n','');
  b.innerHTML=GLOBE+'<span></span>';
  b.style.cssText='display:inline-flex;align-items:center;gap:6px;margin-left:12px;padding:5px 10px;border-radius:999px;border:1px solid var(--line,var(--border,#c9ced6));background:var(--surface,#fff);color:var(--ink,#1b2430);font:600 13px/1 "Segoe UI",system-ui,sans-serif;cursor:pointer;flex:none';
  paintButton(b);
  b.addEventListener('click',async()=>{const next=I.lang==='en'?'pt-BR':'en';b.disabled=true;
    try{const r=await fetch('/setup/language',{method:'POST',headers:{'Content-Type':'application/json','X-Setup-Token':I.token},body:JSON.stringify({language:next})});
      const data=await r.json().catch(()=>({}));if(!r.ok)throw Error(data.error||('HTTP '+r.status));location.reload()}
    catch(e){b.disabled=false;nativeAlert((I.lang==='en'?'Could not change the language: ':'Não foi possível trocar o idioma: ')+e.message)}});
  return b}
document.addEventListener('DOMContentLoaded',()=>{document.documentElement.lang=I.lang;walk(document.body);applyTitle();
  const header=document.querySelector('header');if(header)header.appendChild(button())});
})();
"""
