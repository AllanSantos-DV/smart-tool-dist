// Análise estática conservadora de Java a partir de snapshots; nenhum código é executado.
import fs from 'node:fs';
import {parse} from './web_adapters/node/node_modules/java-parser/src/index.js';
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const symbols=[],dependencies=[],calls=[],unresolved=[],diagnostics=[],units=[],classes=new Map(),methodNodes=new Map();
const MAX_SYMBOLS=8000,MAX_EDGES=20000;
function children(n){return n?.children?Object.values(n.children).flat().filter(x=>x.children):[]}
function all(n,name,stop=false){const result=[];function visit(node){if(node?.name===name){result.push(node);if(stop)return}for(const child of children(node))visit(child)}visit(n);return result}
function first(n,name){return all(n,name,true)[0]}
function identifiers(n){const out=[];function visit(node){if(node?.children)for(const [key,items] of Object.entries(node.children))for(const child of items){if(child.children)visit(child);else if(key==='Identifier')out.push(child)}}visit(n);return out.sort((a,b)=>a.startOffset-b.startOffset)}
function text(u,n){return n?.location?u.text.slice(n.location.startOffset,n.location.endOffset+1):''}
function addSymbol(u,n,name,kind){if(symbols.length>=MAX_SYMBOLS)return null;const s={id:u.path+'::'+name+'@'+n.location.startLine+':'+n.location.startOffset,path:u.path,name,kind,language:'java',start_line:n.location.startLine,end_line:n.location.endLine};symbols.push(s);return s}
const classKinds=new Set(['normalClassDeclaration','normalInterfaceDeclaration','enumDeclaration','recordDeclaration']);
for(const file of input.files){try{const tree=parse(file.text),pkg=first(tree,'packageDeclaration'),unit={...file,tree,package:pkg?identifiers(pkg).map(t=>t.image).join('.'): '',imports:[],classes:[]};
 for(const n of all(tree,'importDeclaration')){const raw=text(unit,n).replace(/^import\s+/,'').replace(/;\s*$/,'').trim(),stat=raw.startsWith('static '),spec=(stat?raw.slice(7):raw).replace(/\s+/g,'');unit.imports.push({spec,static:stat,line:n.location.startLine})}
 function scan(n,parent=null){let current=parent;if(classKinds.has(n.name)){const id=first(n,'typeIdentifier')||first(n,'typeName'),short=identifiers(id||n)[0]?.image;if(!short)return;const local=(parent?parent.local+'.':'')+short,fqn=(unit.package?unit.package+'.':'')+local;const symbol=addSymbol(unit,n,local,n.name==='normalInterfaceDeclaration'?'interface':'class');if(!symbol)return;current={unit,fqn,local,short,symbol,node:n,body:n.children.classBody?.[0]||n.children.interfaceBody?.[0]||n.children.recordBody?.[0]||n.children.enumBody?.[0],methods:[],fields:new Map(),extends:text(unit,n.children.classExtends?.[0]).replace(/^extends\s+/, '').trim()};classes.set(fqn,classes.has(fqn)?null:current);unit.classes.push(current)}
 if(current&&n.name==='classBody'&&n!==current.body)return;
 if(current&&['methodDeclaration','interfaceMethodDeclaration','constructorDeclaration','compactConstructorDeclaration'].includes(n.name)){const declarator=first(n,'methodDeclarator')||first(n,'constructorDeclarator'),ctor=n.name.includes('Constructor')||n.name==='constructorDeclaration',name=ctor?current.short:(declarator?.children.Identifier?.[0]?.image);if(name){const params=all(declarator||n,'formalParameter'),types=params.map(p=>text(unit,first(p,'unannType'))||'?');const signature=name+'('+types.join(',')+')';const sym=addSymbol(unit,n,current.local+'.'+signature,ctor?'constructor':'method');if(sym){sym.signature=signature;const method={symbol:sym,name,arity:params.length,types,node:n,cls:current,params,concrete:!!first(n,'block')||ctor};current.methods.push(method);methodNodes.set(n,method)}}}
 for(const child of children(n))scan(child,current)}scan(tree);units.push(unit)
 }catch{diagnostics.push({path:file.path,reason:'Invalid or fragmented Java, or syntax not supported by the parser.'})}}
