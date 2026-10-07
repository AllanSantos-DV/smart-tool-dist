"""Referências locais de HTML, CSS e Markdown; documentos textuais mantêm cobertura."""
import posixpath
import re
from html.parser import HTMLParser
from urllib.parse import unquote,urlsplit


def analyze(sources,all_paths):
    paths=set(all_paths);dependencies=[];diagnostics=[];parsed=[]
    def reference(origin,value,line,kind):
        if not value or value.startswith(('#','data:','javascript:','mailto:','tel:')):return
        url=urlsplit(value)
        if url.scheme or url.netloc:
            dependencies.append({'source':origin,'target':None,'specifier':value[:300],'line':line,'kind':kind,'resolution':'external'});return
        target=posixpath.normpath(posixpath.join(posixpath.dirname(origin),unquote(url.path))) if not url.path.startswith('/') else unquote(url.path).lstrip('/')
        if target.startswith('../') or target==origin:return
        found=target if target in paths else None
        dependencies.append({'source':origin,'target':found,'specifier':value[:300],'line':line,'kind':kind,'resolution':'resolved' if found else 'unresolved'})
    class Html(HTMLParser):
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            for attr in ('src','href','poster'):
                if attr in attrs:reference(self.origin,attrs[attr],self.getpos()[0],'html_resource' if tag!='a' else 'document_link')
            if attrs.get('srcset'):
                for item in attrs['srcset'].split(','):
                    value=item.strip().split(' ')[0]
                    if value:reference(self.origin,value,self.getpos()[0],'html_resource')
    def css_links(text):
        # Scanner de tokens: ignora comentários e strings que apenas mencionam url().
        i=0
        def quoted(at):
            quote=text[at];j=at+1;out=[]
            while j<len(text):
                if text[j]=='\\' and j+1<len(text):out.append(text[j+1]);j+=2;continue
                if text[j]==quote:return ''.join(out),j+1
                out.append(text[j]);j+=1
            return ''.join(out),j
        while i<len(text):
            if text.startswith('/*',i):
                end=text.find('*/',i+2);i=len(text) if end<0 else end+2;continue
            if text[i] in ('"',"'"):
                _,i=quoted(i);continue
            match=re.match(r'(?i)(@import\s+|url\s*\()',text[i:])
            if match and (i==0 or not (text[i-1].isalnum() or text[i-1] in '_-')):
                start=i;i+=len(match[0])
                while i<len(text) and text[i].isspace():i+=1
                if i<len(text) and text[i] in ('"',"'"):value,i=quoted(i);yield value,text.count('\n',0,start)+1
                elif match[0].lower().startswith('url'):
                    end=text.find(')',i)
                    if end<0:return
                    value=text[i:end].strip();i=end+1;yield value,text.count('\n',0,start)+1
                continue
            i+=1
    for path,text in sources.items():
        extension=posixpath.splitext(path)[1].lower()
        if extension in ('.html','.htm'):
            parser=Html(convert_charrefs=True);parser.origin=path
            try:parser.feed(text);parsed.append(path)
            except (ValueError,AssertionError):diagnostics.append({'path':path,'reason':'HTML could not be fully parsed.'})
        elif extension in ('.css','.scss','.sass','.less'):
            for target,line in css_links(text):reference(path,target,line,'style_resource')
            parsed.append(path)
        elif extension in ('.md','.markdown'):
            in_fence=False
            for line_no,line in enumerate(text.splitlines(),1):
                if re.match(r'^\s*(```|~~~)',line):in_fence=not in_fence;continue
                if in_fence:continue
                line=re.sub(r'`[^`]*`','',line)
                for match in re.finditer(r'!?\[[^\]]*\]\(<?([^\s)>]+)>?(?:\s+[^)]*)?\)',line):reference(path,match[1],line_no,'document_link')
            parsed.append(path)
    return {'dependencies':dependencies,'diagnostics':diagnostics,'parsed':parsed}
