/* Análise estática de snapshots JS/TS. Não executa código nem lê arquivos do projeto. */
const fs = require('node:fs');
const path = require('node:path').posix;
const ts = require('./web_adapters/node/node_modules/typescript');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const clean = name => path.normalize(name.replaceAll('\\', '/')).replace(/^\.\//, '');
const docs = new Map(input.files.map(f => ['/snapshot/' + clean(f.path), f]));
const knownFiles = new Set([...docs.keys(),...(input.paths||[]).map(p=>'/snapshot/'+clean(p))]);
const names = [...docs.keys()].filter(n => /\.(?:[cm]?[jt]s|[jt]sx)$/.test(n));
const options = {allowJs:true, checkJs:true, jsx:ts.JsxEmit.Preserve, target:ts.ScriptTarget.Latest,
  module:ts.ModuleKind.ESNext, moduleResolution:ts.ModuleResolutionKind.Bundler,
  noLib:true, noEmit:true, skipLibCheck:true, allowImportingTsExtensions:true};
let configPath = docs.has('/snapshot/tsconfig.json') ? '/snapshot/tsconfig.json' : '/snapshot/jsconfig.json';
const configSeen = new Set();
function readConfig(name, depth=0) {
  if (depth>5 || configSeen.has(name) || !docs.has(name)) return {};
  configSeen.add(name);
  const raw = ts.parseConfigFileTextToJson(name, docs.get(name).text).config || {};
  const base = typeof raw.extends==='string' && raw.extends.startsWith('.') ? readConfig(clean(path.join(path.dirname(name),raw.extends.endsWith('.json')?raw.extends:raw.extends+'.json')),depth+1) : {};
  return {...base,...raw.compilerOptions};
}
const config=readConfig(configPath);
const aliases=config.paths||{},baseUrl=clean(path.join(path.dirname(configPath),config.baseUrl||'.'));
function candidate(base) {
  const variants=[base,base.replace(/\.[cm]?jsx?$/,'.ts'),base.replace(/\.jsx?$/,'.tsx'),
    ...['.ts','.tsx','.js','.jsx','.mts','.cts','.mjs','.cjs'].map(e=>base+e),
    ...['.ts','.tsx','.js','.jsx'].map(e=>base+'/index'+e)];
  const found=variants.find(name=>knownFiles.has(clean(name)));
  return found&&clean(found);
}
const workspaces=new Map();
for(const [name,file] of docs)if(name.endsWith('/package.json')&&!name.includes('/node_modules/')){try{const pkg=JSON.parse(file.text);if(typeof pkg.name==='string'&&pkg.name)workspaces.set(pkg.name,{dir:path.dirname(name),entries:[pkg.source,pkg.main,pkg.module].filter(e=>typeof e==='string')})}catch{}}
function workspace(spec){
  for(const [name,pkg] of workspaces){
    if(spec!==name&&!spec.startsWith(name+'/'))continue;
    const sub=spec.slice(name.length+1);
    if(sub)return candidate(path.join(pkg.dir,'src',sub))||candidate(path.join(pkg.dir,sub));
    return candidate(path.join(pkg.dir,'src/index'))||candidate(path.join(pkg.dir,'index'))||pkg.entries.map(e=>candidate(path.join(pkg.dir,e))).find(Boolean);
  }
  return undefined;
}
function resolve(spec, from) {
  if (spec.startsWith('.')) return candidate(clean(path.join(path.dirname(from),spec)));
  for (const [pattern, targets] of Object.entries(aliases)) {
    const [prefix,suffix='']=pattern.split('*');
    if (pattern.includes('*') ? spec.startsWith(prefix)&&spec.endsWith(suffix) : spec===pattern) {
      const middle=pattern.includes('*')?spec.slice(prefix.length,suffix? -suffix.length:undefined):'';
      for (const target of Array.isArray(targets)?targets:[]) {
        const found=candidate(clean(path.join(baseUrl,target.replace('*',middle))));
        if(found)return found;
      }
    }
  }
  const local=workspace(spec);
  if(local)return local;
  if(config.baseUrl)return candidate(clean(path.join(baseUrl,spec)));
  return undefined;
}
const host={
  getSourceFile:(name,version)=>docs.has(clean(name))?ts.createSourceFile(clean(name),docs.get(clean(name)).text,version,true):undefined,
  getDefaultLibFileName:()=>'/lib.d.ts',writeFile:()=>{},getCurrentDirectory:()=>'/snapshot',
  getDirectories:()=>[],fileExists:name=>docs.has(clean(name)),readFile:name=>docs.get(clean(name))?.text,
  getCanonicalFileName:name=>clean(name),useCaseSensitiveFileNames:()=>true,getNewLine:()=> '\n',
  resolveModuleNames:(modules,containing)=>modules.map(spec=>{const resolved=resolve(spec,containing);return resolved&&docs.has(resolved)&&/\.(?:[cm]?[jt]s|[jt]sx|json)$/.test(resolved)?{resolvedFileName:resolved,extension:ts.extensionFromPath(resolved),isExternalLibraryImport:false}:undefined;})
};
const program=ts.createProgram(names,options,host),checker=program.getTypeChecker();
const symbols=[],dependencies=[],calls=[],unresolved=[],diagnostics=[],declarations=new Map(),moduleNodes=new Map();
const maxSymbols=8000,maxEdges=20000;
function filePath(source){return source.fileName.replace('/snapshot/','');}
function line(source,pos){return source.getLineAndCharacterOfPosition(pos).line+1;}
function displayName(node) {
  if(node.name)return node.name.getText().replace(/^['"]|['"]$/g,'');
  if(ts.isConstructorDeclaration(node))return 'constructor';
  if(node.parent && ts.isVariableDeclaration(node.parent))return node.parent.name.getText();
  if(node.parent && ts.isPropertyAssignment(node.parent))return node.parent.name.getText();
  if(node.parent && ts.isPropertyDeclaration(node.parent))return node.parent.name.getText();
  if(node.parent && ts.isExportAssignment(node.parent))return 'default';
  return 'callback';
}
function isFunction(node){return !!node.body && (ts.isFunctionDeclaration(node)||ts.isMethodDeclaration(node)||ts.isFunctionExpression(node)||ts.isArrowFunction(node)||ts.isConstructorDeclaration(node)||ts.isGetAccessor(node)||ts.isSetAccessor(node));}
function addSymbol(node,source,owner){
  if(symbols.length>=maxSymbols)return null;
  let name=displayName(node),parent=node.parent;
  const classParent=parent&&ts.isPropertyDeclaration(parent)?parent.parent:parent;
  const cls=classParent && (ts.isClassDeclaration(classParent)||ts.isClassExpression(classParent)) ? classParent.name?.getText() : null;
  if(cls)name=cls+'.'+name;
  const start=line(source,node.getStart(source)),id=filePath(source)+'::'+name+'@'+start+':'+node.pos;
  const symbol={id,path:filePath(source),name,start_line:start,end_line:line(source,node.end),kind:cls?'method':'function',language:'typescript',parent:owner?.id||null};
  symbols.push(symbol);declarations.set(node,symbol);return symbol;
}
const parsed=[];
for(const source of program.getSourceFiles()){
  if(!docs.has(source.fileName))continue;
  if(source.parseDiagnostics.length){diagnostics.push({path:filePath(source),reason:'Incomplete or invalid syntax in the snapshot.',line:line(source,source.parseDiagnostics[0].start||0)});continue;}
  parsed.push(source);
  function collect(node,owner){const current=isFunction(node)?addSymbol(node,source,owner):owner;ts.forEachChild(node,child=>collect(child,current));}
  collect(source,null);
}
function moduleSymbol(source){if(!moduleNodes.has(source.fileName)){const s={id:filePath(source)+'::<module>',path:filePath(source),name:'<module>',start_line:1,end_line:1,kind:'module',language:'typescript'};moduleNodes.set(source.fileName,s);symbols.push(s);}return moduleNodes.get(source.fileName);}
function targetFor(node){
  const signature=checker.getResolvedSignature(node);let target=declarations.get(signature?.declaration);
  if(target)return target;
  let symbol=checker.getSymbolAtLocation(node.expression);
  if(symbol?.flags & ts.SymbolFlags.Alias)symbol=checker.getAliasedSymbol(symbol);
  for(const declaration of symbol?.declarations||[]){
    if(declarations.has(declaration))return declarations.get(declaration);
    if(ts.isVariableDeclaration(declaration)&&declarations.has(declaration.initializer))return declarations.get(declaration.initializer);
  }
  return null;
}
for(const source of parsed){
  function visit(node,owner){
    if(isFunction(node) && !declarations.has(node))return;
    const active=declarations.get(node)||owner;
    if((ts.isImportDeclaration(node)||ts.isExportDeclaration(node)) && node.moduleSpecifier && ts.isStringLiteral(node.moduleSpecifier)) {
      const spec=node.moduleSpecifier.text,target=resolve(spec,source.fileName);
      if(dependencies.length<maxEdges)dependencies.push({source:filePath(source),target:target?.replace('/snapshot/','')||null,specifier:spec,line:line(source,node.getStart(source)),kind:node.importClause?.isTypeOnly||node.isTypeOnly?'type_import':'import',resolution:target?'resolved':spec.startsWith('.')?'unresolved':'external'});
    }
    if(ts.isCallExpression(node)||ts.isNewExpression(node)){
      const expression=node.expression,at=line(source,node.getStart(source));
      if(ts.isCallExpression(node) && (expression.kind===ts.SyntaxKind.ImportKeyword || ts.isIdentifier(expression)&&expression.text==='require') && node.arguments?.length && ts.isStringLiteral(node.arguments[0])){
        const spec=node.arguments[0].text,target=resolve(spec,source.fileName);
        if(dependencies.length<maxEdges)dependencies.push({source:filePath(source),target:target?.replace('/snapshot/','')||null,specifier:spec,line:at,kind:'dynamic_import',resolution:target?'resolved':spec.startsWith('.')?'unresolved':'external'});
      }else{
        const target=targetFor(node);
        if(target && calls.length<maxEdges)calls.push({source:(active||moduleSymbol(source)).id,target:target.id,line:at,kind:ts.isNewExpression(node)?'construct':'call',resolution:'static'});
        else if(unresolved.length<1000)unresolved.push({path:filePath(source),line:at,expression:expression.getText(source).slice(0,120),reason:'External or dynamic target, or no resolved implementation in the snapshot.'});
      }
    }
    ts.forEachChild(node,child=>visit(child,active));
  }
  visit(source,null);
}
// Metadados Angular são lidos da AST; templates são analisados pelo compilador,
// sem inicializar a aplicação, resolver DI em runtime ou executar seus decorators.
const components=[],pipes=[],templatePaths=new Set();
for(const source of parsed){
 const angularAliases=new Map(),angularNamespaces=new Set();
 for(const statement of source.statements){if(ts.isImportDeclaration(statement)&&statement.moduleSpecifier.text==='@angular/core'){const bindings=statement.importClause?.namedBindings;if(bindings&&ts.isNamedImports(bindings))for(const spec of bindings.elements)angularAliases.set(spec.name.text,(spec.propertyName||spec.name).text);else if(bindings&&ts.isNamespaceImport(bindings))angularNamespaces.add(bindings.name.text)}}
 function visitClasses(node){if(ts.isClassDeclaration(node))for(const decorator of ts.canHaveDecorators(node)?ts.getDecorators(node)||[]:[]){const call=decorator.expression;if(!ts.isCallExpression(call)||!call.arguments.length||!ts.isObjectLiteralExpression(call.arguments[0]))continue;let type=null;if(ts.isIdentifier(call.expression)){const symbol=checker.getSymbolAtLocation(call.expression);if(symbol?.declarations?.some(ts.isImportSpecifier))type=angularAliases.get(call.expression.text)}else if(ts.isPropertyAccessExpression(call.expression)&&angularNamespaces.has(call.expression.expression.getText(source)))type=call.expression.name.text;if(!['Component','Pipe'].includes(type))continue;const metadata=new Map(call.arguments[0].properties.filter(ts.isPropertyAssignment).map(p=>[p.name.getText(source).replace(/^['"]|['"]$/g,''),p.initializer]));const methods=new Map();for(const member of node.members){const symbol=declarations.get(member)||declarations.get(member.initializer);if(symbol&&member.name)methods.set(member.name.getText(source).replace(/^['"]|['"]$/g,''),symbol)}const literal=n=>n&&ts.isStringLiteralLike(n)?n.text:null;const entry={source,node,name:node.name?.text||'<component>',methods,selector:literal(metadata.get('selector'))};if(type==='Pipe'){entry.pipeName=literal(metadata.get('name'));pipes.push(entry);continue}const template=metadata.get('template'),url=literal(metadata.get('templateUrl'));entry.template=literal(template);entry.templatePath=source.fileName;entry.inlineLine=template?line(source,template.getStart(source)):1;if(url){const target=resolve(url.startsWith('.')?url:'./'+url,source.fileName);entry.templatePath=target;entry.template=target?docs.get(target)?.text:null;entry.inlineLine=1;dependencies.push({source:filePath(source),target:target?.replace('/snapshot/','')||null,specifier:url,line:line(source,node.getStart(source)),kind:'angular_template',resolution:target?'resolved':'unresolved'})}for(const key of ['styleUrl','styleUrls']){const value=metadata.get(key),values=value&&ts.isArrayLiteralExpression(value)?value.elements:[value];for(const item of values){const style=literal(item);if(!style)continue;const target=resolve(style.startsWith('.')?style:'./'+style,source.fileName);dependencies.push({source:filePath(source),target:target?.replace('/snapshot/','')||null,specifier:style,line:line(source,item.getStart(source)),kind:'angular_style',resolution:target?'resolved':'unresolved'})}}components.push(entry)}ts.forEachChild(node,visitClasses)}visitClasses(source)
}
if(components.some(c=>c.template!=null)){
 try{
  const {parseTemplate}=require('./web_adapters/node/node_modules/@angular/compiler/fesm2022/compiler.mjs');
  for(const component of components){if(component.template==null||!component.templatePath)continue;const pathName=component.templatePath.replace('/snapshot/',''),tree=parseTemplate(component.template,pathName);if(tree.errors?.length){diagnostics.push({path:pathName,reason:'Invalid Angular template or syntax not supported in this version.'});continue}templatePaths.add(pathName);const owner={id:pathName+'::'+filePath(component.source)+'::'+component.name+'.template@'+component.inlineLine,path:pathName,name:component.name+'.template',kind:'template',language:'angular',start_line:component.inlineLine,end_line:component.inlineLine+component.template.split('\n').length-1,location_kind:component.templatePath===component.source.fileName?'inline_template':'source_line'};if(symbols.length>=maxSymbols)break;symbols.push(owner);const shadow=new Set(),seen=new WeakSet();function walk(n,fn){if(!n||typeof n!=='object'||seen.has(n))return;seen.add(n);fn(n);for(const [key,value] of Object.entries(n)){if(['sourceSpan','span','startSourceSpan','endSourceSpan','keySpan','valueSpan','parent'].includes(key))continue;if(Array.isArray(value))value.forEach(x=>walk(x,fn));else if(value&&typeof value==='object')walk(value,fn)}}walk(tree.nodes,n=>{if(['Variable','Reference','LetDeclaration'].includes(n.constructor.name)&&n.name)shadow.add(n.name);if(n.constructor.name==='ForLoopBlock'&&n.item?.name)shadow.add(n.item.name)});const seen2=new WeakSet();function inspect(n){if(!n||typeof n!=='object'||seen2.has(n))return;seen2.add(n);const start=n.sourceSpan?.start,at=(typeof start==='number'?component.template.slice(0,start).split('\n').length-1:(start?.line??0))+component.inlineLine;if(n.constructor.name==='Call'||n.constructor.name==='SafeCall'){const receiver=n.receiver;if(receiver?.constructor.name==='PropertyRead'&&['ImplicitReceiver','ThisReceiver'].includes(receiver.receiver?.constructor.name)){const target=shadow.has(receiver.name)?null:component.methods.get(receiver.name);if(target&&calls.length<maxEdges)calls.push({source:owner.id,target:target.id,line:at,kind:'template_call',resolution:'static'});else if(unresolved.length<1000)unresolved.push({path:pathName,line:at,expression:receiver.name+'()',reason:'Unresolved template method, or a local variable that shadows the component.'})}}if(n.constructor.name==='Element'){const matches=components.filter(c=>c.selector===n.name);if(matches.length===1&&filePath(matches[0].source)!==pathName)dependencies.push({source:pathName,target:filePath(matches[0].source),specifier:n.name,line:at,kind:'angular_component',resolution:'resolved'})}if(n.constructor.name==='BindingPipe'){const matches=pipes.filter(p=>p.pipeName===n.name&&p.methods.has('transform'));if(matches.length===1){const pipe=matches[0];dependencies.push({source:pathName,target:filePath(pipe.source),specifier:n.name,line:at,kind:'angular_pipe',resolution:'resolved'});calls.push({source:owner.id,target:pipe.methods.get('transform').id,line:at,kind:'template_pipe',resolution:'static'})}}for(const [key,value] of Object.entries(n)){if(['sourceSpan','span','startSourceSpan','endSourceSpan','keySpan','valueSpan','parent'].includes(key))continue;if(Array.isArray(value))value.forEach(inspect);else if(value&&typeof value==='object')inspect(value)}}inspect(tree.nodes)
  }
 }catch{diagnostics.push({path:'Angular',reason:'The Angular template analyzer did not finish. Check the local runtime.'})}
}
process.stdout.write(JSON.stringify({symbols,dependencies:dependencies.slice(0,maxEdges),calls:calls.slice(0,maxEdges),unresolved,diagnostics,parsed:[...parsed.map(filePath),...templatePaths],truncated:symbols.length>=maxSymbols||calls.length>=maxEdges||dependencies.length>=maxEdges}));