function resolveType(unit,raw,context){let name=raw.replace(/@[\w.]+(?:\([^)]*\))?/g,'').replace(/<.*>/g,'').replace(/\[\]|\.\.\./g,'').trim();if(!name||['void','boolean','byte','short','int','long','float','double','char','var'].includes(name))return null;if(classes.has(name))return classes.get(name);const candidates=new Set();const local=[context?context.fqn+'.'+name:'',unit.package?unit.package+'.'+name:name];for(const fq of local)if(classes.has(fq))candidates.add(fq);for(const item of unit.imports.filter(i=>!i.static)){if(item.spec.endsWith('.'+name)&&classes.has(item.spec))candidates.add(item.spec);if(item.spec.endsWith('.*')){const fq=item.spec.slice(0,-1)+name;if(classes.has(fq))candidates.add(fq)}}return candidates.size===1?classes.get([...candidates][0]):null}
function lookupMethod(cls,name,arity,visited=new Set()){if(!cls||visited.has(cls.fqn))return null;visited.add(cls.fqn);const methods=cls.methods.filter(m=>m.name===name&&m.arity===arity&&m.concrete);if(methods.length===1)return methods[0].symbol;if(methods.length>1)return null;return cls.extends?lookupMethod(resolveType(cls.unit,cls.extends,cls),name,arity,visited):null}
function putBinding(map,name,type){if(name)map.set(name,map.has(name)&&map.get(name)!==type?null:type)}
function scoped(node,name){const out=[];function scan(n){if(n!==node&&(classKinds.has(n.name)||n.name==='lambdaExpression'||n.name==='classBody'))return;if(n.name===name)out.push(n);for(const c of children(n))scan(c)}scan(node);return out}
function collectVariables(unit,node,map){for(const parentName of ['fieldDeclaration','localVariableDeclaration','variableParaRegularParameter','variableArityParameter'])for(const declaration of scoped(node,parentName)){const type=text(unit,first(declaration,'unannType'));for(const id of all(declaration,'variableDeclaratorId'))putBinding(map,identifiers(id)[0]?.image,type)}}
for(const unit of units){for(const cl of unit.classes){const fieldNodes=scoped(cl.body,'fieldDeclaration');for(const node of fieldNodes){const type=text(unit,first(node,'unannType'));for(const id of all(node,'variableDeclaratorId'))putBinding(cl.fields,identifiers(id)[0]?.image,type)}}}
for(const unit of units){
 for(const item of unit.imports){const spec=item.spec;let target=classes.get(item.static?spec.split('.').slice(0,-1).join('.'):spec);dependencies.push({source:unit.path,target:target?.unit.path||null,specifier:spec,line:item.line,kind:item.static?'java_static_import':'import',resolution:target?'resolved':'external'})}
 function walk(n,cls=null,method=null,vars=new Map(),owner=null){if(classKinds.has(n.name))cls=unit.classes.find(c=>c.node===n)||cls;if(cls&&n.name==='classBody'&&n!==cls.body)return;
 if(methodNodes.has(n)){method=methodNodes.get(n);vars=new Map(cls.fields);collectVariables(unit,n,vars);owner=method.symbol}
 if(n.name==='lambdaExpression'){vars=new Map(vars);for(const param of identifiers(n.children.lambdaParameters?.[0]))vars.set(param.image,null);owner=addSymbol(unit,n,(cls?.local||'')+'.lambda@'+n.location.startLine,'function')||owner}
 if(cls&&['unannType','classType','interfaceType'].includes(n.name)){const type=resolveType(unit,text(unit,n),cls);if(type&&type.unit.path!==unit.path&&dependencies.length<MAX_EDGES)dependencies.push({source:unit.path,target:type.unit.path,specifier:type.fqn,line:n.location.startLine,kind:'java_type',resolution:'resolved'})}
 if(cls&&n.name==='primary'){
   const prefix=n.children.primaryPrefix?.[0],prefixText=text(unit,prefix).trim(),suffixes=n.children.primarySuffix||[];let chain=prefixText,fluent=false;
   const newMatch=/^new\s+([\w.$]+)/.exec(prefixText);let constructed=newMatch?resolveType(unit,newMatch[1],cls):null;
   const link=(type,line)=>{if(type&&type.unit.path!==unit.path&&dependencies.length<MAX_EDGES)dependencies.push({source:unit.path,target:type.unit.path,specifier:type.fqn,line,kind:'java_type',resolution:'resolved'})};
   if(constructed&&calls.length<MAX_EDGES)calls.push({source:(owner||cls.symbol).id,target:constructed.symbol.id,line:n.location.startLine,kind:'construct',resolution:'static'});
   link(constructed,n.location.startLine);
   const staticHead=/^([A-Z][\w$]*)\./.exec(prefixText);if(staticHead&&!vars.has(staticHead[1]))link(resolveType(unit,staticHead[1],cls),n.location.startLine);
   for(const suffix of suffixes){const invoke=suffix.children.methodInvocationSuffix?.[0];if(!invoke){chain+=text(unit,suffix);continue}const args=invoke.children.argumentList?.[0],arity=args?(args.children.expression||[]).length:0;const match=/^(?:([\w.$]+)\.)?([\w$]+)$/.exec(chain);let target=null;
     if(match&&!fluent){const receiver=match[1],name=match[2];let type=null;if(!receiver||receiver==='this')type=cls;else if(receiver==='super')type=resolveType(unit,cls.extends,cls);else if(receiver.startsWith('this.'))type=resolveType(unit,cls.fields.get(receiver.slice(5))||'',cls);else if(vars.has(receiver))type=resolveType(unit,vars.get(receiver)||'',cls);else{type=resolveType(unit,receiver,cls);link(type,invoke.location.startLine)}target=lookupMethod(type,name,arity);if(!target&&!receiver){const candidates=unit.imports.filter(i=>i.static&&(i.spec.endsWith('.'+name)||i.spec.endsWith('.*'))).map(i=>lookupMethod(classes.get(i.spec.split('.').slice(0,-1).join('.')),name,arity)).filter(Boolean);if(candidates.length===1)target=candidates[0]}}
     if(constructed&&!fluent){const methodName=/\.([\w$]+)$/.exec(chain)?.[1];if(methodName)target=lookupMethod(constructed,methodName,arity)}
     if(target&&calls.length<MAX_EDGES)calls.push({source:(owner||cls.symbol).id,target:target.id,line:invoke.location.startLine,kind:'call',resolution:'static'});
     else if(unresolved.length<1000)unresolved.push({path:unit.path,line:invoke.location.startLine,expression:chain.slice(0,140),reason:'External/dynamic type, interface without implementation, or ambiguous overload in the Java snapshot.'});
     fluent=true;chain+='<result>';
   }
 }
 for(const child of children(n))walk(child,cls,method,vars,owner)
 }walk(unit.tree)
}
const ids=new Set(symbols.map(s=>s.id));
process.stdout.write(JSON.stringify({symbols,dependencies:dependencies.slice(0,MAX_EDGES),calls:calls.filter(c=>ids.has(c.source)&&ids.has(c.target)),unresolved,diagnostics,parsed:units.map(u=>u.path),truncated:symbols.length>=MAX_SYMBOLS||dependencies.length>MAX_EDGES||calls.length>=MAX_EDGES}));
